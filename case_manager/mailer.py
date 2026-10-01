"""Outgoing email, sent through Postmark's HTTPS API (so it isn't affected by
the SMTP ports DigitalOcean blocks on droplets).

One Postmark server token, POSTMARK_TOKEN, covers both domains: which one a
message goes out as is chosen from the host the visitor used, the same way
the site name is (see app.py's SITE_NAMES). Both domains must be verified in
Postmark (DKIM + Return-Path records) -- see deploy/README.md.

With no POSTMARK_TOKEN set (local development) nothing is sent: the message,
links included, is logged instead.

Mail is delivered on a background thread so a slow or failing Postmark never
holds up a web request; failures are logged, not raised.
"""

from __future__ import annotations

import html
import json
import logging
import os
import threading
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

POSTMARK_URL = "https://api.postmarkapp.com/email"
POSTMARK_TIMEOUT_SECONDS = 15

# domain -> (site name, From address)
SITES = {
    "medicalsafety.in": ("Medical Safety", "noreply@medicalsafety.in"),
    "caseplan.in": ("Case Plan", "noreply@caseplan.in"),
}
DEFAULT_DOMAIN = "caseplan.in"
_DEV_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def _bare_host(host: str) -> str:
    return host.rsplit(":", 1)[0].lower().removeprefix("www.") if not host.endswith("]") else host.lower()


def site_for_host(host: str) -> tuple[str, str, str]:
    """(domain, site name, From address) for the host a request came in on;
    unknown hosts get the default site."""
    domain = _bare_host(host)
    if domain not in SITES:
        domain = DEFAULT_DOMAIN
    name, sender = SITES[domain]
    return domain, name, sender


def base_url(host: str) -> str:
    """The site's absolute base URL, for links in an email. Only a known
    domain (or localhost, in development) is ever echoed back: the Host
    header is attacker-controlled, and a reset link pointing at it would
    let someone phish the token."""
    bare = _bare_host(host)
    if bare in SITES:
        return f"https://{bare}"
    if bare in _DEV_HOSTS:
        return f"http://{host}"
    return f"https://{DEFAULT_DOMAIN}"


def send_email(host: str, to: str, subject: str, text: str, html_body: str | None = None) -> None:
    """Sends an email from the noreply@ address of the site `host` belongs
    to, without waiting for it to be delivered."""
    _, name, sender = site_for_host(host)
    payload = {
        "From": f"{name} <{sender}>",
        "To": to,
        "Subject": subject,
        "TextBody": text,
        "MessageStream": "outbound",
    }
    if html_body:
        payload["HtmlBody"] = html_body

    token = os.environ.get("POSTMARK_TOKEN", "")
    if not token:
        log.warning("POSTMARK_TOKEN is not set; not sending. From: %s To: %s Subject: %s\n%s",
                    payload["From"], to, subject, text)
        return
    threading.Thread(target=_deliver, args=(token, payload), daemon=True).start()


def _deliver(token: str, payload: dict) -> None:
    request = urllib.request.Request(
        POSTMARK_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "X-Postmark-Server-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=POSTMARK_TIMEOUT_SECONDS) as resp:
            resp.read()
    except urllib.error.HTTPError as err:
        log.error("Postmark rejected email %r (HTTP %s): %s", payload["Subject"], err.code, err.read(500))
    except Exception:
        log.exception("Could not send email %r", payload["Subject"])


def send_action_email(host: str, to: str, subject: str, message: str, button_label: str, link: str, footer: str) -> None:
    """The shape every email here has: a short message, one button/link, and
    a closing note (e.g. "ignore this if it wasn't you"). The generic hook
    for notifications too."""
    _, name, _ = site_for_host(host)
    text = f"{message}\n\n{button_label}: {link}\n\n{footer}\n\n-- {name}\n"
    esc = html.escape
    html_body = (
        '<div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; font-size: 15px; color: #222; max-width: 520px;">'
        f"<p>{esc(message)}</p>"
        f'<p><a href="{esc(link, quote=True)}" style="display: inline-block; padding: 10px 18px; background: #2a5bd7; '
        f'color: #fff; text-decoration: none; border-radius: 6px;">{esc(button_label)}</a></p>'
        f'<p style="font-size: 13px; color: #555;">If the button doesn\'t work, copy this address into your browser:<br>{esc(link)}</p>'
        f'<p style="font-size: 13px; color: #555;">{esc(footer)}</p>'
        f'<p style="font-size: 13px; color: #555;">&mdash; {esc(name)}</p>'
        "</div>"
    )
    send_email(host, to, subject, text, html_body)
