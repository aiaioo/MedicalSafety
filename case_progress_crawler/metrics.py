#!/usr/bin/env python3
"""Labels for cases and hearings, stored so the numbers can be sliced any way in SQL.

Two tables (see db/schema.sql), rebuilt wholesale by `python metrics.py` (the daily crawl runs it after the analyses):
  case_metrics     one row per case: outcome (open / disposed / allowed / dismissed / withdrawn / settled / returned), which side is the hospital or doctor,
                   hospitals, years, hearing counts, days between hearings, adjournments by patient / hospital side, filing date and days from filing to close.
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

OUTCOMES = [("open", "Still open"), ("disposed", "Disposed"), ("allowed", "Allowed"), ("dismissed", "Dismissed"),
            ("withdrawn", "Withdrawn"), ("settled", "Settled"), ("returned", "Returned or transferred")]
# the e-Jagriti case stage of a closed consumer case; a bare "DISPOSED OFF" does not say who won
CONSUMER_OUTCOMES = {"DISPOSED OFF": "disposed", "ALLOWED": "allowed", "DISMISSED": "dismissed",
                     "DISMISSED IN NON-PROSECUTION": "dismissed"}

MEDICAL_COUNCILS = ("kmc", "dmc")  # analysed courts whose status is the council's own (open / disposed / dismissed / withdrawn)
ORDER_OUTCOMES = {"dismissed": "dismissed", "part": "allowed", "full": "allowed"}  # a final order's judgement class -> the outcome it counts as
REGISTER_COURTS = ("dmc",)  # courts known only from a register of complaints: its timeline rows (received, appeal decided) are not hearings, so no gaps or adjournments

PATIENT_WORDS = re.compile(r"complainant|petitioner|appellant|\bLC\b|\bCC\b", re.I)
HOSPITAL_WORDS = re.compile(r"\bO\.?\s?Ps?\b|opposite part|respondent|defendant|\bRes\b|\bR\d\b", re.I)
TIME_ASKED = re.compile(r"(pray|seek|sought|request|ask)\w*\s+(for\s+)?(some\s+|further\s+|last\s+|more\s+)?(time|adjournment)|\bprays?\s+time", re.I)
ABSENT = re.compile(r"\babsent\b|not present|no representation|remained absent|called out.{0,20}none", re.I)


SETTLED = re.compile(r"settle|compromise|fully satisfied|memo of satisfaction", re.I)
WITHDRAWN = re.compile(r"withdr[ae]w|withdrawn", re.I)
ALLOWED = re.compile(r"^\W*allowed\b|\b(partly|part)\b[^.]{0,15}\ballowed\b|\ballowed\b[^.]{0,10}\b(partly|in part)\b|\b(complaint|appeal|petition|revision|application|case)\b[^.]{0,60}\ballowed\b", re.I)
DISMISSED = re.compile(r"dismiss", re.I)
RETURNED = re.compile(r"\breturn(ed)?\b.{0,40}\bcomplain|tr[a-z]{0,3}sferred to|\btransfer(red)? (the )?(case|complaint)|\bjurisdiction", re.I)


def disposal_outcome(text):
    """How a case the portal calls "DISPOSED OFF" ended, from the text of the hearing that disposed of it:
    settled / withdrawn / allowed / dismissed / returned, else 'disposed' (the text does not say). Settlement and
    withdrawal come first ("settled out of court and withdrawn", "dismissed as withdrawn"); a partly allowed
    complaint counts as allowed."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]*>|&nbsp;", " ", text or ""))
    for outcome, rx in (("settled", SETTLED), ("withdrawn", WITHDRAWN), ("allowed", ALLOWED), ("dismissed", DISMISSED),
                        ("returned", RETURNED)):
        if rx.search(text):
            return outcome
    return "disposed"


