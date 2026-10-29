#!/usr/bin/env python3
"""Download new case-progress PDFs from each court's website.

Every court (see courts/) has its own Court subclass that knows how that court
publishes its lists; this script just runs them. Downloads go into pdfs/<court>/ and
what has been fetched is remembered in storage/<court>/state.json.

Usage:  python downloader.py [--court ID ...] [--dry-run]
        (no --court: every court)
"""

import argparse
import sys

from courts import all_courts, log


def main():
    courts = all_courts()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--court", action="append", choices=sorted(courts),
                        help="court to download (repeatable); default: all")
    parser.add_argument("--dry-run", action="store_true", help="list new PDFs without downloading")
    args = parser.parse_args()

    failures = 0
    for court_id in args.court or sorted(courts):
        court = courts[court_id]
        log(f"== {court.name} ({court.id})")
        try:
            failures += court.download(args.dry_run)
        except Exception as e:
            failures += 1
            log(f"ERROR {court.id}: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
