"""Consumer commissions (national, state and district) listed on the e-Jagriti portal.

The cause-list page https://e-jagriti.gov.in/cause-list fills its "Commission" and
"CauseList For" drop-downs from a JSON API. Choosing a state there calls
getCommissionDetailsByStateId, which returns that state's state commission, circuit
benches and district commissions (one per district, plus numbered "additional" benches,
e.g. "Bangalore 2nd Additional"). The date of hearing on the page is not needed for this.
"""

import json
import re

from . import log, make_session

API_URL = "https://e-jagriti.gov.in/services/master/master/v2/getCommissionDetailsByStateId"
USER_AGENT = "Mozilla/5.0 (compatible; case-progress-crawler)"
TIMEOUT = 60

NATIONAL_COMMISSION = 1  # commissionTypeId
STATE_COMMISSION = 2
DISTRICT_COMMISSION = 3

KARNATAKA = {"state": "Karnataka", "state_id": 29}
DELHI = {"state": "Delhi", "state_id": 7}
KERALA = {"state": "Kerala", "state_id": 32}
TAMIL_NADU = {"state": "Tamil Nadu", "state_id": 33}
NATIONAL = {"state_id": 0}  # the portal lists the National Commission (NCDRC) under state id 0
NATIONAL_SEAT = DELHI  # where the National Commission sits: its courts are listed with that state's
BANGALORE_RE = re.compile(r"\b(?:bangalore|bengaluru)\b", re.IGNORECASE)  # two spellings of the same name


def fetch_commissions(state_id):
    """All commissions of a state, as the portal returns them."""
    session = make_session(USER_AGENT)
    try:
        resp = session.get(API_URL, params={"stateId": state_id}, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()["data"]
    finally:
        session.close()


def state_courts(state, state_id, state_city=None):
    """Consumer courts of a state: the State Commission, its circuit benches (which list cases of their own) and every
    district commission. `state_city` is the city of the State Commission and of any district
    commission whose name matches BANGALORE_RE for Karnataka ("Bangalore": Bangalore Urban, Bengaluru Rural,
    Bengaluru South and the additional benches); a circuit bench is in the city it is named after, and any other
    district commission in the city named after its district."""
    courts = []
    for c in fetch_commissions(state_id):
        is_state = c["commissionTypeId"] == STATE_COMMISSION and not c["circuitAdditionBenchStatus"]
        is_bench = c["commissionTypeId"] == STATE_COMMISSION and bool(c["circuitAdditionBenchStatus"])
        if not (is_state or is_bench or c["commissionTypeId"] == DISTRICT_COMMISSION):
            continue
        name = c["commissionNameEn"]
        in_state_city = is_state or (state == KARNATAKA["state"] and BANGALORE_RE.search(name))
        courts.append({
            "id": f"ejagriti-{c['commissionId']}",
            "name": f"{state} State Commission" if is_state else " ".join(name.split()),
            "kind": "consumer state commission" if is_state else "consumer circuit bench" if is_bench else "consumer district commission",
            "city": " ".join(name.split()).title().removeprefix("Circuit Bench ") if is_bench else state_city if in_state_city else re.sub(r"\b(Ii|Iii|Iv)\b", lambda m: m[0].upper(), " ".join(name.split()).title()),  # "South II", not "South Ii"
            "state": state,
            "source": API_URL + f"?stateId={state_id}",
            "ejagriti_commission_id": c["commissionId"],
            "ejagriti_case_prefix": c["casePrefixText"],
        })
    if not courts:
        log(f"ERROR: no {state} consumer courts found; has the API changed?")
    return courts


def national_courts():
    """The National Consumer Disputes Redressal Commission (NCDRC), listed with the state it sits in (Delhi)."""
    courts = []
    for c in fetch_commissions(NATIONAL["state_id"]):
        if c["commissionTypeId"] != NATIONAL_COMMISSION:
            continue
        courts.append({
            "id": f"ejagriti-{c['commissionId']}",
            "name": "National Consumer Disputes Redressal Commission (NCDRC)",
            "kind": "consumer national commission",
            "city": "New Delhi",
            "state": NATIONAL_SEAT["state"],
            "source": API_URL + f"?stateId={NATIONAL['state_id']}",
            "ejagriti_commission_id": c["commissionId"],
            "ejagriti_case_prefix": c["casePrefixText"],
        })
    if not courts:
        log("ERROR: no National Commission found; has the API changed?")
    return courts


def karnataka_courts():
    return state_courts(KARNATAKA["state"], KARNATAKA["state_id"], "Bangalore")


def delhi_courts():
    return state_courts(DELHI["state"], DELHI["state_id"], "New Delhi") + national_courts()


def kerala_courts():
    return state_courts(KERALA["state"], KERALA["state_id"], "Thiruvananthapuram")


def tamil_nadu_courts():
    return state_courts(TAMIL_NADU["state"], TAMIL_NADU["state_id"], "Chennai")


def consumer_courts():
    """Every consumer court the crawler follows: those of Karnataka, those of Delhi with the National Commission,
    and those of Kerala and Tamil Nadu."""
    return karnataka_courts() + delhi_courts() + kerala_courts() + tamil_nadu_courts()


API_BASE = "https://e-jagriti.gov.in/services"


NO_CAUSELIST_MESSAGE = "CL Data not found"  # the portal's HTTP 400 for a day with no list published


def _get(session, path, **params):
    resp = session.get(f"{API_BASE}/{path}", params=params, timeout=TIMEOUT)
    if resp.status_code == 400 and NO_CAUSELIST_MESSAGE in resp.text:
        return {"data": None}
    resp.raise_for_status()
    return resp.json()


def fetch_causelist(session, commission_id, day):
    """The cause lists of all the court numbers of one commission for `day` (a date) -- the
    portal's "All" choice, which leaves courtId out of the request -- or None when none has
    been published. Returns the portal's rows (one per court number, with a "courtId") with
    each row's "clData" (a JSON string holding the listed cases) decoded into a "cases" list."""
    body = _get(session, "courtmaster/courtRoom/v1/getAllEjclData",
                commissionId=commission_id, clDate=day.isoformat())
    rows = body.get("data") or []
    out = []
    for row in rows:
        row = dict(row)
        row["cases"] = json.loads(row.pop("clData") or "[]")
        out.append(row)
    return out or None
