#!/usr/bin/env python3
"""For every saved case: read history.json, and download the order of each held hearing that has none on disk.

Ignores the status's dailyOrderStatus flag (asks the portal for every gap). A hearing the portal has no order
for (HTTP 400) is noted in the case's no_orders.json, so it is not asked about again for a week.

Usage:  python backfill_orders.py [--dry-run]
"""
import argparse
import glob
import json
import os
import time
from datetime import datetime

from courts import log
from courts.ejagriti_case import RECHECK_AFTER, REQUEST_DELAY, ConsumerCaseDownloader, DiskPersister, history_hearing_dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="only count the gaps")
    args = ap.parse_args()
    persister = DiskPersister()
    dl = ConsumerCaseDownloader(persister)
    now = datetime.now()
    found = missed = failed = skipped = 0
    for folder in sorted(glob.glob(os.path.join(persister.root, "*"))):
        try:
            with open(os.path.join(folder, "history.json"), encoding="utf-8") as f:
                history = json.load(f)
            with open(os.path.join(folder, "status.json"), encoding="utf-8") as f:
                status = json.load(f)
            filing, number = status["fillingReferenceNumber"], status["caseNumber"]
        except (OSError, ValueError, KeyError):
            continue
        orders = os.path.join(folder, "orders")
        have = {n.split("_")[0] for n in os.listdir(orders)} if os.path.isdir(orders) else set()
        misses_path = os.path.join(folder, "no_orders.json")
        misses = json.load(open(misses_path)) if os.path.exists(misses_path) else {}
        gaps = [d for d in history_hearing_dates(history) if d not in have]
        todo = [d for d in gaps if not (d in misses and now - datetime.fromisoformat(misses[d]) < RECHECK_AFTER)]
        skipped += len(gaps) - len(todo)
        if args.dry_run or not todo:
            missed += len(todo) if args.dry_run else 0
            continue
        key = os.path.basename(folder)
        for day in todo:
            try:
                content, ext = dl.fetch_order(filing, day, 1)
                persister.save_file(number, f"orders/{day}_daily_order.{ext}", content)
                misses.pop(day, None)
                found += 1
                log(f"  FOUND {key} {day}")
            except Exception as e:
                if isinstance(e, ValueError) or getattr(getattr(e, "response", None), "status_code", None) == 400:
                    misses[day] = now.isoformat(timespec="seconds")
                    missed += 1
                else:
                    failed += 1
                    log(f"  FAILED {key} {day}: {e}")
            time.sleep(REQUEST_DELAY)
        if misses:
            with open(misses_path, "w") as f:
                json.dump(dict(sorted(misses.items())), f, indent=1)
        log(f"{key}: done ({found} found so far, {missed} none, {failed} failed)")
    log(f"backfill: {found} orders found, {missed} {'to try' if args.dry_run else 'with no order'}, {skipped} skipped as recently checked, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
