#!/usr/bin/env python3
"""Analyse the hearing-list PDFs downloaded by downloader.py.

For every enquiry (e.g. KMC/ENQ/25/2025) it builds
  a) the complainant(s) and respondent(s), and
  b) a timeline of hearings: the date, the list (A, B, ...) it was posted in,
     what the hearing is for (cross-examination of X by Y, examination-in-chief,
     filing evidence by affidavit, order on ..., arguments, disposed, dismissed ...)
     and what happened / what was scheduled next.

Inputs:  pdfs/future_hearings/*.pdf  - lists of cases for an upcoming hearing
         (list A/B/..., complainant, respondent). Older ones are laid out as
         "Complainant  V/s  Respondents", newer ones as a table.
         pdfs/past_hearings/*.pdf    - outcome of a hearing and the next date/stage.
         Circulars announcing that a sitting is postponed to another date may sit
         in either folder; a next date that falls on a postponed sitting is moved to the new date.
Outputs (in storage/, via storage.py; rebuilt from scratch on every run, so re-running after
the downloader fetches new PDFs updates them):
         enquiries.json   - everything, one object per enquiry
         status.csv       - one row per enquiry: case_key, enquiry_id, status, date the status first appeared
         parties.csv      - one row per party: enquiry, party, party_type (doctor/non_doctor/institution), role (complainant/respondent)
         timeline.csv     - one row per enquiry per hearing date, including next_date (extracted from the outcome
                            text, after applying postponement circulars; postponed_from holds the original date) and raw_text (what the list PDFs printed for the case, source file by source file)

Requires poppler's `pdftotext` on PATH (or /opt/local/bin). Table-style lists
are parsed from word coordinates (`-bbox`), older ones from `-layout` text.
The layouts are free-form, so parsing is heuristic: lists or rows that could
not be parsed are reported as warnings at the end of the run.

Usage:  python analysis.py
"""

import html
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_DIR = os.path.join(BASE_DIR, "pdfs")

