#!/usr/bin/env python3
"""Analyse the consumer cause lists downloaded by courts/consumer.py.

Writes the same four files as analysis.py does for the KMC, in storage/consumer/ (via storage.py;
rebuilt from scratch on every run), so viewer.py can show them with `--court consumer`:
  enquiries.json   - everything, one object per case
  status.csv       - one row per case: case_key, enquiry_id (the case number), status, date the status first appeared
  parties.csv      - one row per party: complainants and respondents (party_type as in analysis.py)
  timeline.csv     - one row per case per cause-list date

Differences from the KMC data, because a cause list is only a schedule:
  * status is "listed" -- the case is on a cause list -- never open/disposed/dismissed, which the
    lists do not say. status_since is the date it first appeared.
  * each timeline row is a listing: its purpose is the stage printed on the list ("FOR DIRECTIONS",
    "FOR PRONOUNCEMENT OF ORDER", ...) with the court number and bench. There is no outcome text and no
    next date; the viewer then shows the next date the case was listed on. "held" means the date has passed.
  * the portal's per-case "dateOfHearing" field is kept in raw_text only; its meaning is not established.
  * advocates are kept in enquiries.json and raw_text, not in the CSVs.

Where a cause list was revised (<date>_2.json ...) only the latest revision is used.

Usage:  python analysis_consumer.py
"""

import glob
import json
import os
import re
import sys
from datetime import date

import storage
from analysis import party_type
from courts import BASE_DIR

COURT = "consumer"
DATA_DIR = os.path.join(BASE_DIR, "causelists", COURT)

FILE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:_(\d+))?\.json$")
CASE_NO_RE = re.compile(r"(?P<prefix>.+)/(?P<serial>\d+)/(?P<year>\d{4})$")
TIME_RE = re.compile(r"\bAT\s+(\d{1,2}):(\d{2})\s*([AP])\.?\s*M", re.IGNORECASE)
# (tag, regex) for the stage printed on the list
STAGE_TAGS = [("admission", r"admission"), ("directions", r"direction"), ("order", r"pronouncement|\border\b"),
              ("arguments", r"argument"), ("evidence", r"evidence"), ("hearing", r"hearing")]

warnings = []


def warn(msg):
    warnings.append(msg)


def case_key(case_number):
    """Unique key: PREFIX_serial_year, e.g. DC/525/CC/763/2026 -> DC-525-CC_763_2026 (viewer.py splits on "_")."""
    m = CASE_NO_RE.fullmatch(case_number)
    if not m:
        raise ValueError(f"unrecognised case number: {case_number}")
    return f"{m['prefix'].replace('/', '-')}_{int(m['serial'])}_{m['year']}"


def latest_files():
    """{(commission folder, date): path} -- the highest revision of each commission's list for each date."""
    best = {}
    for path in glob.glob(os.path.join(DATA_DIR, "*", "*.json")):
        m = FILE_RE.search(path)
        if not m:
            continue
        key = (os.path.basename(os.path.dirname(path)), m.group(1))
        version = int(m.group(2) or 1)
        if key not in best or version > best[key][0]:
            best[key] = (version, path)
    return {k: p for k, (v, p) in sorted(best.items())}


def list_time(stage):
    m = TIME_RE.search(stage or "")
    if not m:
        return ""
    hour, minute, ampm = int(m[1]) % 12, m[2], m[3].upper()
    return f"{hour + (12 if ampm == 'P' else 0):02d}:{minute}"


def stage_tags(stage):
    return [tag for tag, pat in STAGE_TAGS if re.search(pat, stage or "", re.I)]


def names(items, field):
    return [i[field].strip() for i in items or [] if i.get(field) and i[field].strip()]


