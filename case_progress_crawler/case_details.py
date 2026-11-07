#!/usr/bin/env python3
"""Download the status, hearing history and order PDFs of consumer cases from e-Jagriti.

Files go to case_details/consumer/<case key>/ (status.json, history.json, orders/<date>_daily_order.pdf or
_judgement.pdf, or .html for older orders); orders already saved are not fetched again.

Usage:  python case_details.py DC/525/CC/763/2026 [CASE_NUMBER ...]
        python case_details.py --tracked          every case in storage/consumer/status.csv
"""

import argparse
import csv
import os
import sys

from courts import BASE_DIR, log
from courts.ejagriti_case import ConsumerCaseDownloader, DiskPersister


def tracked_case_numbers():
    """Every consumer case the analysis knows of (storage/consumer/status.csv)."""
    with open(os.path.join(BASE_DIR, "storage", "consumer", "status.csv"), newline="", encoding="utf-8") as f:
        return sorted({row["enquiry_id"] for row in csv.DictReader(f)})


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("case_numbers", nargs="*", metavar="CASE_NUMBER", help="e.g. DC/525/CC/763/2026")
    parser.add_argument("--tracked", action="store_true", help="all cases in storage/consumer/status.csv")
    args = parser.parse_args()
    numbers = args.case_numbers + (tracked_case_numbers() if args.tracked else [])
    if not numbers:
        parser.error("give case numbers or --tracked")

    downloader = ConsumerCaseDownloader(DiskPersister())
    failures = 0
    for number in numbers:
        log(f"== {number}")
        try:
            result = downloader.download(number)
        except Exception as e:
            failures += 1
            log(f"  FAILED: {e}")
            continue
        failures += bool(result["failed"])
        log(f"  saved {len(result['saved'])}, already had {len(result['skipped'])}, failed {len(result['failed'])}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