# KMC/ENQ/25/2025, KMC/INQ/89/2026, KMC/84Exp/Doct/2019 (tolerating stray spaces)
ID_RE = re.compile(
    r"KMC\s*/(?:\s*[A-Za-z0-9()]+(?:-\s*[A-Za-z0-9()]+)*\s*/){1,4}\s*(?:\d\s*){4}(?!\d)",
    re.IGNORECASE,
)
ID_PART_RE = re.compile(ID_RE.pattern.replace(r"(?:\d\s*){4}(?!\d)", r"(?:\d\s*){3}$"), re.IGNORECASE)
VS_RE = re.compile(r"\bV\s*/?\s*s\b\.?", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{1,2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{4})\b")
HELD_RE = re.compile(r"(?:held|to\s+be\s+held)\s+on\s+[^\d]{0,20}?(\d{1,2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{4})", re.IGNORECASE)
LIST_RE = re.compile(r"^\s*LIST\s+([A-Z])\b\s*[:\-–—.]?\s*(.*)$", re.IGNORECASE)
FOOTER_RE = re.compile(r"(\d+\s*\|\s*Page|Enquiries\s+(?:held|to\s+be\s+held)\s+on|KARNATAKA MEDICAL COUNCIL,?\s*BANGALORE|^\s*Sd/-|^\s*Registrar)", re.IGNORECASE)
TIME_RE = re.compile(r"(?<![\d/-])(\d{1,2})(?:\s*[:.]\s*(\d{2}))?\s*([ap])\s*\.?\s*m\b\.?", re.IGNORECASE)
NUMBERED_RE = re.compile(r"(?:(?<=\s)|^)\(?(\d{1,2})[).]\s*(?=\S)")

warnings = []


def warn(msg):
    warnings.append(msg)


def norm_id(s):
    s = re.sub(r"\s+", "", s).upper().rstrip(".")
    s = re.sub(r"^(KMC/)+", "KMC/", s)  # typos in the lists: KMC/KMC/INQ/.., ENO, ENQL18
    s = re.sub(r"/ENO/", "/ENQ/", s)
    s = re.sub(r"/ENQL(\d+)/", r"/ENQ/\1/", s)
    return re.sub(r"/(\d+(?:\([A-Z]\))?)/EXP/", r"/\1EXP/", s)  # KMC/76/EXP/DOCT/2023 == KMC/76Exp/Doct/2023


def case_key(eid):
    """Unique key: SERIES_serial[suffix]_year, e.g. KMC/ENQ/12/2024 -> ENQ_12_2024, KMC/98(A)EXP/DOCT/2021 -> EXP_98A_2021."""
    parts = eid.split("/")
    mid = "/".join(parts[1:-1])
    year = int(parts[-1])
    for pat, series in ((r"(ENQ|INQ)/(\d+)()", None), (r"(\d+)(?:\(([A-Z])\))?EXP/DOCT", "EXP"),
                        (r"MEIC/COMP-(\d+)()/INSP-\d+", "MEIC"), (r"(\d+)()", "PLAIN")):
        m = re.fullmatch(pat, mid)
        if m:
            g = m.groups()
            if series is None:
                return f"{g[0]}_{int(g[1])}_{year}"
            return f"{series}_{int(g[0])}{g[1] or ''}_{year}"
    raise ValueError(f"unrecognised enquiry id: {eid}")


PERSON_PREFIXES = r"(?:mr|mrs|ms|miss|sri|shri|shree|sh|smt|kum|kumari|capt|lt|col|sgt)"
INSTITUTION_WORDS = r"hospital|clinic|centre|center|institute|instt|medical college|nursing home|laborator|diagnostic|health care|healthcare|trust|association"
OFFICE_WORDS = (r"administ|director|superintendent|manager|ceo|head of|secretary|president|commissioner|officer|police|"
                r"department|samithi|prathishtana|forum|foundation|society|committee|council|bbmp|nmc|headquaters|headquarters|"
                r"services|enterprises|pvt|ltd|su-moto|health")
LEADING_JUNK = r"^\W*(?:IS CLUBBED WITH KMC/\S+|\[KMC/\S*|PRONOUNCEMENT OF THE FINAL ORDERS|\d+:\d+\s*[ap]\.?m\.?)?\W*(?:\(\d+\)\W*)?"


def is_noise(name):
    """Fragments of the PDF layout (headings, times, dates, wrapped text) that were parsed as a party."""
    return bool(re.fullmatch(r"[\W\d]*(?:respondents?|to|[ap]\.?m\.?)?[\W\d]*", name, re.I)
                or re.match(r"\d+\s+clubbed with", name, re.I))


def party_type(name, role=""):
    """Classify a party from its prefix: Dr. -> doctor, Mr./Smt./Sri. etc. -> non_doctor, M/s. -> institution.
    Without a prefix: office-holders and bodies ("The Administrator", "District Health Officer") and names with a
    hospital/institution word are institutions, a mention of doctors is doctor, and anything else is a doctor if a respondent, a non_doctor if a complainant."""
    if not name:
        return ""
    name = re.sub(LEADING_JUNK, "", name, flags=re.I)  # "IS CLUBBED WITH KMC/.. Dr.X", "[KMC/..] Mr.X", "3:00 p.m. (1)Ms.X"
    if re.match(r"m\s*/\s*s\b", name, re.I):
        return "institution"
    if re.match(r"dr\b", name, re.I):  # "Dr.Agarwals Eye Hospital" (at most two words, then the institution word) is an institution
        return "institution" if re.match(r"dr\W*\w+(?:\s+\w+)?\s+(?:%s)" % INSTITUTION_WORDS, name, re.I) else "doctor"
    if re.match(PERSON_PREFIXES + r"\b", name, re.I):  # "Sri Jayadeva Institute..." is an institution; "Mr.X, M/s.Y Hospital" a person
        return "institution" if re.match(r"[^,]*?(?:%s)" % INSTITUTION_WORDS, name.split("M/s")[0].split("C/o")[0], re.I) else "non_doctor"
    if re.search(r"m/s\.", name, re.I) or re.search(INSTITUTION_WORDS + "|" + OFFICE_WORDS, name, re.I):
        return "institution"
    if re.search(r"doctor", name, re.I):
        return "doctor"
    return "non_doctor" if role == "complainant" else "doctor"  # medical councils only enquire into doctors and medical institutions


def squash(s):
    return re.sub(r"\s+", " ", s).strip()


def parse_time(text):
    """First clock time in text as 24-hour 'HH:MM', e.g. '3:00 p.m.' -> '15:00'."""
    m = TIME_RE.search(text or "")
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    if not (1 <= h <= 12 and mi < 60):
        return None
    return f"{h % 12 + (12 if ap == 'p' else 0):02d}:{mi:02d}"


def is_heading_continuation(text):
    """A wrapped second line of a list heading: capitals only, apart from 'at <time>'."""
    s = re.sub(r"\bat\b", "", TIME_RE.sub("", text), flags=re.I)
    return not re.match(r"\d", text) and not ID_RE.search(text) and not re.search(r"[a-z]", s) and bool(re.search(r"[A-Z]{3}", s))


def make_date(d, m, y):
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def extract_text(path):
    exe = shutil.which("pdftotext") or "/opt/local/bin/pdftotext"
    try:
        return subprocess.run([exe, "-layout", path, "-"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        from pypdf import PdfReader  # fallback; layout is poorer
        return "\n".join(p.extract_text(extraction_mode="layout") or "" for p in PdfReader(path).pages)


def hearing_date(path, text):
    """Hearing date: from KMC_Enquiry_<yyyymmdd>[_n].pdf, else from the heading."""
    m = re.match(r"KMC_Enquiry_(\d{4})(\d{2})(\d{2})", os.path.basename(path))
    if m:
        return make_date(m.group(3), m.group(2), m.group(1))
    m = HELD_RE.search(text[:600])
    return make_date(*m.groups()) if m else None


# --------------------------------------------------------------------------
# Splitting parties
# --------------------------------------------------------------------------

def split_parties(text):
    """'(1) Dr A (2) Dr B & others' -> ['Dr A', 'Dr B & others']."""
    text = squash(text)
    if not text:
        return []
    marks = list(NUMBERED_RE.finditer(text))
    if len(marks) >= 2 or (marks and marks[0].start() == 0):
        out = []
        pre = text[:marks[0].start()].strip()
        if pre:
            out.append(pre)
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            out.append(text[m.end():end].strip(" ,;"))
        return [p for p in out if p]
    return [text.strip(" ,;")]


# --------------------------------------------------------------------------
# Table-style lists (Sl. No | Inquiry Number | Complainant | Respondents [| Next Date and Stage])
# --------------------------------------------------------------------------

WORD_RE = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', re.S)
HEADER_KEYS = {"id": "Inquiry", "complainant": "Complainant", "respondent": "Respondents", "outcome": "Next"}


def read_pages(path):
    """Words with coordinates, one list per page: (x0, y_centre, text)."""
    exe = shutil.which("pdftotext") or "/opt/local/bin/pdftotext"
    xml = subprocess.run([exe, "-bbox", path, "-"], capture_output=True, text=True, check=True).stdout
    pages = []
    for chunk in xml.split("<page ")[1:]:
        words = []
        for x0, y0, x1, y1, t in WORD_RE.findall(chunk):
            t = html.unescape(t)
            words.append((float(x0), (float(y0) + float(y1)) / 2, t))
        pages.append(words)
    return pages


def group_lines(words, tol=2.5):
    lines = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if lines and abs(lines[-1][0] - w[1]) <= tol:
            lines[-1][1].append(w)
        else:
            lines.append([w[1], [w]])
    return [(y, sorted(ws)) for y, ws in lines]


def column_edges(pages):
    """Left edge of each column present in the header: the commonest word-start x near its header word.

    Returns (keys, edges) or None. Some lists lack the complainant/respondent columns.
    """
    keys = list(HEADER_KEYS)
    header = {}
    starts = Counter()
    for words in pages:
        for _, ws in group_lines(words):
            text = " ".join(w[2] for w in ws)
            if re.search(r"Inquiry\s+Number", text) and not header:
                for k in keys:
                    hw = [w for w in ws if w[2].startswith(HEADER_KEYS[k])]
                    if hw:
                        header[k] = hw[0][0]
            else:
                for w in ws:
                    starts[round(w[0])] += 1
    keys = [k for k in keys if k in header]
    if "id" not in keys or len(keys) < 2:
        return None
    sl = next((w[0] for w in pages[0] if w[2] == "Sl."), None)
    floor = sl + 8 if sl is not None else -1  # peaks must lie right of the previous column
    edges = []
    for k in keys:
        cands = [(c, x) for x, c in starts.items() if floor <= x <= header[k] + 5 and c >= 2]
        if not cands:
            return None
        # merge x values within 2pt, then take the most frequent cluster
        best = max(cands, key=lambda cx: (sum(c for c, x in cands if abs(x - cx[1]) <= 2), -abs(cx[1] - header[k])))
        edges.append(best[1] - 3)
        floor = best[1] + 12
    return keys, edges


def page_body(words, edges):
    """Lines of a page as [("list", info) | ("line", words, y)], headers/footers dropped."""
    body = []
    for y, ws in group_lines(words):
        text = squash(" ".join(w[2] for w in ws))
        if not text or FOOTER_RE.search(text):
            continue
        lm = LIST_RE.match(text.replace("–", "-"))
        if lm:
            body.append(("list", (lm.group(1).upper(), squash(lm.group(2))), y))
        elif re.fullmatch(r"PRONOUNCEMENT OF FINAL ORDERS\W*", text, re.I):
            body.append(("list", ("FO", "PRONOUNCEMENT OF FINAL ORDERS"), y))
        elif body and body[-1][0] == "list" and is_heading_continuation(text):
            letter, title = body[-1][1]
            body[-1] = ("list", (letter, squash(title + " " + text)), body[-1][2])
        elif re.search(r"Inquiry\s+Number|^Sl\.|^No\.$|KARNATAKA MEDICAL COUNCIL\s*-|Enquiries\s+(?:held|to\s+be\s+held)\s+on\s*$|AT\s+\d+[.:]\d+\s*[AP]\.?M|^NIL$", text, re.I) and not ID_RE.search(text):
            continue
        else:
            body.append(("line", ws, y))
    return body


def is_anchor(item, edges):
    ws = item[1]
    return item[0] == "line" and re.fullmatch(r"\d{1,3}[A-Za-z]?", ws[0][2]) and ws[0][0] < edges[0]


def detect_alignment(bodies, edges):
    """'bottom' if rows end on the ID line (the line after an ID line starts a new sentence), else 'centre'."""
    cont = new = 0
    for body in bodies:
        for it, nxt in zip(body, body[1:]):
            if is_anchor(it, edges) and nxt[0] == "line" and not is_anchor(nxt, edges):
                txt = " ".join(w[2] for w in nxt[1] if w[0] >= edges[-1])
                if txt:
                    if txt[0].islower():
                        cont += 1
                    else:
                        new += 1
    return "centre" if cont > 0.25 * (cont + new) else "bottom"


def has_id(lines, edges):
    """Does the ID column of these lines hold a complete enquiry ID?"""
    words = [w[2] for it in lines for w in it[1] if edges[0] <= w[0] < edges[1]]
    return bool(ID_RE.search(" ".join(words)))


def table_rows(bodies, edges, align, name):
    """Yield (list_info, lines) for each table row, across pages."""
    cur_list, buf, waiting = (None, ""), [], False
    for body in bodies:
        if align == "bottom":
            for it in body:
                if it[0] == "list":
                    cur_list = it[1]
                    continue
                # a serial number may sit above the ID line: wait until the row has an ID
                if is_anchor(it, edges) and waiting:
                    yield cur_list, buf  # previous row never got a full ID; make_entry will warn or repair it
                    buf, waiting = [], False
                buf.append(it)
                if is_anchor(it, edges) or waiting:
                    waiting = not has_id(buf, edges)
                    if not waiting:
                        yield cur_list, buf
                        buf = []
            continue
        anchors = [i for i, it in enumerate(body) if is_anchor(it, edges)]
        rows = {i: [] for i in anchors}
        lst_at, lst = {}, cur_list
        for i, it in enumerate(body):
            if it[0] == "list":
                lst = it[1]
            elif i in rows:
                lst_at[i] = lst
        cur_list = lst
        for i, it in enumerate(body):
            if it[0] != "line":
                continue
            ok = [a for a in anchors if not any(body[j][0] == "list" for j in range(min(a, i), max(a, i)))]
            if not ok:
                warn(f"{name}: line not attached to any row: {squash(' '.join(w[2] for w in it[1]))[:60]}")
                continue
            rows[min(ok, key=lambda a: (abs(body[a][2] - it[2]), -a))].append(it)
        for i in anchors:
            yield lst_at[i], rows[i]
    if buf:
        warn(f"{name}: {len(buf)} trailing lines not attached to a row")


def parse_table(path, is_past, name=""):
    pages = read_pages(path)
    found = column_edges(pages)
    if not found:
        warn(f"{name}: could not find column positions")
        return []
    keys, edges = found
    bodies = [page_body(w, edges) for w in pages]
    align = detect_alignment(bodies, edges)
    entries = []
    for lst, lines in table_rows(bodies, edges, align, name):
        ent = make_entry(lines, keys, edges, lst, is_past, name)
        if ent:
            entries.append(ent)
    return entries


def make_entry(lines, keys, edges, cur_list, is_past, name):
    cells = [[] for _ in edges]
    lone = []
    for it in lines:
        for w in it[1]:
            ci = max((i for i, e in enumerate(edges) if w[0] >= e), default=None)
            if ci is not None:  # words left of the ID column are the serial number
                cells[ci].append(w[2])
            if re.fullmatch(r"\d", w[2]):
                lone.append(w[2])
    by = dict(zip(keys, (squash(" ".join(c)) for c in cells)))
    ids = [norm_id(m.group(0)) for m in ID_RE.finditer(by["id"])]
    if not ids:  # the last digit of the year sometimes wraps onto its own line, outside the ID column
        m = ID_PART_RE.search(by["id"])
        if m and lone:
            ids = [norm_id(m.group(0) + lone[0])]
    if not ids:
        warn(f"{name}: table row without enquiry id: {squash(' '.join(by.values()))[:80]}")
        return None
    return {"ids": ids, "list": cur_list[0], "list_title": cur_list[1],
            "complainant": by.get("complainant", ""), "respondent": by.get("respondent", ""),
            "outcome": by.get("outcome", ""),
            "raw": "\n".join(f"{k}: {v}" for k, v in by.items() if v)}


# --------------------------------------------------------------------------
# Older future lists: "(n) KMC/..." then "Complainant   V/s  Respondents"
# --------------------------------------------------------------------------

def parse_vs_style(text):
    entries, cur_list, cur = [], (None, ""), None
    for ln in text.splitlines():
        if not ln.strip() or FOOTER_RE.search(ln):
            continue
        lm = LIST_RE.match(ln.replace("–", "-"))
        if lm:
            cur_list = (lm.group(1).upper(), squash(lm.group(2)))
            cur = None
            continue
        if re.search(r"COMPLAINANTS?\s+RESPONDENTS?|ENQUIRIES TO BE HELD", ln, re.I):
            continue
        if cur is None and cur_list[0] and is_heading_continuation(squash(ln)):
            cur_list = (cur_list[0], squash(cur_list[1] + " " + ln))
            continue
        im = re.match(r"^\s*\(?\d+\)\s*(" + ID_RE.pattern + r")(.*)$", ln, re.I)
        if im:
            cur = {"ids": [norm_id(im.group(1))], "list": cur_list[0], "list_title": cur_list[1], "lines": [], "head": ln.strip()}
            entries.append(cur)
            if im.group(2).strip(" .,"):
                cur["lines"].append(im.group(2))
            continue
        if cur is not None:
            cur["lines"].append(ln)
    out = []
    for e in entries:
        vs_col = None
        for l in e["lines"]:
            m = VS_RE.search(l)
            if m:
                vs_col = m.start()
                break
        comp, resp = [], []
        for l in e["lines"]:
            if vs_col is None:
                comp.append(l.strip())
                continue
            m = VS_RE.search(l)
            if m and abs(m.start() - vs_col) <= 3:
                comp.append(l[:m.start()].strip())
                resp.append(l[m.end():].strip())
            else:  # names sit left of the V/s column, respondents right of it
                comp.append(l[:max(vs_col - 2, 0)].strip())
                resp.append(l[max(vs_col - 2, 0):].strip())
        out.append({"ids": e["ids"], "list": e["list"], "list_title": e["list_title"],
                    "complainant": squash(" ".join(comp)), "respondent": squash(" ".join(resp)), "outcome": "",
                    "raw": "\n".join([f"LIST {e['list']}: {e['list_title']}", e["head"]] + [l.rstrip() for l in e["lines"]])})
    return out


# --------------------------------------------------------------------------
# Older past lists: "(n) KMC/ID.   <outcome text, wrapped in the right column>"
# The text is centred on the ID line, so its first lines sit above it.
# --------------------------------------------------------------------------

ITEM_START_RE = re.compile(r"^(?:\d{1,2}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{4}|Posted|Call\b|Case\b|Next\b|\(1\)|1\.)", re.I)


def parse_past_text_style(text):
    items = []  # [text, id-or-None]
    for ln in text.splitlines():
        if not ln.strip() or FOOTER_RE.search(ln) or re.search(r"ENQUIRIES HELD ON", ln, re.I):
            continue
        im = re.match(r"^\s*\(?\d+\)\s*(" + ID_RE.pattern + r")\.?(.*)$", ln, re.I)
        if im:
            items.append([im.group(2).strip(), norm_id(im.group(1))])
        else:
            items.append([ln.strip(), None])
    anchors = [i for i, it in enumerate(items) if it[1]]
    if not anchors:
        return []
    starts = []  # first item of each entry's text
    for k, a in enumerate(anchors):
        prev = anchors[k - 1] if k else -1
        if ITEM_START_RE.match(items[a][0]):
            starts.append(a)
            continue
        cands = [i for i in range(prev + 1, a) if ITEM_START_RE.match(items[i][0])]
        starts.append(cands[-1] if cands else (prev + 1 if k == 0 else a))
    entries = []
    for k, a in enumerate(anchors):
        end = starts[k + 1] if k + 1 < len(anchors) else len(items)
        entries.append({"ids": [items[a][1]], "list": None, "list_title": "", "complainant": "", "respondent": "",
                        "outcome": squash(" ".join(it[0] for it in items[starts[k]:end])),
                        "raw": "\n".join(it[0] for it in items[starts[k]:end])})
    return entries


def add_times(entries, sitting):
    """Give each entry a start time: the one in its list heading, else the sitting's start time.

    Lists A/B state theirs; C/D usually follow List A (the sitting time) or say 'at 3 p.m.'.
    Final orders (FO) are pronounced with List D.
    """
    d_time = None
    for e in entries:
        t = parse_time(e["list_title"])
        if e["list"] == "D" and t:
            d_time = t
        if e["list"] == "FO" and d_time and not t:
            t = d_time
        e["time"], e["time_source"] = (t, "list heading") if t else (sitting, "sitting start" if sitting else None)


def parse_file(path, kind):
    text = extract_text(path)
    if not text.strip():
        warn(f"{os.path.relpath(path, BASE_DIR)}: no text (scanned image?)")
        return None, []
    d = hearing_date(path, text)
    if re.search(r"Inquiry\s+Number", text, re.I):
        entries = parse_table(path, kind == "past", f"{kind}_hearings/{os.path.basename(path)}")
    elif kind == "past":
        entries = parse_past_text_style(text)
    else:
        entries = parse_vs_style(text)
    add_times(entries, parse_time(text[:800]))
    return d, entries


# --------------------------------------------------------------------------
# Stage classification
# --------------------------------------------------------------------------

def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z(0-9])", text) if s.strip()]


CROSS_RE = re.compile(r"(?:further\s+|physical\s+|re-?\s*)?cross[\s-]*examin\w*\s+(?:of\s+(?P<who>.+?)\s+by\s+(?P<by>.+?))(?=\s*(?:\[|\(\d+\)\s*for|\.|;|\band\s+also\b|\band\s+for\b|$))", re.I)

# (tag, regex) – a text can carry several tags.
STAGE_RULES = [
    ("examination_in_chief", re.compile(r"examination[\s-]*in[\s-]*chief|chief[\s-]*examination", re.I)),
    ("cross_examination", re.compile(r"cross[\s-]*examin", re.I)),
    ("evidence_by_affidavit", re.compile(r"evidence\s+(?:by\s+way\s+of|in\s+the\s+form\s+of|on)?\s*affidavit|affidavit\s+(?:of\s+|by\s+way\s+of\s+)?evidence|evidence\s+affidavit|affidavit\s+evidence", re.I)),
    ("filing_objections", re.compile(r"(?:fil\w+|submi\w+)\s+(?:of\s+)?(?:the\s+)?(?:written\s+)?objection|objections?\s+(?:if\s+any\s+)?by", re.I)),
    ("filing_reply", re.compile(r"(?:fil\w+|submi\w+)\s+(?:of\s+)?(?:a\s+|the\s+)?(?:reply|rejoinder|counter|statement\s+of|written\s+statement|response)|reply\s+(?:in\s+the\s+form\s+of\s+affidavit|to\s+the\s+interrogator)", re.I)),
    ("interrogatories", re.compile(r"interrogator", re.I)),
    ("arguments", re.compile(r"argument", re.I)),
    ("oral_submission", re.compile(r"oral\s+submission|oral\s+explanation|explanation\s+on\s+the\s+complaint", re.I)),
    ("hearing_on_IA", re.compile(r"\bI\.?\s?A\.?\s*(?:No\.?|\(|\d)|\bAppl(?:ication|\.)|hearing\s+on\s+(?:the\s+)?I\.?A", re.I)),
    ("marking_of_documents", re.compile(r"marking\s+of\s+documents|production\s+of\s+documents|furnish\w*\s+(?:of\s+)?(?:copy|copies|documents)|submi\w+\s+(?:of\s+)?documents", re.I)),
    ("admissibility_order", re.compile(r"admissibility", re.I)),
    ("order", re.compile(r"\border\b|pronouncement", re.I)),
    ("appearance", re.compile(r"appearance|appear\b|to\s+appear", re.I)),
    ("issue_notice", re.compile(r"issue\s+(?:\w+\s+){0,2}notice|notice\s+to", re.I)),
    ("awaiting_court_order", re.compile(r"await\w*\s+(?:order|the\s+order|High\s+Court)|High\s+Court", re.I)),
    ("non_prosecution_warning", re.compile(r"non[\s-]*prosecution", re.I)),
]
DISMISS_RE = re.compile(r"(?:complaint|case|enquiry|inquiry)?\s*(?:is|are|stands?|was|hereby)\s+(?:hereby\s+)?dismissed|dismissed\s+(?:for|as)\b|dismiss(?:al)?\s+of\s+(?:the\s+)?complaint", re.I)
DISPOSE_RE = re.compile(r"(?:is|are|stands?|was|hereby)\s+(?:hereby\s+)?(?:disposed|closed|dropped|withdrawn)|disposed\s+of", re.I)
CONDITIONAL_RE = re.compile(r"failing\s+which|otherwise|if\s+(?:the\s+)?(?:complainant|respondent)|will\s+be\s+dismissed|shall\s+be\s+dismissed|may\s+be\s+dismissed", re.I)


def classify(text):
    """Return (tags, details) for an outcome / next-stage string."""
    tags, details = [], {}
    if not text:
        return tags, details
    terminal = []
    for s in sentences(text):
        if DISMISS_RE.search(s) and not CONDITIONAL_RE.search(s):
            terminal.append("complaint_dismissed")
        if DISPOSE_RE.search(s) and not CONDITIONAL_RE.search(s):
            terminal.append("complaint_disposed")
    tags.extend(dict.fromkeys(terminal))
    for tag, rx in STAGE_RULES:
        if rx.search(text):
            tags.append(tag)
    cross = [f"{squash(m.group('who'))} by {squash(m.group('by'))}" for m in CROSS_RE.finditer(text)]
    if cross:
        details["cross_examination_of"] = list(dict.fromkeys(cross))
    om = re.search(r"order\s+on\s+(.+?)(?=[.;]|$)", text, re.I)
    if om:
        details["order_on"] = squash(om.group(1))
    return tags, details


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
# dates glued to neighbouring words ("Postedon28-02-2026fororders") still count, so no \b here
NUM_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{4})(?!\d)")
TEXT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:of\s+)?(" + "|".join(m + r"[a-z]*" for m in MONTHS) + r")\.?,?\s*(\d{4})(?!\d)", re.I)
# a date introduced like this is a past event (the complaint, an earlier order), not the next date
PAST_DATE_CUE_RE = re.compile(r"(?:dated|dt\.?|dtd\.?|vide|since|filed\s+on|received\s+on)\W*$", re.I)

