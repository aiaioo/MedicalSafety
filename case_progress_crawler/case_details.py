#!/usr/bin/env python3
"""Download the status, hearing history and order PDFs of consumer cases from e-Jagriti.

The portal asks for a captcha for every case, so this stops and asks you to read it: the image is saved
(and opened on a Mac) and you type what it says. Enter on its own skips that case.

Files go to case_details/consumer/<case key>/ (status.json, history.json, orders/<date>_daily_order.pdf or
_judgement.pdf); orders already saved are not fetched again.

Usage:  python case_details.py DC/525/CC/763/2026 [CASE_NUMBER ...]
"""

import argparse
import sys

from courts import log
from courts.ejagriti_case import CaptchaAbandoned, ConsoleCaptchaPrompt, ConsumerCaseDetailsDownloader, DiskPersister


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("case_numbers", nargs="+", metavar="CASE_NUMBER", help="e.g. DC/525/CC/763/2026")
    args = parser.parse_args()

    downloader = ConsumerCaseDetailsDownloader(DiskPersister(), ConsoleCaptchaPrompt())
    failures = 0
    for number in args.case_numbers:
        log(f"== {number}")
        try:
            result = downloader.download(number)
        except CaptchaAbandoned as e:
            failures += 1
            log(f"  skipped: {e}")
            continue
        except Exception as e:
            failures += 1
            log(f"  FAILED: {e}")
            continue
        failures += bool(result["failed"])
        log(f"  saved {len(result['saved'])}, already had {len(result['skipped'])}, failed {len(result['failed'])}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
