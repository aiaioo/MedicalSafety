#!/usr/bin/env python3
"""Analyse the Delhi Medical Council complaint registers downloaded by courts/dmc.py.

Stores the same tables as analysis.py does for the KMC (status, parties, timeline, and the enquiries document) for the
court "dmc" of the state Delhi, via progress_storage.py, rebuilt from scratch on every run, so viewer.py shows Delhi like
Karnataka. The DMC is a medical council like the KMC, so a complaint is an enquiry and its outcome a KMC-style status;
only the source differs. The KMC's lists are PDFs of hearings. The DMC's are HTML pages of one row per complaint, in a layout
that changes from year to year (a column for the patient from 2020, one for the appeal from 2003, no header row in 2018 and
2019, dates written four ways), so this module has its own parser:

  status     one row per complaint: open / disposed / dismissed / withdrawn / unknown, from the Council's status column. A blank
             status with an order linked is read as disposed (an order is the decision); with none it is "unknown".
             status_since is empty: the pages say when a complaint was received, not when it was decided.
  parties    the complainant (name only) and the respondents (doctors and hospitals), without addresses
  timeline   "Complaint received" on the date received, and "Appeal" on the date of the appeal body's decision, where the
             appeal column gives one (MCI / NMC / High Court); an appeal with no readable date goes on the first row
  enquiries  everything about each complaint, as parsed, with the page it came from, the order file name, and whether the
             order document is in pdfs/dmc/orders/

Every status is as of the day the Archive captured the page (3 Dec 2022 for all the pages found), not as of today; that date is
stored in each enquiry as "captured_at".

Only names are kept in the database -- not the complainants' postal addresses, which the pages print -- but the pages themselves
are kept whole in pdfs/dmc/status_pages/ (not in git).

Usage:  python analysis_dmc.py
"""

import os
import re
import sys
from datetime import date
from difflib import SequenceMatcher
from urllib.parse import urljoin

import dmc_orders
import progress_storage as storage
from analysis import party_type
from courts import BASE_DIR
from courts.dmc import DMC, PAGE, parse_table
from dmc_dates import find_date

COURT = "dmc"
STATE = "Delhi"
MIN_YEAR = 2000

# a page name -> the years its complaints were received in (the page for "2002" holds 2000-2002); others: the year, give or take one
PAGE_YEARS = {"2002": (2000, 2002), "2000-2002": (2000, 2002), "2000_2002": (2000, 2002),
              "2021-2022": (2021, 2022), "2021_2022": (2021, 2022)}

# header text -> field, tried in this order (a header cell takes the first it matches)
HEADER_FIELDS = [("no", r"complaint\s*no"), ("patient", r"patient"), ("complainant", r"complainant"), ("date", r"\bdate\b"),
                 ("respondent", r"respondent"), ("appeal", r"appeal"), ("order", r"\border\b"), ("status", r"status|expr\d")]
# without a header row, the columns by how many there are
LAYOUTS = {6: ["no", "complainant", "date", "respondent", "status", "order"],
           7: ["no", "complainant", "date", "respondent", "status", "appeal", "order"],
           8: ["no", "patient", "complainant", "date", "respondent", "status", "appeal", "order"]}

APPEAL_NOTE = re.compile(r"^note\b|\bMCI\b|\bNMC\b|High\s+Court|\bappeal\b|\bDMC\s+order\b", re.I)
warnings = []


def warn(msg):
    warnings.append(msg)


def squash(s):
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------------------------------------
# Rows

def column_map(rows):
    """(fields by column, index of the first data row) from the header row if there is one, else from the width."""
    first = rows[0][0]
    if re.search(r"complaint\s*no", first[0] if first else "", re.I):
        fields = []
        for cell in first:
            fields.append(next((f for f, rx in HEADER_FIELDS if re.search(rx, cell, re.I)), None))
        return fields, 1
    width = max(len(cells) for cells, _ in rows[:20])
    return LAYOUTS.get(width), 0


