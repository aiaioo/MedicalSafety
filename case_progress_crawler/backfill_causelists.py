#!/usr/bin/env python3
"""Fetch consumer-commission cause lists for past days, newest first, going back to a start date.

Usage:  python backfill_causelists.py [--from 2015-01-01] [--to 2024-12-31] [--delay 0.5]

The nightly crawl (downloader.py) only looks a day back; this fills in older history. It uses
the same code and filters as the nightly crawl (courts/consumer.py), but keeps its own record
of which days it has already asked about (crawl state "consumer-backfill"), so it can be stopped
and started again and carries on where it left off, and never overwrites the nightly crawl's state.
It does not fetch case details; the next nightly crawl does that for every case now on a list.
"""

import argparse
import sys
import time
from datetime import date, timedelta

import progress_storage as storage
from courts import ejagriti, log, make_session
from courts.consumer import EJagritiConsumer

STATE_ID = "consumer-backfill"
RETRIES = 6


def fetch(session_box, commission_id, day):
    """fetch_causelist with a pause and a fresh connection after a network error (the portal resets
    connections that come too fast)."""
    for attempt in range(RETRIES):
        try:
            return ejagriti.fetch_causelist(session_box[0], commission_id, day)
        except Exception as e:
            wait = 15 * 2 ** attempt
            log(f"  retry {attempt + 1}/{RETRIES} in {wait}s: {commission_id} {day}: {e}")
            time.sleep(wait)
            session_box[0].close()
            session_box[0] = make_session(ejagriti.USER_AGENT)
    raise RuntimeError(f"gave up on {commission_id} {day}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2015, 1, 1))
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=date(2024, 12, 31))
    p.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    args = p.parse_args()

    court = EJagritiConsumer()
    court.id = STATE_ID                      # court.save() records what it saved under this id
    commissions = court.commissions()
    state = storage.load_state(STATE_ID)
    state.setdefault("seen", {})
    seen = state["seen"]       # crawl state keeps only "seen": days asked about are recorded there as "checked/<day>"
    checked = {k.split("/", 1)[1] for k in seen if k.startswith("checked/")}
    session_box = [make_session(ejagriti.USER_AGENT)]
    failures = saved = 0
    day, asked = args.end, 0
    total = sum(1 for i in range((args.end - args.start).days + 1) if (args.start + timedelta(days=i)).weekday() < 5)
    log(f"{len(commissions)} commissions, {total} working days from {args.end} back to {args.start}")
    while day >= args.start:
        if day.weekday() < 5 and day.isoformat() not in checked:
            listed = 0
            ok = True
            for c in commissions:
                try:
                    rows = fetch(session_box, c["ejagriti_commission_id"], day)
                    if rows:
                        listed += 1
                        saved += court.save(c, day, rows, state, False)
                except Exception as e:
                    ok = False
                    failures += 1
                    log(f"  FAILED {c['name']}, {day}: {e}")
                time.sleep(args.delay)
            if ok:
                checked.add(day.isoformat())
                seen[f"checked/{day.isoformat()}"] = {"lists": listed}
            asked += 1
            if asked % 20 == 0:
                storage.save_state(STATE_ID, state)
                log(f"progress: at {day}; {len(checked)} days done, {saved} lists saved, {failures} failures")
        day -= timedelta(days=1)
    storage.save_state(STATE_ID, state)
    log(f"finished: {len(checked)} days checked, {saved} lists saved, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