# "Posted on 08-08-2026", "Call on...", "next date of hearing ..." name the next listing outright
LISTING_CUE_RE = re.compile(r"(?:posted|call(?:ed)?|listed|adjourned|next\s+date(?:\s+of\s+hearing)?)\s*(?:on|to|for|is)?\W*$", re.I)


def next_date(text, after=None):
    """The date the case is next listed for, from an outcome text; ISO string or None.

    Prefers a date introduced by 'Posted on' / 'Call on' and otherwise takes the first one;
    skips dates introduced as earlier events ('order dated ...') and, when `after`
    (ISO date of this hearing) is given, any on or before it.
    """
    cands = []
    for rx, to_date in ((NUM_DATE_RE, lambda d, m, y: make_date(d, m, y)),
                        (TEXT_DATE_RE, lambda d, m, y: make_date(d, MONTHS[m[:3].lower()], y))):
        for m in rx.finditer(text or ""):
            if PAST_DATE_CUE_RE.search(text[:m.start()]):
                continue
            d = to_date(*m.groups())
            if d and (after is None or d > after):
                cands.append((not LISTING_CUE_RE.search(text[:m.start()]), m.start(), d))
    return min(cands)[2] if cands else None


# --------------------------------------------------------------------------
# Assemble
# --------------------------------------------------------------------------

