#!/usr/bin/env python3
"""Karnataka Medical Council (KMC): download new pre- and post-hearing PDFs from its enquiry page.

The page https://www.karnatakamedicalcouncil.com/Enquiry.aspx links to PDFs
listing the cases before the Council. Two kinds exist:

  * pre_hearing   - cases listed for the next hearing. No "Next Date and
    Stage" column.
  * post_hearing  - what happened at the last hearing. These carry a
    "Next Date and Stage" column.

Each run fetches the page, finds pre- and post-hearing PDFs not seen before, downloads
them into pdfs/kmc/pre_hearing or pdfs/kmc/post_hearing, and records them in
storage/kmc/state.json so they are never downloaded twice. A PDF that fails to download is
not recorded, so the next run retries it.

Files placed by hand in manually_downloaded/kmc/{pre,post}_hearing are also
copied into pdfs/kmc/ (same sub-folder, renamed KMC_Enquiry_<date>.pdf) when no
identical (or near-identical) file is already there.
"""

import hashlib
import os
import re
import shutil
import tempfile
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader

from storage import load_state, save_state

from . import BASE_DIR, Court, log, make_session

PAGE_URL = "https://www.karnatakamedicalcouncil.com/Enquiry.aspx"
# Pre- and post-hearing PDFs live here; the rest of /Upload/ holds general notices and forms.
HEARING_PATH_PREFIX = "/Upload/MarqueeFiles/"

USER_AGENT = "Mozilla/5.0 (compatible; kmc-case-progress-crawler)"
TIMEOUT = 60

NEXT_DATE_RE = re.compile(r"next\s+date", re.IGNORECASE)
HEARING_DATE_RE = re.compile(r"\bheld\s+on\s+[^\d]{0,20}?(\d{1,2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{4})", re.IGNORECASE)


def find_hearing_links(html):
    """Return {absolute_url: link_text} for pre- and post-hearing PDFs on the page."""
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(PAGE_URL, a["href"].strip())
        path = urlparse(url).path
        if path.startswith(HEARING_PATH_PREFIX) and path.lower().endswith(".pdf"):
            links[url] = " ".join(a.get_text().split())
    return links


def pdf_text(path):
    try:
        return " ".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception as e:
        log(f"  could not read PDF text ({e}); falling back to link text")
        return ""


def classify(text, title):
    """Return 'past', 'future' or None.

    A "Next Date and Stage" column means past. Older past lists lack that column
    but are headed "ENQUIRIES HELD ON ..." (future ones say "TO BE HELD ON"), so
    the heading is checked next, in the PDF text and then the link text.
    """
    if NEXT_DATE_RE.search(text):
        return "past"
    for source in (text[:500], title):
        s = " ".join(source.upper().split())
        if "TO BE HELD" in s:
            return "future"
        if "HELD ON" in s:
            return "past"
    return "future" if text.strip() else None


def hearing_date(text, title):
    """Return the hearing date as yyyymmdd, from the PDF heading then the link text."""
    for source in (text[:500], title):
        m = HEARING_DATE_RE.search(source)
        if m:
            day, month, year = (int(g) for g in m.groups())
            try:
                return f"{datetime(year, month, day):%Y%m%d}"
            except ValueError:
                pass
    return None


def unique_path(dest_dir, base):
    """KMC_Enquiry_<date>.pdf, or _2, _3... if that name is taken."""
    n = 1
    while True:
        suffix = "" if n == 1 else f"_{n}"
        dest = os.path.join(dest_dir, f"{base}{suffix}.pdf")
        if not os.path.exists(dest):
            return dest
        n += 1


MANUAL_NAME_RE = re.compile(r"KMC_Enquiry_(\d{8})(?:_after)?\.pdf$", re.IGNORECASE)


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def text_key(path):
    """PDF text with whitespace removed, so a re-saved or converted copy of the same
    document matches. None when there is no text (e.g. a scan)."""
    text = re.sub(r"\s+", "", pdf_text(path))
    return re.sub(r"\d+\|Page", "", text, flags=re.IGNORECASE) or None  # drop "1 | P a g e" footers


SAME_TEXT_RATIO = 0.98