def read_page(name, path):
    """The complaints of one saved page: [{field: text, "links": [hrefs]}]."""
    with open(path, encoding="utf-8", errors="replace") as f:
        rows = parse_table(f.read())
    rows = [r for r in rows if any(c for c in r[0])]
    if not rows:
        return []
    fields, start = column_map(rows)
    if not fields:
        warn(f"page {name}: unrecognised table layout")
        return []
    out = []
    for cells, hrefs in rows[start:]:
        if len(cells) < 4:
            if out and len(cells) == 1 and re.search(r"appeal|MCI|NMC|High Court", cells[0]):  # a note spanning the row, about the complaint above
                out[-1]["appeal"] = squash(f"{out[-1].get('appeal', '')} {cells[0]}")
            else:
                warn(f"page {name}: short row {cells!r}")
            continue
        row = {f: squash(cells[i]) for i, f in enumerate(fields) if f and i < len(cells)}
        if APPEAL_NOTE.search(row.get("status", "")) and not status_word(row["status"]):  # an appeal note typed into the status column
            row["appeal"] = squash(f"{row.get('appeal', '')} {row.pop('status')}")
        row["links"] = hrefs
        out.append(row)
    return out


def complaint_number(row):
    """The complaint's number, from its column or failing that from the order label ("Order No-3343"); None if neither has one."""
    for text in (row.get("no", ""), row.get("order", "")):
        m = re.search(r"\d+", text)
        if m:
            return int(m.group())
    return None


def looks_like(word, target, cutoff=0.7):
    """Is the word the target, allowing the registers' typos (dispsoed, diposed, dsiposed, dsipsoed, disposedn)?"""
    return SequenceMatcher(None, word, target).ratio() >= cutoff


def status_word(text):
    """The status a status cell states -- open / disposed / dismissed / withdrawn -- or None if it states none."""
    low = re.sub(r"[^a-z ]", " ", squash(text).lower())
    if re.search(r"dism|dimiss", low):
        return "dismissed"
    if re.search(r"withdr", low):
        return "withdrawn"
    if any(looks_like(w, "disposed") for w in low.split()) or re.search(r"\bclosed\b|clubbed", low):
        return "disposed"
    if re.search(r"consider|pending|process|progress|\bunder\b|\bhearing\b|\bnotice\b", low):
        return "open"
    return None


def norm_status(text, has_order):
    """(open / disposed / dismissed / withdrawn / unknown, inferred): the status the register states, and True if it
    had to be inferred (blank or unreadable: an order linked means the complaint was decided)."""
    stated = status_word(text)
    if stated:
        return stated, False
    if squash(text):
        warn(f"unrecognised status {text!r}")
    return ("disposed", True) if has_order else ("unknown", True)


ADDRESS_START = re.compile(r"^(?:\d|[A-Z]{1,2}[-\s]?\d|[A-Z]-?\d+/|flat|house|h\.?\s?no|plot|sector|block|pocket|near|opp|village|vill|"
                           r"gali|street|c/o|r/o|s/o|w/o|d/o|p\.?s\.?\b|po\b)|\b\d{6}\b|\bpin\b|new delhi|delhi\s*[-–]?\s*\d|"
                           r"\bmarg\b|\bnagar\b|\bcolony\b|\benclave\b|\bvihar\b|\bpuri\b|\bpura\b|\bbagh\b|\bchowk\b|"
                           r"\broad\b|\blane\b|\bgurgaon\b|\bnoida\b|\bghaziabad\b|\bfaridabad\b", re.I)
RESIDENCE = re.compile(r"\s+(?:r/o|resident of|s/o|w/o|d/o|c/o)\b.*$", re.I)
LEAD_IN = re.compile(r"^(?:a\s+)?(?:complaint|compliant|representation|reference|letter|request|application)\s+(?:\(e-?mail\)\s+)?(?:of|from|by)\s+", re.I)


def clean_party(text):
    """A party's name (and, for a doctor, the hospital named with them) without postal addresses: chunks are kept up to the first
    that looks like an address."""
    text = RESIDENCE.sub("", squash(text))
    keep = []
    for chunk in (c.strip(" ,.;") for c in text.split(",")):
        if not chunk:
            continue
        if keep and ADDRESS_START.search(chunk):
            break
        keep.append(chunk)
    return ", ".join(keep)[:200]


def complainant_name(text):
    """The complainant as a name or short description: a person's name without their address, or for an official reference
    ("A Court Order dated ...", "A representation from ...") its first clause."""
    text = LEAD_IN.sub("", squash(text))
    first = re.split(r",\s*|\s+r/o\b|\s+c/o\b", text, maxsplit=1)[0].strip(" ,.;")
    return (first or text)[:150]


def split_respondents(text):
    """The respondents as separate parties: "Dr. X r/o ... and Y Hospital, Saket" -> ["Dr. X", "Y Hospital, Saket"]."""
    text = re.sub(r"^\s*by\s+", "", squash(text), flags=re.I)
    parts = [clean_party(p) for p in re.split(r"\s+and\s+|;|\s&\s", text)]
    return [p for p in parts if p]


