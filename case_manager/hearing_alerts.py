"""Emails the people on a case the first time the case tracker has the court's record of a hearing, and reminds them of its
coming hearings: two weeks before, two days before and on the morning (the round from 4 am IST on that day; see send_reminders).

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

INTERVAL_SECONDS = int(os.environ.get("HEARING_ALERTS_INTERVAL_SECONDS", 15 * 60))
REMINDER_HOUR_IST = 4  # reminders go out at the first round on or after 4 am IST on their day
REMINDERS = {14: "in two weeks", 2: "in two days", 0: "today"}  # days before the hearing -> how the email says it
SITE_HOST = os.environ.get("HEARING_ALERTS_SITE_HOST", "medicalsafety.in")  # the site the emails go out as
IST = timezone(timedelta(hours=5, minutes=30))  # the courts' day


WARNING = ("Do not rely on this alert. Changes to the courts' websites can stop the tracker from picking up a hearing or its date, "
           "so always keep track of your proceeding dates yourself.")


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
            "\n\n" + "\n".join(notes) + f"\n\nOpen your cases: {link}\n\n{WARNING}\n\n"
            "You get this because you have access to this case. To stop these emails, untick the alerts box on your home page.\n")
    esc = html.escape
    body = (
        '<div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; font-size: 15px; color: #222; max-width: 560px;">'
        f"<p>{esc(intro)}</p><p style=\"margin-bottom: 4px;\"><strong>What the court recorded</strong></p>"
        '<blockquote style="margin: 0 0 12px; padding: 4px 14px; border-left: 3px solid #2a5bd7; background: #f4f6fb;">'
        + "".join(f"<p style=\"margin: 8px 0;\">{esc(ln)}</p>" for ln in record.splitlines() if ln.strip()) +
        "</blockquote>" + "".join(f"<p>{esc(n)}</p>" for n in notes) +
        f'<p><a href="{esc(link, quote=True)}">Open your cases</a></p>'
        f'<p style="font-size: 14px; border: 1px solid #c9a227; background: #fff8e1; padding: 8px 12px;"><strong>{esc(WARNING)}</strong></p>'
        '<p style="font-size: 13px; color: #555;">You get this because you have access to this case. '
        "To stop these emails, untick the alerts box on your home page.</p></div>")
    return subject, text, body


def _upcoming(tracker_case: dict) -> dict[str, str]:
    """{ISO date: what it is for ('' if unknown)} of the hearings today or later: the date the case is next posted to, and any
    later day already on a cause list."""
    today = _today()
    found = {h["date"]: h.get("stage", "") for h in tracker_case["hearings"] if h["date"] >= today}
    if tracker_case.get("next_hearing", "") >= today and tracker_case.get("next_hearing"):
        found[tracker_case["next_hearing"]] = tracker_case.get("next_stage", "") or found.get(tracker_case["next_hearing"], "")
    return found


def source_links(tracker_viewer, tracker_case: dict, date: str) -> list[tuple[str, str]]:
    """[(file name, absolute link)] of the court's PDF(s) that name `date` as the next hearing -- those of the hearing the tracker
    took it from -- or [] when the date is not that one (a cause-list day) or the court gave no document (the DMC)."""
    if date != tracker_case.get("next_hearing") or not tracker_case.get("next_from"):
        return []
    try:
        found = tracker_viewer.hearing_files(tracker_case.get("case_key"), tracker_case.get("court_id"), tracker_case["next_from"])
    except Exception:
        log.exception("finding the document naming %s", date)
        return []
    base = mailer.base_url(SITE_HOST)
    kmc = (tracker_case.get("court_id") or "").split("-")[0] == "kmc"
    return [(name, f"{base}/case_tracker/" + (f"pdf/post_hearing/{name}" if kmc else f"order/{tracker_case['case_key']}/{name}"))
            for _, name in found]


def compose_reminder(case: dict, tracker_case: dict, date: str, stage: str, when: str,
                     sources: list[tuple[str, str]] = ()) -> tuple[str, str, str]:
    """(subject, text body, HTML body) of a reminder; `when` is "today", "in two days" or "in two weeks"."""
    label = case["case_number"]
    day = datetime.strptime(date, "%Y-%m-%d")
    pretty = f"{day.strftime('%A')}, {_pretty(date)}"
    subject = f"Hearing {when}: {label}"
    court = tracker_case.get("court") or tracker_case.get("court_label") or ""
    lines = [f"You have a hearing {when}, {pretty}.", f"Case: {label}" + (f" ({case['name']})" if case["name"] else "")]
    if court:
        lines.append(f"Court: {court}")
    if stage:
        lines.append(f"Listed for: {stage}")
    link = f"{mailer.base_url(SITE_HOST)}/cases"
    foot = "You get this because you have access to this case. To stop these emails, untick the alerts box on your home page."
    disclaimer = "Please check this date: it was read from the court's published record and may be wrong or have changed."
    src_text = "".join(f"\nThe document it was taken from: {n}: {u}" for n, u in sources)
    text = f"{pretty.upper()}\n\n{disclaimer}{src_text}\n\n" + "\n".join(lines) + f"\n\nOpen your cases: {link}\n\n{WARNING}\n\n{foot}\n"
    esc = html.escape
    body = ('<div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; font-size: 15px; color: #222; max-width: 560px;">'
            f'<h1 style="text-align: center; font-size: 28px; line-height: 1.25; margin: 0 0 16px;">{esc(pretty)}</h1>'
            f'<p style="text-align: center; font-size: 13px; color: #555; margin: 0 0 16px;">{esc(disclaimer)}'
            + "".join(f'<br>The document it was taken from: <a href="{esc(u, quote=True)}">{esc(n)}</a>' for n, u in sources) + "</p>"
            f"<p><strong>{esc(lines[0])}</strong></p>" + "".join(f"<p style=\"margin: 4px 0;\">{esc(x)}</p>" for x in lines[1:]) +
            f'<p><a href="{esc(link, quote=True)}">Open your cases</a></p>'
            f'<p style="font-size: 14px; border: 1px solid #c9a227; background: #fff8e1; padding: 8px 12px;"><strong>{esc(WARNING)}</strong></p>'
            f'<p style="font-size: 13px; color: #555;">{esc(foot)}</p></div>')
    return subject, text, body


def send_reminders(tracker_viewer, case: dict, tracker_case: dict) -> int:
    """The reminders due now for one case: a hearing exactly 14, 2 or 0 days away, once it is 4 am IST. (Only on the exact day: a
    hearing added to the tracker five days ahead gets no "in two weeks".) Returns how many were sent."""
    now = datetime.now(IST)
    if now.hour < REMINDER_HOUR_IST:
        return 0
    sent_count = 0
    for date, stage in sorted(_upcoming(tracker_case).items()):
        days = (datetime.strptime(date, "%Y-%m-%d").date() - now.date()).days
        if days not in REMINDERS or storage.reminder_sent(case["id"], date, days):
            continue
        recipients = storage.alert_recipients(case["id"])
        if recipients:
            subject, text, body = compose_reminder(case, tracker_case, date, stage, REMINDERS[days],
                                                     source_links(tracker_viewer, tracker_case, date))
            if not any([mailer.send_email_now(SITE_HOST, to, subject, text, body) for to in recipients]):
                log.error("hearing reminder for case %s, %s (%d days): not delivered; will retry", case["id"], date, days)
                continue
        storage.mark_reminder_sent(case["id"], date, days)
        sent_count += 1
    return sent_count


def check_case(tracker_viewer, case: dict) -> int:
    """One case: baseline it if new, else email its newly recorded hearings. Returns how many hearings were announced."""
    tracker_case = tracker_viewer.case_prefill(case["case_number"], with_court=True)
    if tracker_case is None:
        return 0
    send_reminders(tracker_viewer, case, tracker_case)
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