# Circulars are scanned and badly OCR'd: "L9-O9-2O25", "25,-10-\n20.25."
OCR_DATE_RE = re.compile(r"(?<![A-Za-z0-9])([0-9OLl]{1,2})[-.,]+([0-9OLl]{1,2})[-.,]+([0-9OLl]{2}[.,]?[0-9OLl]{2})(?![0-9OLl])")
CIRCULAR_HEADER_DATE_RE = re.compile(r"Date\s*[:;]?\s*\S+", re.I)


def parse_circular(text):
    """(from_date, to_date) ISO strings if the text announces that sittings are postponed, else None."""
    flat = squash(text)
    if not re.search(r"postpone", flat, re.I):
        return None
    flat = re.sub(r"(?<=[-.,])\s+(?=[0-9OLl])", "", flat)  # a date wrapped over lines
    flat = CIRCULAR_HEADER_DATE_RE.sub(" ", flat)  # the date the circular was issued
    fix = str.maketrans("OLl", "011")
    dates = []
    for m in OCR_DATE_RE.finditer(flat):
        d, mo, y = (g.translate(fix) for g in m.groups())
        iso = make_date(d, mo, re.sub(r"\D", "", y))
        if iso and iso not in dates:
            dates.append(iso)
    return (dates[0], dates[1]) if len(dates) >= 2 and dates[0] < dates[1] else None


