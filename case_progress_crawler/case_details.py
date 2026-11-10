#!/usr/bin/env python3
"""Download the status, hearing history and order PDFs of consumer cases from e-Jagriti.

The status, history and other documents go to the database, the orders to case_details/consumer/<case key>/orders/
(<date>_daily_order.pdf or _judgement.pdf, or .html for older orders), all through storage.py; orders already saved are not fetched again, and a re-issued order is
saved as a new version (_v2) beside the old one. The daily cron run (downloader.py) does this for every tracked case.

Usage:  python case_details.py DC/525/CC/763/2026 [CASE_NUMBER ...]
        python case_details.py --tracked          every case seen on a saved cause list
"""

import argparse
import sys

from courts import log
from courts.ejagriti_case import ConsumerCaseDownloader, StoragePersister, tracked_cases


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("case_numbers", nargs="*", metavar="CASE_NUMBER", help="e.g. DC/525/CC/763/2026")
    parser.add_argument("--tracked", action="store_true", help="all cases seen on a saved cause list")
    args = parser.parse_args()
    known = tracked_cases()  # the parties of a case, for labelling it; a case not on any saved list is not labelled
    numbers = args.case_numbers + (sorted(known) if args.tracked else [])
    if not numbers:
        parser.error("give case numbers or --tracked")

    downloader = ConsumerCaseDownloader(StoragePersister())
    failures = 0
    for number in numbers:
        log(f"== {number}")
        try:
            result = downloader.sync(number, parties=known.get(number))
        except Exception as e:
            failures += 1
            log(f"  FAILED: {e}")
            continue
        failures += bool(result["failed"])
        log(f"  saved {len(result['saved'])}, already had {len(result['skipped'])}, failed {len(result['failed'])}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