def outcome_of(row, status_doc):
    """open / disposed / allowed / dismissed / withdrawn / settled / returned: the medical council's own status (the
    KMC's or the DMC's), or for a consumer case the portal's case stage; a bare "DISPOSED OFF" is refined from the
    disposing hearing's text."""
    if row["court"] in MEDICAL_COUNCILS:
        if row["court"] in REGISTER_COURTS:  # the DMC's register says only "disposed": its final order, read, says which way it went
            from judgements import judgement_of  # here, not at the top: judgements.py is loaded by the viewer, which imports this module
            j = judgement_of(row["case_key"])
            if j and j["class"] in ORDER_OUTCOMES:
                return ORDER_OUTCOMES[j["class"]]
        return row["status"] if row["status"] in ("disposed", "dismissed", "withdrawn") else "open"
    stage = ((status_doc or {}).get("caseStage") or "").strip().upper()
    outcome = CONSUMER_OUTCOMES.get(stage, "open")
    if outcome == "disposed":
        final = [h for h in (status_doc or {}).get("caseHearingDetails") or []
                 if (h.get("caseStage") or "").strip().upper() == stage and h.get("proceedingText")]
        if final:
            outcome = disposal_outcome(max(final, key=lambda h: h.get("dateOfHearing") or "")["proceedingText"])
    return outcome


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


def days_to_close(filing_date, last_hearing, outcome):
    """Days from filing to the last hearing for a closed case (the last hearing stands in for the decision date),
    or None for an open case, a missing date, or dates that do not run forwards."""
    if outcome == "open" or not filing_date or not last_hearing:
        return None
    try:
        days = (datetime.date.fromisoformat(last_hearing) - datetime.date.fromisoformat(filing_date)).days
    except ValueError:
        return None
    return days if days >= 0 else None


def _add_rows(c, today, case_rows, hearing_rows):
    """Append one case's row, and its hearings' rows, to the two tables' rows."""
    labels = hearing_labels(c["hearings"], c["provider_side"], today)
    gaps = [l["gap_days"] for l in labels if l["gap_days"] is not None]
    counts = c["adjournments"]
    held = [l for l in labels if l["held"]]
    case_rows.append([
        c["court"], c["case_key"], c["enquiry_id"], c["year"], c["kind"], c["outcome"], c["provider_side"],
        c.get("label", ""), "; ".join(sorted(c["hospitals"].values())), len(labels), len(held),
        min((l["date"] for l in held), default=None), max((l["date"] for l in held), default=None),
        len(gaps), int(statistics.median(gaps)) if gaps else None, max(gaps, default=None),
        counts[("patient", "time")], counts[("hospital", "time")], counts[("patient", "absent")], counts[("hospital", "absent")],
        c["filing_date"] or None, c["days_to_close"], gaps])
    for h, l in zip(c["hearings"], labels):
        hearing_rows.append([c["court"], c["case_key"], l["date"] or None, l["held"], l["gap_days"],
                             l["time_asked_by"], l["absent_side"], c["provider_side"], bool(h.get("from_history"))])


def rebuild():
    """Recompute both tables for every analysed court, from the viewer's merged view of each case (cause lists plus
    the portal's history), so the numbers stored are the numbers the viewer shows. The viewer uses these rows (outcome,
    gaps, adjournments) to load faster, up to a few days old, so this runs after each crawl's analyses."""
    import viewer  # imported here: the viewer imports this module for the classifiers
    today = datetime.date.today().isoformat()
    case_rows, hearing_rows, courts = [], [], []
    viewer.USE_STORED_METRICS = False  # work every figure out afresh, never from the rows being replaced
    viewer.KEEP_TEXTS = True  # hearing_labels() reads each hearing's record
    try:
        for state in storage.analysis_states():  # one state's cases in memory at a time
            with viewer.in_state(state):
                courts += viewer.courts_of(state)
                ordered = viewer.build_state(state)["ordered"]
                for c in ordered:
                    _add_rows({**c, "hearings": []} if c["court"] in REGISTER_COURTS else c, today, case_rows, hearing_rows)
    finally:
        viewer.USE_STORED_METRICS = True
        viewer.KEEP_TEXTS = False
    storage.replace_metrics(case_rows, hearing_rows, courts)
    return len(case_rows), len(hearing_rows)


if __name__ == "__main__":
    cases, hearings = rebuild()
    print(f"{cases} cases, {hearings} hearings -> case_metrics, hearing_metrics")
