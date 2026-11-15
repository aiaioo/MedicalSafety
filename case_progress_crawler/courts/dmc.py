#!/usr/bin/env python3
"""Delhi Medical Council (DMC): the complaint registers it used to publish, recovered from the Wayback Machine.

Until its 2026 rebuild the DMC published, for each year, a "Status of Complaint Received in year <Y>" page
(https://delhimedicalcouncil.org/CmeStatus/status_comp<Y>): one table row per complaint -- number, complainant,
date received, respondent, status, appeal, and a link to the Council's order as a .doc/.docx under /images/.
The live site no longer serves them (its "View Report" links are dead), so this court is a one-off backfill
from web.archive.org, not a daily crawl: re-running it only fetches what is still missing.

What is kept, all in pdfs/dmc/ (the original files, byte for byte, never edited):
  status_pages/status_comp<Y>_<capture>.html   each year's page as the Archive captured it (<capture> = yyyymmddHHMMSS)
  orders/<name>                                each order document the Archive still holds; most it does not
The crawl state (progress_storage.py) records every page and order fetched, with the Archive URL and capture
time, and every order the Archive answered 404 for, so a re-run does not ask again. analysis_dmc.py reads the saved
pages. A fetch that fails for any other reason (network, 5xx, 429) is not recorded, so the next run retries it.

Usage:  python downloader.py --court dmc [--dry-run]
"""

import hashlib
import os
import re
import time
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from progress_storage import load_state, save_state

from . import BASE_DIR, Court, log

SITE = "https://delhimedicalcouncil.org"
PAGE = SITE + "/CmeStatus/status_comp{name}"
ARCHIVE = "https://web.archive.org/web/{when}id_/{url}"  # id_ = the original bytes, without the Archive's banner and rewritten links
# Page names to try: the years 2000-2024, and the spellings of the combined years the old site is known to have used.
PAGE_NAMES = [str(y) for y in range(2000, 2025)] + ["2000-2002", "2000_2002", "2021-2022", "2021_2022"]
NEAR = "2022"  # the Archive answers with the capture nearest this; every capture of these pages found so far is 3 Dec 2022

USER_AGENT = "case-progress-crawler (Delhi Medical Council archive backfill; contact via repository owner)"
TIMEOUT = 90
PAUSE = 0.6  # seconds between requests to the Archive
RETRIES = 3
ORDER_EXT = (".doc", ".docx", ".pdf", ".rtf", ".odt")
CAPTURE_RE = re.compile(r"/web/(\d{14})id_/")
OLE_MAGIC, ZIP_MAGIC = b"\xd0\xcf\x11\xe0", b"PK"  # a .doc, a .docx: how an order differs from the Archive's HTML 404 page


def parse_table(html):
    """The rows of the page's complaint table (the one with the most rows) as lists of cell texts, with the
    order links of each: [(cells, [href, ...]), ...]. Header handling is the analysis's job."""
    soup = BeautifulSoup(html, "html.parser")
    tables = sorted(soup.find_all("table"), key=lambda t: len(t.find_all("tr")), reverse=True)
    if not tables:
        return []
    return [([" ".join(c.get_text().split()) for c in tr.find_all(["td", "th"])], [a["href"] for a in tr.find_all("a", href=True)])
            for tr in tables[0].find_all("tr")]


def is_register(html):
    """A real status page, not the Archive's or the site's 404 page."""
    return "<table" in html.lower() and re.search(r"complaint\s*no|respondent", html, re.IGNORECASE) is not None