def collect():
    """Return ([(kind, filename, hearing date, entries)], [(from_date, to_date, source)]) for the PDFs.

    The second list holds the circulars that postpone a sitting.
    """
    out, circulars = [], []
    for kind in ("future", "past"):
        d = os.path.join(PDF_DIR, f"{kind}_hearings")
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".pdf"):
                continue
            path = os.path.join(d, fn)
            hd, entries = parse_file(path, kind)
            if hd is None or not entries:
                c = parse_circular(extract_text(path))
                if c:
                    circulars.append((*c, f"{kind}_hearings/{fn}"))
                else:
                    warn(f"{kind}_hearings/{fn}: " + ("hearing date not found" if hd is None else "no enquiries found") + "; skipped")
                continue
            out.append((kind, fn, hd, entries))
    return out, circulars


def build(files, circulars=()):
    moved = {frm: to for frm, to, _ in circulars}  # a sitting postponed by circular
    enq = defaultdict(lambda: {"hearings": {}, "party_candidates": []})
    for kind, fn, hd, entries in files:
        for e in entries:
            for eid in e["ids"]:
                rec = enq[eid]
                h = rec["hearings"].setdefault(hd, {"date": hd, "list": None, "list_title": "", "time": None, "time_source": None, "sources": [],
                                                    "outcome": "", "next_date": None, "postponed_from": None, "next_stage": "", "raw": [],
                                                    "next_stage_tags": [], "next_stage_details": {}})
                h["sources"].append(f"{kind}_hearings/{fn}")
                if e.get("raw"):
                    h["raw"].append(f"[{kind}_hearings/{fn}]\n{e['raw']}")
                if e["time"] and (h["time"] is None or (h["time_source"] != "list heading" and e["time_source"] == "list heading")):
                    h["time"], h["time_source"] = e["time"], e["time_source"]
                if kind == "future":
                    # a case can only sit in one list per day; later duplicate file keeps the first
                    if h["list"] is None:
                        h["list"], h["list_title"] = e["list"], e["list_title"]
                    if e["complainant"] or e["respondent"]:
                        rec["party_candidates"].append((hd, e["complainant"], e["respondent"]))
                else:
                    if e["list"] and h["list"] is None:
                        h["list"], h["list_title"] = e["list"], e["list_title"]
                    if e["complainant"] or e["respondent"]:
                        rec["party_candidates"].append((hd, e["complainant"], e["respondent"]))
                    text = e["outcome"]
                    h["outcome"] = text
                    h["next_date"] = next_date(text, hd)
                    h["postponed_from"] = None
                    while h["next_date"] in moved:
                        h["postponed_from"] = h["postponed_from"] or h["next_date"]
                        h["next_date"] = moved[h["next_date"]]
                    h["next_stage"] = text
                    h["next_stage_tags"], h["next_stage_details"] = classify(text)

    result = {}
    for eid, rec in sorted(enq.items()):
        hearings = [rec["hearings"][d] for d in sorted(rec["hearings"])]
        # the purpose of a hearing is what the previous hearing said was next
        for h in hearings:
            h["purpose"] = None
        for prev in hearings:
            nd = prev["next_date"]
            if nd and nd in rec["hearings"]:
                tgt = rec["hearings"][nd]
                tgt["purpose"] = {"scheduled_on": prev["date"], "text": prev["next_stage"],
                                  "tags": prev["next_stage_tags"], "details": prev["next_stage_details"]}
        for h in hearings:
            h["held"] = any(s.startswith("past_") for s in h["sources"])
        cands = [c for c in rec["party_candidates"] if c[1] or c[2]]
        parties = {"complainants": [], "respondents": [], "as_of": None}
        if cands:
            # fullest description wins; newest breaks ties
            best = max(cands, key=lambda c: (len(c[1]) + len(c[2]), c[0]))
            parties = {"complainants": split_parties(best[1]), "respondents": split_parties(best[2]), "as_of": best[0]}
        last = hearings[-1] if hearings else None
        status, status_since = "open", hearings[0]["date"] if hearings else None
        for h in reversed(hearings):
            for t in ("complaint_dismissed", "complaint_disposed"):
                if t in h["next_stage_tags"]:
                    status = t.replace("complaint_", "")
                    status_since = next(x["date"] for x in hearings if t in x["next_stage_tags"])  # first hearing recording it
                    break
            else:
                continue
            break
        result[eid] = {"enquiry_id": eid, **parties, "status": status, "status_since": status_since,
                       "first_seen": hearings[0]["date"] if hearings else None,
                       "last_seen": last["date"] if last else None,
                       "hearings": hearings}
    return result


