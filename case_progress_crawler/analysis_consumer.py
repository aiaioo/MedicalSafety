#!/usr/bin/env python3
"""Analyse the consumer cause lists downloaded by courts/consumer.py.

Stores the same four tables as analysis.py does for the KMC, plus advocates, for the court "consumer" (via progress_storage.py;
rebuilt from scratch on every run), so viewer.py can show them with `--court consumer`:
  enquiries        - everything, one object per case
  status           - one row per case: case_key, enquiry_id (the case number), status, date the status first appeared,
                     and the label (medical / not_medical / unclear, with the reason) that case_details.py gave the case
                     once its details were downloaded -- empty if they have not been (the viewer hides not_medical)
  parties          - one row per party: complainants and respondents (party_type as in analysis.py)
  timeline         - one row per case per cause-list date

Differences from the KMC data, because a cause list is only a schedule:
  * status is "listed" -- the case is on a cause list -- never open/disposed/dismissed, which the
    lists do not say. status_since is the date it first appeared.
  * each timeline row is a listing: its purpose is the stage printed on the list ("FOR DIRECTIONS",
    "FOR PRONOUNCEMENT OF ORDER", ...) with the court number and bench. There is no outcome text and no
    next date; the viewer then shows the next date the case was listed on. "held" means the date has passed.
  * the portal's per-case "dateOfHearing" field is kept in raw_text only; its meaning is not established.
  * advocates (named on the cause lists, and in each case's status once its details are downloaded) are
    stored in the advocates table, one row per case, side and advocate field as printed; viewer.py cleans the names.

Where a cause list was revised only the latest revision is used.

Usage:  python analysis_consumer.py
"""

import re
import sys
from datetime import date

import progress_storage as storage
from analysis import party_type

COURT = "consumer"

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


def source_name(cause_list):
    """How a cause list is named in the raw text of a hearing: <commission>/<date>[_<version>].json, as its file was."""
    suffix = "" if cause_list["version"] == 1 else f"_{cause_list['version']}"
    return f"{cause_list['commission_id']}/{cause_list['date']}{suffix}.json"


def read_labels():
    """{case key: (label, reason)} from the classification of each case whose details were downloaded."""
    return {k: (d["label"], d["reason"]) for k, d in storage.load_case_documents("classification.json").items()
            if "label" in d and "reason" in d}


def detail_advocates(status):
    """{"complainant": [...], "respondent": [...]} from a case's downloaded status (None if not downloaded)."""
    status = status or {}
    return {role: [a.strip() for a in status.get(f"{role}Advocate") or [] if isinstance(a, str) and a.strip()]
            for role in ("complainant", "respondent")}


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


def ist_date(value):
    """YYYY-MM-DD of a portal date; a UTC timestamp like 2026-11-04T18:30:00.000Z is 5:30 ahead (2026-11-05, India)."""
    value = value or ""
    if "T" not in value:
        return value
    from datetime import datetime, timedelta
    return (datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def collect():
    """{case number: case} from every cause list file."""
    cases = {}
    for cause_list in storage.latest_cause_lists():
        doc, source = cause_list["doc"], source_name(cause_list)
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
                list_date = ist_date(row.get("clDate") or doc["date"])
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
    party_rows = []
    for eid, r in result.items():
        for role, listed in (("complainant", r["complainants"]), ("respondent", r["respondents"])):
            for name in listed:
                party_rows.append([eid, name, party_type(name, role), role, r["as_of"], r["case_key"]])
    advocate_rows = []
    statuses = storage.load_case_documents("status.json")
    for eid, r in result.items():
        details = detail_advocates(statuses.get(r["case_key"]))
        for role in ("complainant", "respondent"):
            seen = set()
            for source, listed in (("case_details", details[role]), ("cause_list", r["advocates"][role])):
                for a in listed:
                    if a not in seen:
                        seen.add(a)
                        advocate_rows.append([eid, r["case_key"], role, a, source])
    labels = read_labels()
    status_rows = [[r["case_key"], eid, r["status"], r["status_since"], *labels.get(r["case_key"], ("", ""))] for eid, r in result.items()]
    timeline_rows = []
    for eid, r in result.items():
        for h in r["hearings"]:
            p = h["purpose"]
            timeline_rows.append([eid, h["date"], h["time"] or "", h["time_source"] or "", h["list"] or "",
                                  "yes" if h["held"] else "no", ", ".join(p["tags"]), p["text"],
                                  h["outcome"], h["next_date"] or "", h["postponed_from"] or "", "", "",
                                  "\n\n".join(h["raw"]), r["case_key"]])
    storage.replace_analysis(COURT, result, status=status_rows, parties=party_rows, advocates=advocate_rows, timeline=timeline_rows)


def main():
    lists = len(storage.latest_cause_lists())
    result = build(collect())
    write_outputs(result)
    print(f"{lists} cause lists, {len(result)} cases -> database ({COURT})")
    for w in warnings:
        print("warning:", w, file=sys.stderr)


if __name__ == "__main__":
    main()
