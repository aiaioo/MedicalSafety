#!/usr/bin/env python3
"""Labels for cases and hearings, stored so the numbers can be sliced any way in SQL.

Two tables (see db/schema.sql), rebuilt wholesale by `python metrics.py` (the daily crawl runs it after the analyses):
  case_metrics     one row per case: outcome (open / disposed / allowed / dismissed), which side is the hospital or doctor,
                   hospitals, years, hearing counts, days between hearings, adjournments by patient / hospital side.
  hearing_metrics  one row per hearing: the days since the previous hearing day, and which side asked for time or was
                   absent, as far as the court's recorded text says (never guessed: '' when it does not say).

"patient" is the side that is not the hospital/doctor; the label is only given where the provider's side is known.
Join to the analysis tables on (court, case_key), e.g.
  SELECT year, outcome, count(*) FROM case_metrics GROUP BY 1, 2;
  SELECT hospitals, avg(median_gap_days) FROM case_metrics GROUP BY 1;
"""

import datetime
import re
import statistics
from collections import Counter

import progress_storage as storage

OUTCOMES = [("open", "Still open"), ("disposed", "Disposed"), ("allowed", "Allowed"), ("dismissed", "Dismissed")]
# the e-Jagriti case stage of a closed consumer case; a bare "DISPOSED OFF" does not say who won
CONSUMER_OUTCOMES = {"DISPOSED OFF": "disposed", "ALLOWED": "allowed", "DISMISSED": "dismissed",
                     "DISMISSED IN NON-PROSECUTION": "dismissed"}

PATIENT_WORDS = re.compile(r"complainant|petitioner|appellant|\bLC\b|\bCC\b", re.I)
HOSPITAL_WORDS = re.compile(r"\bO\.?\s?Ps?\b|opposite part|respondent|defendant|\bRes\b|\bR\d\b", re.I)
TIME_ASKED = re.compile(r"(pray|seek|sought|request|ask)\w*\s+(for\s+)?(some\s+|further\s+|last\s+|more\s+)?(time|adjournment)|\bprays?\s+time", re.I)
ABSENT = re.compile(r"\babsent\b|not present|no representation|remained absent|called out.{0,20}none", re.I)


def outcome_of(row, status_doc):
    """open / disposed / allowed / dismissed: the KMC's own status, or the portal's case stage for a consumer case."""
    if row["court"] == "kmc":
        return row["status"] if row["status"] in ("disposed", "dismissed") else "open"
    return CONSUMER_OUTCOMES.get(((status_doc or {}).get("caseStage") or "").strip().upper(), "open")


def adjournment_sides(text):
    """Which side(s) ('complainant', 'respondent') asked for time or were absent, by clauses of a hearing's
    recorded text: {"time": set, "absent": set}. A clause naming both sides, or neither, is not attributed."""
    found = {"time": set(), "absent": set()}
    for clause in re.split(r"[.;\n]|\s[-–]\s", text or ""):
        kind = "time" if TIME_ASKED.search(clause) else "absent" if ABSENT.search(clause) else None
        if not kind:
            continue
        sides = {side for side, rx in (("complainant", PATIENT_WORDS), ("respondent", HOSPITAL_WORDS)) if rx.search(clause)}
        if len(sides) == 1:
            found[kind] |= sides
    return found


def _who(sides, patient_side):
    """'patient', 'hospital', 'both' or '' for a set of sides."""
    who = {"patient" if s == patient_side else "hospital" for s in sides}
    return "both" if len(who) == 2 else next(iter(who), "")


def hearing_labels(hearings, provider_side, today):
    """One dict per hearing, in the order given: date, held (on or before today), gap_days (since the previous
    hearing day; None for the first or a later hearing of the same day), time_asked_by and absent_side
    ('patient' / 'hospital' / 'both' / '', only where provider_side is known)."""
    held_days = sorted({h["hearing_date"] for h in hearings if h["hearing_date"] and h["hearing_date"] <= today})
    previous = dict(zip(held_days[1:], held_days[:-1]))
    patient_side = {"complainant": "respondent", "respondent": "complainant"}.get(provider_side)
    out, seen = [], set()
    for h in hearings:
        d = h["hearing_date"]
        held = bool(d) and d <= today
        gap = None
        if held and d in previous and d not in seen:
            gap = (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(previous[d])).days
        seen.add(d)
        sides = {"time": set(), "absent": set()}
        if held and patient_side:
            sides = adjournment_sides((h.get("record") or "") + " " + (h.get("outcome_text") or ""))
        out.append({"date": d, "held": held, "gap_days": gap,
                    "time_asked_by": _who(sides["time"], patient_side), "absent_side": _who(sides["absent"], patient_side)})
    return out


def case_pace(hearings, provider_side, today):
    """(days between consecutive hearing days up to today, {(who, kind): hearings}) for one case."""
    labels = hearing_labels(hearings, provider_side, today)
    gaps = [l["gap_days"] for l in labels if l["gap_days"] is not None]
    counts = Counter()
    for l in labels:
        for kind, who in (("time", l["time_asked_by"]), ("absent", l["absent_side"])):
            for w in ("patient", "hospital") if who == "both" else (who,):
                if w:
                    counts[(w, kind)] += 1
    return gaps, counts


def rebuild():
    """Recompute both tables for every analysed court, from the viewer's merged view of each case (cause lists plus
    the portal's history), so the numbers stored are the numbers the viewer shows."""
    import viewer  # imported here: the viewer imports this module for the classifiers
    data = viewer.load("all")
    today = datetime.date.today().isoformat()
    case_rows, hearing_rows = [], []
    for c in data["ordered"]:
        labels = hearing_labels(c["hearings"], c["provider_side"], today)
        gaps = [l["gap_days"] for l in labels if l["gap_days"] is not None]
        counts = c["adjournments"]
        held = [l for l in labels if l["held"]]
        case_rows.append([
            c["court"], c["case_key"], c["enquiry_id"], c["year"], c["kind"], c["outcome"], c["provider_side"],
            c.get("label", ""), "; ".join(sorted(c["hospitals"].values())), len(labels), len(held),
            min((l["date"] for l in held), default=None), max((l["date"] for l in held), default=None),
            len(gaps), int(statistics.median(gaps)) if gaps else None, max(gaps, default=None),
            counts[("patient", "time")], counts[("hospital", "time")], counts[("patient", "absent")], counts[("hospital", "absent")]])
        for h, l in zip(c["hearings"], labels):
            hearing_rows.append([c["court"], c["case_key"], l["date"] or None, l["held"], l["gap_days"],
                                 l["time_asked_by"], l["absent_side"], c["provider_side"], bool(h.get("from_history"))])
    storage.replace_metrics(case_rows, hearing_rows)
    return len(case_rows), len(hearing_rows)


if __name__ == "__main__":
    cases, hearings = rebuild()
    print(f"{cases} cases, {hearings} hearings -> case_metrics, hearing_metrics")
