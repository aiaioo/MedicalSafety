#!/usr/bin/env python3
"""How each case's judgement PDF reads (dismissed / allowed in part / allowed in full / unclear / scanned) and, for
a dismissal, why -- worked out once and kept in the case_judgements table, so the tracker's analytics need not
open every PDF after each restart.

`python judgements.py` (the daily crawl runs it after the downloader) classifies each case's judgement PDF that is
new, has changed (file name or size), or was last classified by an older CLASSIFIER_VERSION, and drops rows for
cases whose judgement PDF is gone. The viewer calls judgement_of(), which uses the stored row and classifies on the
spot (and stores it) only for a PDF the daily run has not reached yet.

Bump CLASSIFIER_VERSION whenever the keyword rules below change, so the next run re-reads everything.
"""

import re
import threading
import time

import fitz  # PyMuPDF

import progress_storage as storage

CLASSIFIER_VERSION = 1
TAIL_CHARS = 3500  # the operative end of a judgement is all the rules look at

ORDER_RE = re.compile(r"(?P<date>[0-9-]+)_(?P<kind>daily_order|judgement)(?:_v(?P<v>\d+))?\.(?P<ext>pdf|html)")

JUDGEMENT_CLASSES = [("dismissed", "Dismissed"), ("part", "Allowed in part"), ("full", "Allowed in full"),
                     ("unclear", "Unclear"), ("scanned", "Scanned (not read)")]
# why a dismissed case was dismissed, first match wins (a keyword pass over the end of the judgement)
DISMISSAL_REASONS = [
    ("nonpros", "Non-prosecution (no one appeared)", r"non[- ]?prosecution|no representation|not interested in|failed to appear|remained absent|\bnemo\b|not turn(?:ed)? up"),
    ("limitation", "Time-barred or no jurisdiction", r"time[- ]barred|barred by (?:limitation|time)|beyond (?:the )?(?:period of )?limitation|delay (?:is )?not condon|jurisdiction|not a consumer|free of charge"),
    ("withdrawn", "Withdrawn or settled", r"withdr[ae]w|settle|compromise"),
    ("appeal", "Appeal: lower commission's order upheld", r"no interference|(?:is|are) (?:hereby )?confirmed|devoid of merit|does not call for interference|no (?:valid )?grounds"),
    ("merits", "Negligence or deficiency not proved", r"no (?:any )?(?:medical )?negligence|not proved|failed to prove|not established|no deficiency|burden of proof|failed to establish"),
    ("other", "Other or unclear", r""),
]


def _iso_date(s):
    """The portal's date as YYYY-MM-DD (it may come as DD-MM-YYYY); unchanged if unrecognised."""
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", s)
    return f"{m[3]}-{m[2]}-{m[1]}" if m else s


def classify_judgement(text):
    """Which of JUDGEMENT_CLASSES a judgement's text falls in, by the wording of its operative end. Rough: a keyword
    pass, not a reading of the order."""
    if len(text) < 200:
        return "scanned"  # no text layer
    tail = text[-3000:]
    if re.search(r"partly|partially|in part", tail, re.I) and re.search(r"allow", tail, re.I):
        return "part"
    if re.search(r"(?:complaint|case)\s+(?:is|stands)\s+(?:hereby\s+)?allowed|is\s+allowed", tail, re.I):
        return "full"
    if re.search(r"dismissed", tail, re.I):
        return "dismissed"
    return "unclear"


def dismissal_reason(text):
    """Which of DISMISSAL_REASONS a dismissed judgement gives (from the wording at its end)."""
    tail = text[-TAIL_CHARS:]
    return next((k for k, _, rx in DISMISSAL_REASONS if rx and re.search(rx, tail, re.I)), "other")


def judgement_file(case_key):
    """The case's judgement PDF (latest date, then latest version) as a Path, or None."""
    best = None
    for p in storage.order_files(case_key):
        m = ORDER_RE.fullmatch(p.name)
        if m and m["kind"] == "judgement" and m["ext"] == "pdf":
            k = (_iso_date(m["date"]), int(m["v"] or 1))
            if best is None or k > best[0]:
                best = (k, p)
    return best[1] if best else None


def tail_text(path):
    """The end of a PDF's text: pages are read from the last backwards until TAIL_CHARS are in hand, so a long
    judgement is not read whole. A PDF with less than that in total is read whole ('' if it has no text layer)."""
    parts, size = [], 0
    with fitz.open(path) as d:
        for page in reversed(list(d)):
            t = page.get_text()
            parts.insert(0, t)
            size += len(t)
            if size >= TAIL_CHARS + 500:
                break
    return re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()


def classify_file(path):
    """(class, reason) of a judgement PDF; reason is '' unless dismissed."""
    text = tail_text(path)
    cls = classify_judgement(text)
    return cls, (dismissal_reason(text) if cls == "dismissed" else "")


def _result(row):
    return {"name": row["file"], "class": row["class"], "label": dict(JUDGEMENT_CLASSES)[row["class"]], "reason": row["reason"] or None}


def _current(row, path):
    return bool(row) and row["file"] == path.name and row["size"] == path.stat().st_size and row["version"] == CLASSIFIER_VERSION


_rows, _rows_at, _lock = {}, 0.0, threading.Lock()
RELOAD_AFTER = 60  # seconds: how long the viewer trusts its copy of the table (the daily run writes it)


def _stored():
    global _rows, _rows_at
    with _lock:
        if time.time() - _rows_at > RELOAD_AFTER:
            _rows, _rows_at = storage.load_judgements(), time.time()
        return _rows


def judgement_of(case_key):
    """{"name", "class", "label", "reason"} for a case's judgement PDF, or None if it has none. Uses the stored
    classification; classifies and stores it when there is none (or it is out of date)."""
    path = judgement_file(case_key)
    if path is None:
        return None
    row = _stored().get(case_key)
    if not _current(row, path):
        try:
            cls, reason = classify_file(path)
        except Exception:
            return {"name": path.name, "class": "scanned", "label": dict(JUDGEMENT_CLASSES)["scanned"], "reason": None}  # unreadable: not stored, so tried again
        row = {"file": path.name, "size": path.stat().st_size, "class": cls, "reason": reason, "version": CLASSIFIER_VERSION}
        try:
            storage.save_judgement(case_key, **row)
        except Exception:
            pass  # the answer is still good; the daily run stores it
        _stored()[case_key] = row
    return _result(row)


def main():
    stored = storage.load_judgements()
    done = skipped = failed = 0
    seen = set()
    for folder in sorted(storage.CASE_DETAILS_DIR.glob("*")):
        case_key = folder.name
        path = judgement_file(case_key)
        if path is None:
            continue
        seen.add(case_key)
        if _current(stored.get(case_key), path):
            skipped += 1
            continue
        try:
            cls, reason = classify_file(path)
        except Exception as e:
            print(f"!! {case_key}: {e}")
            failed += 1
            continue
        storage.save_judgement(case_key, file=path.name, size=path.stat().st_size, **{"class": cls}, reason=reason, version=CLASSIFIER_VERSION)
        done += 1
    gone = storage.delete_judgements_except(seen) if seen else 0
    print(f"judgements: {done} classified, {skipped} already up to date, {failed} unreadable, {gone} removed")


if __name__ == "__main__":
    main()