def complaint_kind(text):
    """Who brought the complaint: patient_or_relative, court, police, government, council (the Council itself), representation."""
    t = text.lower()
    if re.search(r"suo.?moto|on its own", t):
        return "council"
    if re.search(r"\bcourt\b|hon.?ble|magistrate|tribunal|commission", t):
        return "court"
    if re.search(r"police|\bp\.?s\.?\b|\bfir\b|\bdcp\b|crime", t):
        return "police"
    if re.search(r"directorate|ministry|\bdhs\b|\bgovt|government|department|\bnmc\b|\bmci\b|\bcmo\b|\bdda\b|\bmcd\b|\bdm\b|board|authority|\bsdm\b", t):
        return "government"
    if re.search(r"representation|reference|referred", t):
        return "representation"
    return "patient_or_relative"


def case_year(row, name, received):
    lo, hi = PAGE_YEARS.get(name) or ((int(name[:4]) - 1, int(name[:4]) + 1) if name[:4].isdigit() else (MIN_YEAR, 9999))
    if received and lo <= int(received[:4]) <= hi:
        return int(received[:4])
    return max(int(name[:4]) if name[:4].isdigit() else MIN_YEAR, MIN_YEAR)


# ---------------------------------------------------------------------------------------------
# Building

def latest_pages(dmc):
    """{page name: (path, capture)} of the newest capture saved for each page."""
    pages = {}
    for name, path in dmc.saved_pages():
        pages[name] = (path, re.search(r"_(\d{14})\.html$", path).group(1))
    return pages


def order_files(state_seen):
    """{order link: path in the file store} for the orders downloaded."""
    return {url: v["saved_to"] for url, v in state_seen.items() if isinstance(v, dict) and v.get("kind") == "order"}


def build(today=None):
    today = today or date.today().isoformat()
    dmc = DMC()
    pages = latest_pages(dmc)
    if not pages:
        raise SystemExit("no saved status pages: run  python downloader.py --court dmc  first")
    seen = storage.load_state(COURT)["seen"]
    downloaded = order_files(seen)
    cases, used = {}, {}
    for name in sorted(pages, key=lambda n: (n[:4], n)):
        path, capture = pages[name]
        for row in read_page(name, path):
            number = complaint_number(row)
            if number is None:
                warn(f"page {name}: no complaint number in {row.get('no')!r} / {row.get('order')!r}; skipped")
                continue
            received = find_date(row.get("date", ""))
            year = case_year(row, name, received)
            who, respondent_text = row.get("complainant", ""), row.get("respondent", "")
            fingerprint = (re.sub(r"\W", "", respondent_text.lower())[:60], received)
            base = (number, year)
            if base in used and used[base] == fingerprint:  # the same complaint listed twice
                warn(f"duplicate of complaint {number}/{year} on page {name}; first kept")
                continue
            letter = ""
            if base in used:  # a different complaint with the same number and year
                n = sum(1 for k in cases if re.fullmatch(rf"COMP_{number}[A-H]?_{year}", k))  # 1 for the first, which has no letter
                letter = "ABCDEFGH"[min(n - 1, 7)]
                warn(f"complaint {number}/{year} appears twice with different details on page {name}; kept as {number}{letter}")
            used.setdefault(base, fingerprint)
            key = f"COMP_{number}{letter}_{year}"
            order_url = next((urljoin(PAGE.format(name=name), h) for h in row["links"]), "")
            has_order = bool(order_url) or bool(re.search(r"\d", row.get("order", "")))
            status, inferred = norm_status(row.get("status", ""), has_order)
            appeal = row.get("appeal", "")
            doc_path = downloaded.get(order_url, "")
            cases[key] = {
                "enquiry_id": f"DMC/COMP/{number}{letter}/{year}", "case_key": key, "complaint_no": number, "year": year,
                "received": received, "status": status, "status_text": row.get("status", ""), "status_inferred": inferred,
                "status_since": "", "complainants": [complainant_name(who)] if who else [],
                "complainant_kind": complaint_kind(who), "patient": row.get("patient", ""),
                "respondents": split_respondents(respondent_text), "appeal": appeal, "appeal_date": find_date(appeal),
                "order_label": row.get("order", ""), "order_url": order_url, "order_file": doc_path,
                "source_page": name, "captured_at": capture, "as_of": f"{capture[:4]}-{capture[4:6]}-{capture[6:8]}"}
    attach_orders(cases)
    return cases, today