class KMC(Court):
    id = "kmc"
    name = "Karnataka Medical Council"
    city = "Bangalore"
    state = "Karnataka"

    def _sub(self, name):
        return os.path.join(self.pdf_dir, name)

    def download_pdf(self, session, url, title, state, dry_run):
        name = os.path.basename(unquote(urlparse(url).path))
        log(f"New: {name}  [{title}]")
        if dry_run:
            return True
        resp = session.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        if not resp.content.startswith(b"%PDF"):
            raise ValueError("response is not a PDF")

        os.makedirs(self.pdf_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.pdf_dir, suffix=".part", delete=False) as tmp:
            tmp.write(resp.content)
        try:
            text = pdf_text(tmp.name)
            kind = classify(text, title)
            dest_dir = {"past": self._sub("post_hearing"), "future": self._sub("pre_hearing")}.get(kind, self._sub("unclassified"))
            if kind is None:
                log("  WARNING: could not tell past from future; saved under unclassified")
            date = hearing_date(text, title)
            if date is None:
                log("  WARNING: no hearing date found; keeping the original file name")
            os.makedirs(dest_dir, exist_ok=True)
            if date:
                dest = unique_path(dest_dir, f"KMC_Enquiry_{date}")
            else:
                dest = os.path.join(dest_dir, name)
            os.replace(tmp.name, dest)
        except BaseException:
            if os.path.exists(tmp.name):
                os.remove(tmp.name)
            raise

        log(f"  saved to {os.path.relpath(dest, BASE_DIR)}")
        state["seen"][url] = {
            "title": title,
            "saved_to": os.path.relpath(dest, BASE_DIR),
            "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_state(self.id, state)
        return True

    def sync_manual(self, dry_run):
        """Copy PDFs from manually_downloaded/ that are not yet in pdfs/. A file counts
        as already there if its bytes match, or if its text nearly matches (a converted
        or re-saved copy) a pdfs/ file for the same hearing date."""
        hashes, texts = set(), {}  # texts: yyyymmdd -> [text_key, ...]
        for root, _, files in os.walk(self.pdf_dir):
            for f in files:
                if f.lower().endswith(".pdf"):
                    path = os.path.join(root, f)
                    hashes.add(file_hash(path))
                    m = re.match(r"KMC_Enquiry_(\d{8})", f)
                    if m:
                        texts.setdefault(m.group(1), []).append(text_key(path))

        added = 0
        for root, _, files in sorted(os.walk(self.manual_dir)):
            for f in sorted(files):
                if f.startswith("."):
                    continue
                src = os.path.join(root, f)
                if not f.lower().endswith(".pdf"):
                    log(f"Manual file is not a PDF, skipped: {os.path.relpath(src, BASE_DIR)}")
                    continue
                digest = file_hash(src)
                if digest in hashes:
                    continue
                m = MANUAL_NAME_RE.match(f)
                date = m.group(1) if m else hearing_date(pdf_text(src), "")
                text = text_key(src)
                if text and any(
                    t and SequenceMatcher(None, t, text, autojunk=False).ratio() >= SAME_TEXT_RATIO
                    for t in texts.get(date, [])
                ):
                    continue
                # The folder you put it in decides past/future.
                folder = os.path.basename(root)
                if folder in ("post_hearing", "pre_hearing"):
                    dest_dir = self._sub("post_hearing") if folder == "post_hearing" else self._sub("pre_hearing")
                else:
                    dest_dir = self._sub("unclassified")
                os.makedirs(dest_dir, exist_ok=True)
                dest = unique_path(dest_dir, f"KMC_Enquiry_{date}") if date else os.path.join(dest_dir, f)
                log(f"Manual: {os.path.relpath(src, BASE_DIR)} -> {os.path.relpath(dest, BASE_DIR)}")
                if not dry_run:
                    shutil.copy2(src, dest)
                hashes.add(digest)
                texts.setdefault(date, []).append(text)
                added += 1
        log(f"{added} manually downloaded PDFs {'would be ' if dry_run else ''}added to pdfs/")

    def download(self, dry_run=False):
        try:
            self.sync_manual(dry_run)
        except Exception as e:
            log(f"ERROR syncing manually_downloaded: {e}")

        session = make_session(USER_AGENT)
        try:
            resp = session.get(PAGE_URL, timeout=TIMEOUT)
            resp.raise_for_status()
            links = find_hearing_links(resp.text)
            if not links:
                log("ERROR: no hearing PDF links found on the page; has the layout changed?")
                return 1

            state = load_state(self.id)
            new = {u: t for u, t in links.items() if u not in state["seen"]}
            log(f"{len(links)} hearing PDFs linked, {len(new)} new")

            failures = 0
            for url, title in new.items():
                try:
                    self.download_pdf(session, url, title, state, dry_run)
                except Exception as e:
                    failures += 1
                    log(f"  FAILED {url}: {e}")
            return failures
        finally:
            session.close()
