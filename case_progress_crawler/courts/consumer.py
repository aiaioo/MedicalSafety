"""Consumer commissions (e-Jagriti): download the cause lists of the courts in court_list.py.

For every active consumer court in storage/courts.json (the state commission and the district
commissions of Bangalore; run court_list.py to refresh it), the day's cause lists of all its
court numbers (the portal's "All") are fetched from the e-Jagriti API (courts/ejagriti.py)
for each working day from yesterday to LOOKAHEAD_DAYS ahead, and saved as one JSON file per
commission per day in causelists/consumer/<commission id>/.

Only medical cases are kept (courts/medical.py: a respondent that is a hospital, clinic,
pharmacy, diagnostic centre or lab, pharma firm or doctor); the rest of each list is dropped and
a list with no medical case is not saved at all.

Unlike the KMC's PDFs these are structured: each file holds the cases listed (case number,
parties, advocates, bench, stage, previous hearing date). Cause lists are revised during
the day, so a list whose update time has changed since it was saved is saved again as
<date>_2.json, _3 ... and the earlier versions are kept. What has been saved is
recorded in storage/consumer/state.json; an empty (unpublished) list is not recorded, so it is
asked for again on the next run.
"""

import json
import os
import time
from datetime import date, datetime, timedelta

import storage

from . import BASE_DIR, Court, log, make_session
from . import ejagriti
from .medical import FILTER_VERSION, keep_medical

LOOKBACK_DAYS = 1   # yesterday's list may have been revised after the last run
LOOKAHEAD_DAYS = 7
REQUEST_DELAY = 0.5  # seconds between requests, to go easy on the portal


def working_days(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


class EJagritiConsumer(Court):
    id = "consumer"
    name = "Consumer commissions (e-Jagriti)"
    in_court_list = False  # its courts are the ones in court_list.py, not this crawler itself

    @property
    def data_dir(self):
        return os.path.join(BASE_DIR, "causelists", self.id)

    def commissions(self):
        """The active consumer courts from court_list.py's courts.json; if that has not been
        made yet, read them from the portal."""
        courts = [c for c in storage.load_courts().values()
                  if c.get("active") and c.get("ejagriti_commission_id")]
        if not courts:
            log("No consumer courts in storage/courts.json (run court_list.py); reading them from e-Jagriti")
            courts = [{**c, "active": True} for c in ejagriti.bangalore_courts()]
        return courts

    def save(self, commission, day, rows, state, dry_run):
        """Save the medical cases of the list if its update time is new. Returns True when it was (or would be) saved."""
        updated_at = max(r.get("updatedAt") or "" for r in rows)  # of the whole list, whatever the filter keeps
        rows, listed = keep_medical(rows)
        if not rows:
            return False
        key = f"{commission['ejagriti_commission_id']}/{day.isoformat()}"
        prev = state["seen"].get(key)
        refilter = bool(prev) and prev.get("filter") != FILTER_VERSION
        if prev and not refilter and prev["updated_at"] == updated_at:
            return False
        # a list saved under older filter rules is rewritten in place; a revised list gets a new version
        version = prev["version"] if refilter else prev["version"] + 1 if prev else 1
        suffix = "" if version == 1 else f"_{version}"
        dest = os.path.join(self.data_dir, str(commission["ejagriti_commission_id"]),
                            f"{day.isoformat()}{suffix}.json")
        cases = sum(len(r["cases"]) for r in rows)
        log(f"New: {commission['name']}, {day}: {cases} medical cases of {listed} listed"
            f"{' (filtered again)' if refilter else '' if version == 1 else f' (revision {version})'}")
        if dry_run:
            return True
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"commission": commission["name"], "ejagriti_commission_id": commission["ejagriti_commission_id"],
                       "date": day.isoformat(), "cases_listed": listed,
                       "fetched_at": datetime.now().isoformat(timespec="seconds"), "causelists": rows},
                      f, indent=1, ensure_ascii=False)
        os.replace(tmp, dest)
        state["seen"][key] = {"saved_to": os.path.relpath(dest, BASE_DIR), "updated_at": updated_at,
                              "is_final": all(r.get("isFinal") for r in rows), "version": version, "filter": FILTER_VERSION,
                              "downloaded_at": datetime.now().isoformat(timespec="seconds")}
        storage.save_state(self.id, state)
        return True

    def download(self, dry_run=False, back_days=None):
        session = make_session(ejagriti.USER_AGENT)
        failures = saved = 0
        try:
            commissions = self.commissions()
            if not commissions:
                log("ERROR: no consumer commissions found; has the API changed?")
                return 1
            today = date.today()
            days = list(working_days(today - timedelta(days=LOOKBACK_DAYS if back_days is None else back_days), today + timedelta(days=LOOKAHEAD_DAYS)))
            log(f"{len(commissions)} commissions x {len(days)} days")
            state = storage.load_state(self.id)
            for commission in commissions:
                for day in days:
                    try:
                        rows = ejagriti.fetch_causelist(session, commission["ejagriti_commission_id"], day)
                        if rows:
                            saved += self.save(commission, day, rows, state, dry_run)
                    except Exception as e:
                        failures += 1
                        log(f"  FAILED {commission['name']}, {day}: {e}")
                    time.sleep(REQUEST_DELAY)
            log(f"{saved} cause lists {'would be ' if dry_run else ''}saved")
            return failures
        finally:
            session.close()