def write_outputs(result):
    storage.write_json("enquiries.json", result)
    party_rows = []
    for eid, r in result.items():
        rows = [(n, "complainant") for n in r["complainants"]] + [(n, "respondent") for n in r["respondents"]]
        rows = [(n, ro) for n, ro in rows if not is_noise(n)]
        for name, role in rows:  # keep enquiries with no parties recorded
            party_rows.append([eid, name, party_type(name, role), role, r["as_of"] or "", case_key(eid)])
    storage.write_csv("parties.csv", ["enquiry_id", "party", "party_type", "role", "party_as_of", "case_key"], party_rows)
    storage.write_csv("status.csv", ["case_key", "enquiry_id", "status", "status_since"],
                      [[case_key(eid), eid, r["status"], r["status_since"] or ""]
                       for eid, r in sorted(result.items(), key=lambda kv: (case_key(kv[0]), kv[0]))])
    timeline_rows = []
    for eid, r in result.items():
        for h in r["hearings"]:
            p = h["purpose"] or {}
            timeline_rows.append([eid, h["date"], h["time"] or "", h["time_source"] or "", h["list"] or "", "yes" if h["held"] else "no",
                                  ", ".join(p.get("tags", [])), p.get("text", ""),
                                  h["outcome"], h["next_date"] or "", h["postponed_from"] or "", ", ".join(h["next_stage_tags"]),
                                  "; ".join(h["next_stage_details"].get("cross_examination_of", [])),
                                  "\n\n".join(h["raw"]), case_key(eid)])
    storage.write_csv("timeline.csv",
                      ["enquiry_id", "hearing_date", "hearing_time", "time_source", "list", "held", "purpose_tags", "purpose_text",
                       "outcome_text", "next_date", "postponed_from", "next_stage_tags", "cross_examination_of", "raw_text", "case_key"],
                      timeline_rows)


def main():
    files, circulars = collect()
    result = build(files, circulars)
    write_outputs(result)
    print(f"{len(files)} PDFs, {len(circulars)} postponement circulars, {len(result)} enquiries -> {os.path.relpath(storage.STORAGE_DIR, os.getcwd())}/")
    for w in warnings:
        print("warning:", w, file=sys.stderr)


if __name__ == "__main__":
    main()