def attach_orders(cases):
    """Read each downloaded order (dmc_orders.py) and record on the case what it decided: c["order"] is None if the order was not
    downloaded, {"file", "readable": False} if it could not be read (a scan), else also finding, mixed, sanctions, date, decision."""
    text_dir = os.path.join(DMC().pdf_dir, "orders_text")
    for c in cases.values():
        path = os.path.join(BASE_DIR, c["order_file"]) if c["order_file"] else ""
        if not path or not os.path.exists(path):
            c["order"] = None
            continue
        text = dmc_orders.order_text(path, text_dir)
        c["order"] = {"file": os.path.basename(path), "size": os.path.getsize(path), "readable": text is not None,
                      **(dmc_orders.classify(text) if text is not None else {})}
        if c["order"].get("date") and c["status"] in ("disposed", "dismissed"):
            c["status_since"] = c["order"]["date"]


def hearings_of(c, today):
    """The timeline of a complaint: received, and the appeal decision where it has a date."""
    out = []
    source = f"status_comp{c['source_page']} captured {c['captured_at']}"
    raw = "\n".join(filter(None, [f"[{source}]", f"complaint no: {c['complaint_no']}", f"received: {c['received'] or '(not given)'}",
                                  f"status: {c['status_text'] or '(blank)'}", f"order: {c['order_label'] or '(none)'}",
                                  f"appeal: {c['appeal']}" if c["appeal"] else ""]))
    note = c["appeal"] if c["appeal"] and not c["appeal_date"] else ""
    if c["received"]:
        out.append({"date": c["received"], "title": "Complaint received", "outcome": note, "tags": [], "raw": raw})
    order = c.get("order") or {}
    if order.get("date"):
        sanctions = f"Sanction: {', '.join(order['sanctions'])}. " if order.get("sanctions") else ""
        out.append({"date": order["date"], "title": "Order", "outcome": f"{sanctions}{order['decision']}", "tags": ["order"], "raw": raw})
    if c["appeal_date"]:
        out.append({"date": c["appeal_date"], "title": "Appeal", "outcome": c["appeal"], "tags": ["appeal"], "raw": raw})
    return out


def write_outputs(cases, today):
    status_rows, party_rows, timeline_rows, enquiries = [], [], [], {}
    for c in sorted(cases.values(), key=lambda c: (c["year"], c["complaint_no"], c["case_key"])):
        eid = c["enquiry_id"]
        status_rows.append([c["case_key"], eid, c["status"], c["status_since"], "", ""])
        for role, names in (("complainant", c["complainants"]), ("respondent", c["respondents"])):
            for n in names:
                party_rows.append([eid, n, party_type(n, role), role, c["as_of"], c["case_key"]])
        hearings = hearings_of(c, today)
        for h in hearings:
            timeline_rows.append([eid, h["date"], "", "", "", "yes" if h["date"] <= today else "no", ", ".join(h["tags"]), h["title"],
                                  h["outcome"], "", "", "", "", h["raw"], c["case_key"]])
        enquiries[eid] = {**c, "hearings": [{"date": h["date"], "purpose": h["title"], "outcome": h["outcome"]} for h in hearings]}
    storage.replace_analysis(COURT, enquiries, state=STATE, status=status_rows, parties=party_rows, timeline=timeline_rows)
    return status_rows, party_rows, timeline_rows


def store_judgements(cases):
    """The Council's final order in each case is its judgement: store its class in case_judgements, where the tracker keeps those of the
    consumer courts (judgements.py), so the case list and the Judgements panel show it. An order that cannot be read (a scan) is "scanned".
    Returns {class: count}."""
    counts = {}
    for c in cases.values():
        o = c.get("order")
        if not o:
            continue
        cls, reason = dmc_orders.judgement_class(o["finding"], o["mixed"]) if o["readable"] else ("scanned", "")
        storage.save_judgement(c["case_key"], file=o["file"], size=o["size"], reason=reason, version=dmc_orders.CLASSIFIER_VERSION, **{"class": cls})
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def main():
    cases, today = build()
    status_rows, party_rows, timeline_rows = write_outputs(cases, today)
    judgements = store_judgements(cases)
    by_status = {}
    for r in status_rows:
        by_status[r[2]] = by_status.get(r[2], 0) + 1
    print(f"{STATE}: {len(cases)} complaints, {len(party_rows)} parties, {len(timeline_rows)} timeline rows -> database ({COURT}); status: {by_status}")
    print(f"orders: {sum(1 for c in cases.values() if c['order'])} downloaded; judgements: {judgements}")
    for w in warnings:
        print("warning:", w, file=sys.stderr)


if __name__ == "__main__":
    main()