class DMC(Court):
    id = "dmc"
    name = "Delhi Medical Council"
    city = "Delhi"
    state = "Delhi"

    @property
    def pages_dir(self):
        return os.path.join(self.pdf_dir, "status_pages")

    @property
    def orders_dir(self):
        return os.path.join(self.pdf_dir, "orders")

    def fetch(self, session, url):
        """GET with a few retries on a network error, 429 or 5xx; the response, or None if it still fails."""
        for attempt in range(1, RETRIES + 1):
            time.sleep(PAUSE if attempt == 1 else 10 * attempt)
            try:
                resp = session.get(url, timeout=TIMEOUT)
            except requests.RequestException as e:
                log(f"  {type(e).__name__} fetching {url} (try {attempt}/{RETRIES})")
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                log(f"  HTTP {resp.status_code} for {url} (try {attempt}/{RETRIES})")
                continue
            return resp
        return None

    def save_page(self, session, name, state, dry_run):
        """Fetch one year's page. Returns 'saved', 'skipped' (already have it), 'absent' (no such page) or 'failed'."""
        url = PAGE.format(name=name)
        absent = state["seen"].setdefault("_absent_pages", {})  # pages the Archive has no copy of: not asked for again
        if url in state["seen"] or url in absent:
            return "skipped"
        if dry_run:
            log(f"Would fetch {url}")
            return "skipped"
        resp = self.fetch(session, ARCHIVE.format(when=NEAR, url=url))
        if resp is None:
            return "failed"
        m = CAPTURE_RE.search(resp.url)
        html = resp.text if resp.status_code == 200 else ""
        if not (m and is_register(html)):
            absent[url] = datetime.now().isoformat(timespec="seconds")
            save_state(self.id, state)
            return "absent"
        os.makedirs(self.pages_dir, exist_ok=True)
        dest = os.path.join(self.pages_dir, f"status_comp{name}_{m.group(1)}.html")
        with open(dest, "wb") as f:
            f.write(resp.content)
        log(f"Saved {os.path.relpath(dest, BASE_DIR)} ({len(resp.content):,} bytes, captured {m.group(1)})")
        state["seen"][url] = {"kind": "status_page", "saved_to": os.path.relpath(dest, BASE_DIR), "archive_url": resp.url,
                              "captured_at": m.group(1), "sha256": hashlib.sha256(resp.content).hexdigest(),
                              "downloaded_at": datetime.now().isoformat(timespec="seconds")}
        save_state(self.id, state)
        return "saved"

    def saved_pages(self):
        """[(name, path)] of the status pages already in pdfs/dmc/status_pages/, oldest year first."""
        found = []
        if os.path.isdir(self.pages_dir):
            for f in sorted(os.listdir(self.pages_dir)):
                m = re.fullmatch(r"status_comp(.+?)_\d{14}\.html", f)
                if m:
                    found.append((m.group(1), os.path.join(self.pages_dir, f)))
        return found

    def order_urls(self):
        """{absolute order URL: the page name it is linked from} for every order link in the saved pages."""
        urls = {}
        for name, path in self.saved_pages():
            with open(path, encoding="utf-8", errors="replace") as f:
                rows = parse_table(f.read())
            for _, hrefs in rows:
                for href in hrefs:
                    url = urljoin(PAGE.format(name=name), href.strip())
                    if urlparse(url).path.lower().endswith(ORDER_EXT):
                        urls.setdefault(url, name)
        return urls

    def save_order(self, session, url, state, missing):
        """Fetch one order. Returns 'saved', 'missing' (the Archive does not have it) or 'failed'."""
        resp = self.fetch(session, ARCHIVE.format(when=NEAR, url=url))
        if resp is None:
            return "failed"
        m = CAPTURE_RE.search(resp.url)
        if resp.status_code == 404 or not m:
            missing[url] = datetime.now().isoformat(timespec="seconds")
            return "missing"
        if resp.status_code != 200 or not (resp.content.startswith(OLE_MAGIC) or resp.content.startswith(ZIP_MAGIC)
                                           or resp.content.startswith(b"%PDF") or resp.content.startswith(b"{\\rtf")):
            log(f"  not a document (HTTP {resp.status_code}, {len(resp.content)} bytes): {url}")
            return "failed"
        os.makedirs(self.orders_dir, exist_ok=True)
        dest = os.path.join(self.orders_dir, os.path.basename(unquote(urlparse(url).path)))
        if os.path.exists(dest):  # two links can share a file name only if they are the same file; never overwrite an original
            return "saved"
        with open(dest, "wb") as f:
            f.write(resp.content)
        state["seen"][url] = {"kind": "order", "saved_to": os.path.relpath(dest, BASE_DIR), "archive_url": resp.url,
                              "captured_at": m.group(1), "sha256": hashlib.sha256(resp.content).hexdigest(),
                              "downloaded_at": datetime.now().isoformat(timespec="seconds")}
        return "saved"

    def download(self, dry_run=False, back_days=None):
        session = requests.Session()  # TLS verification stays on: web.archive.org's certificate is sound
        session.headers.update({"User-Agent": USER_AGENT})
        failures = 0
        try:
            state = load_state(self.id)
            counts = {}
            for name in PAGE_NAMES:
                result = self.save_page(session, name, state, dry_run)
                counts[result] = counts.get(result, 0) + 1
                failures += result == "failed"
            log(f"status pages: {counts}")

            urls = self.order_urls()
            missing = state["seen"].setdefault("_missing_orders", {})
            todo = [u for u in urls if u not in state["seen"] and u not in missing]
            log(f"{len(urls)} order links in the saved pages, {len(todo)} not yet tried")
            if dry_run:
                return failures
            counts = {}
            for i, url in enumerate(todo, 1):
                result = self.save_order(session, url, state, missing)
                counts[result] = counts.get(result, 0) + 1
                failures += result == "failed"
                if i % 100 == 0 or i == len(todo):
                    save_state(self.id, state)
                    log(f"  orders: {i}/{len(todo)} tried: {counts}")
            save_state(self.id, state)
            return failures
        finally:
            session.close()
