#!/usr/bin/env python3
"""Fetch consumer-commission cause lists for past days, newest first, going back to a start date.

Usage:  python backfill_causelists.py [--from 2000-01-01] [--to <yesterday>] [--delay 0.5] [--only COMMISSION_ID ...]

The nightly crawl (downloader.py) only looks a day back; this fills in older history. It uses
the same code and filters as the nightly crawl (courts/consumer.py), but keeps its own record
of which days it has already asked about, per commission (crawl state "consumer-backfill": for each
commission the range of days done, "range/<commission id>"), so it can be stopped and started again and
carries on where it left off, and never overwrites the nightly crawl's state. A commission newly added to
the court list is crawled over the whole range; the Bangalore commissions, which an earlier run covered
for 2015-01-01 to 2024-12-31 (recorded as "checked/<day>"), are only taken back from there to --from.
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


def load_ranges(seen, commissions):
    """{commission id: [low, high]} of the days already asked about, as dates (None, None when none). The
    commissions of the first, Bangalore-only version of this script have their days recorded as "checked/<day>"."""
    legacy = sorted(date.fromisoformat(k.split("/", 1)[1]) for k in seen if k.startswith("checked/"))
    ranges = {}
    for c in commissions:
        cid = c["ejagriti_commission_id"]
        r = seen.get(f"range/{cid}")
        if r:
            ranges[cid] = [date.fromisoformat(r["low"]), date.fromisoformat(r["high"])]
        elif legacy and c["city"] == "Bangalore":
            ranges[cid] = [legacy[0], legacy[-1]]
        else:
            ranges[cid] = [None, None]
    return ranges


def days_to_do(rng, start, end):
    """Working days between start and end not in rng = [low, high], newest first from where the range ends: the
    days above it, oldest first so the range stays unbroken, then those below it, newest first."""
    low, high = rng
    if low is None:
        yield from (d for d in (end - timedelta(days=i) for i in range((end - start).days + 1)) if d.weekday() < 5)
        return
    d = high + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)
    d = low - timedelta(days=1)
    while d >= start:
        if d.weekday() < 5:
            yield d
        d -= timedelta(days=1)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2000, 1, 1))
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    p.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    p.add_argument("--only", type=int, nargs="+", help="only these e-Jagriti commission ids")
    args = p.parse_args()

    court = EJagritiConsumer()
    court.id = STATE_ID                      # court.save() records what it saved under this id
    commissions = [c for c in court.commissions() if not args.only or c["ejagriti_commission_id"] in args.only]
    state = storage.load_state(STATE_ID)
    seen = state["seen"]
    ranges = load_ranges(seen, commissions)
    session_box = [make_session(ejagriti.USER_AGENT)]
    failures = saved = 0
    log(f"{len(commissions)} commissions, back to {args.start}, forward to {args.end}")
    for c in commissions:
        cid = c["ejagriti_commission_id"]
        rng = ranges[cid]
        todo = list(days_to_do(rng, args.start, args.end))
        log(f"{c['name']} ({cid}): {len(todo)} working days to ask about")
        asked = 0
        for day in todo:
            try:
                rows = fetch(session_box, cid, day)
                if rows:
                    saved += court.save(c, day, rows, state, False)
            except Exception as e:      # leave this commission where it is; the next run resumes here
                failures += 1
                log(f"  FAILED {c['name']}, {day}: {e}")
                break
            if rng[0] is None:
                rng[0] = rng[1] = day
            else:
                rng[0], rng[1] = min(rng[0], day), max(rng[1], day)
            seen[f"range/{cid}"] = {"low": rng[0].isoformat(), "high": rng[1].isoformat()}
            asked += 1
            if asked % 50 == 0:
                storage.save_state(STATE_ID, state)
                log(f"  progress: {c['name']} at {day}; {saved} lists saved so far, {failures} failures")
            time.sleep(args.delay)
        storage.save_state(STATE_ID, state)
    log(f"finished: {saved} lists saved, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