def collect():
    """{case number: case} from every cause list file."""
    cases = {}
    for (commission, day), path in latest_files().items():
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        source = os.path.relpath(path, DATA_DIR)
        for row in doc["causelists"]:
            for c in row["cases"]:
                number = " ".join((c.get("caseNumber") or "").split())
                try:
                    key = case_key(number)
                except ValueError as e:
                    warn(f"{source}: {e}")
                    continue
                case = cases.setdefault(number, {
                    "enquiry_id": number, "case_key": key, "case_type": c.get("caseTypeName") or "",
                    "commission": doc["commission"], "complainants": [], "respondents": [],
                    "advocates": {"complainant": [], "respondent": []}, "hearings": {}})
                list_date = row.get("clDate") or doc["date"]
                # parties and advocates as of the latest list; the first-seen order is kept
                for field, listed in (("complainants", names(c.get("complainantList"), "complainant_name")),
                                      ("respondents", names(c.get("respondantList"), "respondant_name"))):
                    if list_date >= case.get("as_of", ""):
                        case[field] = listed
                for role, listed in (("complainant", names(c.get("complainantAdvocateList"), "advocate_name")),
                                     ("respondent", names(c.get("respondantAdvocateList"), "advocate_name"))):
                    case["advocates"][role] += [a for a in listed if a not in case["advocates"][role]]
                case["as_of"] = max(case.get("as_of", ""), list_date)
                stage = c.get("causelistTypeName") or ""
                bench = ", ".join(names(c.get("judgeName"), "judge_name"))
                court_no = c.get("courtRoomName") or ""
                raw = "\n".join(filter(None, [
                    f"[{source}]", f"case: {number} ({c.get('caseTypeName') or ''})", f"court: {court_no}",
                    f"stage: {stage}", f"before: {bench}",
                    f"complainant: {'; '.join(names(c.get('complainantList'), 'complainant_name'))}",
                    f"respondent: {'; '.join(names(c.get('respondantList'), 'respondant_name'))}",
                    f"advocates: {'; '.join(names(c.get('complainantAdvocateList'), 'advocate_name'))}"
                    f" / {'; '.join(names(c.get('respondantAdvocateList'), 'advocate_name'))}",
                    f"date of hearing recorded on the list: {c.get('dateOfHearing') or ''}"]))
                case["hearings"].setdefault(list_date, {
                    "date": list_date, "stage": stage, "court": court_no, "bench": bench,
                    "time": list_time(stage), "sources": [source], "raw": raw})
    return cases


def build(cases, today=None):
    today = today or date.today().isoformat()
    result = {}
    for number, c in sorted(cases.items(), key=lambda kv: kv[1]["case_key"]):
        hearings = []
        for d, h in sorted(c["hearings"].items()):
            purpose = h["stage"] + (f" -- court {h['court']}" if h["court"] else "") + (f", before {h['bench']}" if h["bench"] else "")
            tags = stage_tags(h["stage"])
            hearings.append({
                "date": d, "list": None, "list_title": h["stage"], "time": h["time"] or None,
                "time_source": "list heading" if h["time"] else None, "sources": h["sources"],
                "outcome": "", "next_date": None, "postponed_from": None, "next_stage": "",
                "raw": [h["raw"]], "next_stage_tags": [], "next_stage_details": {},
                "purpose": {"scheduled_on": d, "text": purpose, "tags": tags, "details": {}},
                "held": d < today})
        first, last = hearings[0]["date"], hearings[-1]["date"]
        result[number] = {"enquiry_id": number, "case_key": c["case_key"], "case_type": c["case_type"],
                          "commission": c["commission"], "complainants": c["complainants"], "respondents": c["respondents"],
                          "advocates": c["advocates"], "as_of": c["as_of"], "status": "listed", "status_since": first,
                          "first_seen": first, "last_seen": last, "hearings": hearings}
    return result


def write_outputs(result):
    storage.write_json(COURT, "enquiries.json", result)
    party_rows = []
    for eid, r in result.items():
        for role, listed in (("complainant", r["complainants"]), ("respondent", r["respondents"])):
            for name in listed:
                party_rows.append([eid, name, party_type(name, role), role, r["as_of"], r["case_key"]])
    storage.write_csv(COURT, "parties.csv", ["enquiry_id", "party", "party_type", "role", "party_as_of", "case_key"], party_rows)
    storage.write_csv(COURT, "status.csv", ["case_key", "enquiry_id", "status", "status_since"],
                      [[r["case_key"], eid, r["status"], r["status_since"]] for eid, r in result.items()])
    timeline_rows = []
    for eid, r in result.items():
        for h in r["hearings"]:
            p = h["purpose"]
            timeline_rows.append([eid, h["date"], h["time"] or "", h["time_source"] or "", h["list"] or "",
                                  "yes" if h["held"] else "no", ", ".join(p["tags"]), p["text"],
                                  h["outcome"], h["next_date"] or "", h["postponed_from"] or "", "", "",
                                  "\n\n".join(h["raw"]), r["case_key"]])
    storage.write_csv(COURT, "timeline.csv",
                      ["enquiry_id", "hearing_date", "hearing_time", "time_source", "list", "held", "purpose_tags", "purpose_text",
                       "outcome_text", "next_date", "postponed_from", "next_stage_tags", "cross_examination_of", "raw_text", "case_key"],
                      timeline_rows)


def main():
    files = latest_files()
    result = build(collect())
    write_outputs(result)
    print(f"{len(files)} cause lists, {len(result)} cases -> {os.path.relpath(storage.court_dir(COURT), os.getcwd())}/")
    for w in warnings:
        print("warning:", w, file=sys.stderr)


if __name__ == "__main__":
    main()
