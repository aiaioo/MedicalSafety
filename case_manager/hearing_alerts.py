"""Emails the people on a case the first time the case tracker has the court's record of a hearing.

For every case with a case number the tracker knows, the tracker's hearing days that have passed and have a record (the
text kept as the hearing's "order text": the outcome or order and the next date) are compared with the days already dealt
with (storage.alert_dates_seen). A day not yet dealt with is emailed -- the record quoted, the court's PDF attached where
there is one -- to the verified users with access to the case who have not turned alerts off, then marked as dealt with.

A case's first check only records the hearings it already has (the baseline), so adding a case does not send its history.
If every address of a hearing fails (Postmark down), the hearing is left for the next round; if some got it, it is marked,
so the others are not sent it twice.

This must run inside the web app: the case numbers and addresses are encrypted, and the key is only in that process's
memory (see KEY_MANAGEMENT.md). start() runs one round every INTERVAL_SECONDS on a daemon thread, skipping a round while
the key has not been entered.
"""

from __future__ import annotations

import html
import logging
import mimetypes
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import key_manager
import mailer
import storage

log = logging.getLogger(__name__)

INTERVAL_SECONDS = int(os.environ.get("HEARING_ALERTS_INTERVAL_SECONDS", 30 * 60))
SITE_HOST = os.environ.get("HEARING_ALERTS_SITE_HOST", mailer.DEFAULT_DOMAIN)  # the site the emails go out as
IST = timezone(timedelta(hours=5, minutes=30))  # the courts' day


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def _pretty(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d.day} {d.strftime('%B %Y')}"


def _recorded(tracker_case: dict) -> dict[str, dict]:
    """{ISO date: hearing} of the tracker's hearings that have happened (today or earlier) and have a court's record."""
    today = _today()
    return {h["date"]: h for h in tracker_case["hearings"] if h["date"] <= today and (h.get("order_text") or "").strip()}


def _attachments(tracker_viewer, tracker_case: dict, date: str):
    """([(name, bytes, content type)], [names left out]) for the hearing's PDF(s) -- within Postmark's size limit."""
    files, skipped, total = [], [], 0
    try:
        found = tracker_viewer.hearing_files(tracker_case.get("case_key"), tracker_case.get("court_id"), date)
    except Exception:
        log.exception("finding the documents of %s", date)
        return [], []
    for path, name in found:
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        if total + len(data) > mailer.MAX_ATTACHMENT_BYTES:
            skipped.append(name)
            continue
        total += len(data)
        files.append((name, data, mimetypes.guess_type(name)[0] or "application/octet-stream"))
    return files, skipped


def compose(case: dict, tracker_case: dict, hearing: dict, skipped: list[str], attached: list[str]) -> tuple[str, str, str]:
    """(subject, text body, HTML body) of the alert for one hearing."""
    date = _pretty(hearing["date"])
    label = case["case_number"]
    parties = case["name"] or ""
    subject = f"Update on {label}: the hearing of {date}"
    court = tracker_case.get("court") or tracker_case.get("court_label") or ""
    record = hearing["order_text"].strip()
    link = f"{mailer.base_url(SITE_HOST)}/cases"
    notes = []
    if attached:
        notes.append("The court's document for the hearing is attached: " + ", ".join(attached) + ".")
    if skipped:
        notes.append("Too large to attach (see the case tracker): " + ", ".join(skipped) + ".")
    if not attached and not skipped:
        notes.append("The court published no document for this hearing; the text above is its record.")
    intro = f"The court's record of the hearing of {date} in {label}" + (f" ({parties})" if parties else "") + \
            (f", {court}," if court else "") + " has just appeared."
    text = (f"{intro}\n\nWhat the court recorded:\n\n" + "\n".join("> " + ln for ln in record.splitlines()) +
            "\n\n" + "\n".join(notes) + f"\n\nOpen your cases: {link}\n\n"
            "You get this because you have access to this case. To stop these emails, untick the alerts box on your home page.\n")
    esc = html.escape
    body = (
        '<div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; font-size: 15px; color: #222; max-width: 560px;">'
        f"<p>{esc(intro)}</p><p style=\"margin-bottom: 4px;\"><strong>What the court recorded</strong></p>"
        '<blockquote style="margin: 0 0 12px; padding: 4px 14px; border-left: 3px solid #2a5bd7; background: #f4f6fb;">'
        + "".join(f"<p style=\"margin: 8px 0;\">{esc(ln)}</p>" for ln in record.splitlines() if ln.strip()) +
        "</blockquote>" + "".join(f"<p>{esc(n)}</p>" for n in notes) +
        f'<p><a href="{esc(link, quote=True)}">Open your cases</a></p>'
        '<p style="font-size: 13px; color: #555;">You get this because you have access to this case. '
        "To stop these emails, untick the alerts box on your home page.</p></div>")
    return subject, text, body


def check_case(tracker_viewer, case: dict) -> int:
    """One case: baseline it if new, else email its newly recorded hearings. Returns how many hearings were announced."""
    tracker_case = tracker_viewer.case_prefill(case["case_number"], with_court=True)
    if tracker_case is None:
        return 0
    recorded = _recorded(tracker_case)
    seen = storage.alert_dates_seen(case["id"])
    if seen is None:
        storage.take_alert_baseline(case["id"], sorted(recorded))
        return 0
    announced = 0
    for date in sorted(set(recorded) - seen):
        recipients = storage.alert_recipients(case["id"])
        if recipients:
            files, skipped = _attachments(tracker_viewer, tracker_case, date)
            subject, text, body = compose(case, tracker_case, recorded[date], skipped, [f[0] for f in files])
            sent = [mailer.send_email_now(SITE_HOST, to, subject, text, body, files) for to in recipients]
            if not any(sent):
                log.error("hearing alert for case %s, %s: not delivered; will retry", case["id"], date)
                continue
        storage.mark_hearing_alerted(case["id"], date)  # no one to tell: still dealt with
        announced += 1
    return announced


def run_once(tracker_viewer) -> int:
    n = 0
    for case in storage.cases_with_numbers():
        try:
            n += check_case(tracker_viewer, case)
        except Exception:
            log.exception("hearing alerts for case %s", case["id"])
    return n


def start(get_tracker_viewer) -> None:
    """Starts the background rounds. Does nothing without POSTMARK_TOKEN (development), or if
    HEARING_ALERTS=0. `get_tracker_viewer` returns the crawler's viewer module (None if not mounted)."""
    if os.environ.get("HEARING_ALERTS", "1") == "0" or not os.environ.get("POSTMARK_TOKEN"):
        return

    def loop():
        while True:
            time.sleep(INTERVAL_SECONDS if key_manager.KeyManager().is_key_in_memory() else 60)
            try:
                viewer = get_tracker_viewer()
                if viewer is not None and key_manager.KeyManager().is_key_in_memory():
                    log.info("hearing alerts: %d announced", run_once(viewer))
            except Exception:
                log.exception("hearing alerts round")

    threading.Thread(target=loop, name="hearing-alerts", daemon=True).start()
