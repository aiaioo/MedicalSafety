"""Consumer commissions (e-Jagriti): download the cause lists of the courts in court_list.py.

For every active consumer court in storage/courts.json (the state commission and the district
commissions of Bangalore; run court_list.py to refresh it) and every court number, the day's
cause list is fetched from the e-Jagriti API (courts/ejagriti.py) for each working day from
yesterday to LOOKAHEAD_DAYS ahead, and saved as JSON in causelists/consumer/<commission id>/.

Unlike the KMC's PDFs these are structured: each file holds the cases listed (case number,
parties, advocates, bench, stage, previous hearing date). Cause lists are revised during
the day, so a list whose update time has changed since it was saved is saved again as
<date>_court_<n>_2.json, _3 ... and the earlier versions are kept. What has been saved is
recorded in storage/consumer/state.json; an empty (unpublished) list is not recorded, so it is
asked for again on the next run.
"""

import json
import os
import re
import time
from datetime import date, datetime, timedelta

import storage

from . import BASE_DIR, Court, log, make_session
from . import ejagriti

LOOKBACK_DAYS = 1   # yesterday's list may have been revised after the last run
LOOKAHEAD_DAYS = 7
REQUEST_DELAY = 0.5  # seconds between requests, to go easy on the portal


def working_days(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


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

    def save(self, commission, court_name, day, rows, state, dry_run):
        """Save the list if its update time is new. Returns True when it was (or would be) saved."""
        key = f"{commission['ejagriti_commission_id']}/{slug(court_name)}/{day.isoformat()}"
        updated_at = max(r.get("updatedAt") or "" for r in rows)
        prev = state["seen"].get(key)
        if prev and prev["updated_at"] == updated_at:
            return False
        version = prev["version"] + 1 if prev else 1
        suffix = "" if version == 1 else f"_{version}"
        dest = os.path.join(self.data_dir, str(commission["ejagriti_commission_id"]),
                            f"{day.isoformat()}_court_{slug(court_name)}{suffix}.json")
        cases = sum(len(r["cases"]) for r in rows)
        log(f"New: {commission['name']}, court {court_name}, {day}: {cases} cases"
            f"{'' if version == 1 else f' (revision {version})'}")
        if dry_run:
            return True
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"commission": commission["name"], "ejagriti_commission_id": commission["ejagriti_commission_id"],
                       "court": court_name, "date": day.isoformat(),
                       "fetched_at": datetime.now().isoformat(timespec="seconds"), "causelists": rows},
                      f, indent=1, ensure_ascii=False)
        os.replace(tmp, dest)
        state["seen"][key] = {"saved_to": os.path.relpath(dest, BASE_DIR), "updated_at": updated_at,
                              "is_final": all(r.get("isFinal") for r in rows), "version": version,
                              "downloaded_at": datetime.now().isoformat(timespec="seconds")}
        storage.save_state(self.id, state)
        return True

    def download(self, dry_run=False):
        session = make_session(ejagriti.USER_AGENT)
        failures = saved = 0
        try:
            commissions = self.commissions()
            courts = ejagriti.fetch_court_numbers(session)
            if not commissions or not courts:
                log("ERROR: no consumer commissions or court numbers found; has the API changed?")
                return 1
            today = date.today()
            days = list(working_days(today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=LOOKAHEAD_DAYS)))
            log(f"{len(commissions)} commissions x {len(courts)} court numbers x {len(days)} days")
            state = storage.load_state(self.id)
            for commission in commissions:
                for court_id, court_name in courts:
                    for day in days:
                        try:
                            rows = ejagriti.fetch_causelist(session, commission["ejagriti_commission_id"], court_id, day)
                            if rows:
                                saved += self.save(commission, court_name, day, rows, state, dry_run)
                        except Exception as e:
                            failures += 1
                            log(f"  FAILED {commission['name']}, court {court_name}, {day}: {e}")
                        time.sleep(REQUEST_DELAY)
            log(f"{saved} cause lists {'would be ' if dry_run else ''}saved")
            return failures
        finally:
            session.close()
