"""Consumer commissions (state and district) listed on the e-Jagriti portal.

The cause-list page https://e-jagriti.gov.in/cause-list fills its "Commission" and
"CauseList For" drop-downs from a JSON API. Choosing a state there calls
getCommissionDetailsByStateId, which returns that state's state commission, circuit
benches and district commissions (one per district, plus numbered "additional" benches,
e.g. "Bangalore 2nd Additional"). The date of hearing on the page is not needed for this.
"""

import re

from . import log, make_session

API_URL = "https://e-jagriti.gov.in/services/master/master/v2/getCommissionDetailsByStateId"
USER_AGENT = "Mozilla/5.0 (compatible; case-progress-crawler)"
TIMEOUT = 60

STATE_COMMISSION = 2  # commissionTypeId; district commissions are 3

KARNATAKA = {"state": "Karnataka", "state_id": 29}
BANGALORE_RE = re.compile(r"\bbangalore\b", re.IGNORECASE)


def fetch_commissions(state_id):
    """All commissions of a state, as the portal returns them."""
    session = make_session(USER_AGENT)
    try:
        resp = session.get(API_URL, params={"stateId": state_id}, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()["data"]
    finally:
        session.close()


def bangalore_courts():
    """Consumer courts in Bangalore: the Karnataka State Commission and every commission
    with "Bangalore" in its name (Bangalore Urban and its additional benches)."""
    courts = []
    for c in fetch_commissions(KARNATAKA["state_id"]):
        is_state = c["commissionTypeId"] == STATE_COMMISSION and not c["circuitAdditionBenchStatus"]
        if not (is_state or BANGALORE_RE.search(c["commissionNameEn"])):
            continue
        courts.append({
            "id": f"ejagriti-{c['commissionId']}",
            "name": "Karnataka State Commission" if is_state else c["commissionNameEn"],
            "kind": "consumer state commission" if is_state else "consumer district commission",
            "city": "Bangalore",
            "state": KARNATAKA["state"],
            "source": API_URL + f"?stateId={KARNATAKA['state_id']}",
            "ejagriti_commission_id": c["commissionId"],
            "ejagriti_case_prefix": c["casePrefixText"],
        })
    if not courts:
        log("ERROR: no Bangalore consumer courts found; has the API changed?")
    return courts
