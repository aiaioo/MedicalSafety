#!/usr/bin/env python3
"""Web UI over the CSVs written by analysis.py (read through storage.py).

  /                  all cases, newest year / highest sequence number first,
                     with status and collapsible links; ?year=YYYY filters by year
  /hearings/         days of hearings, latest first, with a short summary of each
                     case; ?year=YYYY filters
  /day/<date>        the cases heard on one day (YYYY-MM-DD), in full
  /pdf/<kind>/<file> a hearing-list PDF in a continuous page viewer, with a download link
                     (/pdf/<kind>/<file>/download, pages as /pdf/<kind>/<file>/page/<n>.png)
  /case/<case_key>   parties and a timeline of hearings, latest first, plus
                     user-added links (related cases, party identities, ...)

The CSVs are re-read whenever their modification time changes, so re-running
analysis.py is picked up without restarting.

Usage:  python viewer.py [--host H] [--port P]
"""

import argparse
import datetime
import re
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse

import fitz  # PyMuPDF
from flask import Flask, Response, abort, redirect, render_template, request, send_from_directory, url_for

import storage

app = Flask(__name__)
PDF_DIR = Path(__file__).parent / "pdfs"
PDF_KINDS = {"future_hearings": "Future hearings list", "past_hearings": "Past hearings list"}
PAGE_IMAGE_DPI = 150
_cache = {"mtimes": None, "data": None}


def _split_key(key):
    """'ENQ_108_2025' -> ('ENQ', (108, ''), 2025); 'EXP_98A_2021' -> ('EXP', (98, 'A'), 2021)"""
    kind, seq, year = key.rsplit("_", 2)
    num, suffix = re.fullmatch(r"(\d+)(.*)", seq).groups()
    return kind, (int(num), suffix), int(year)


def load():
    mtimes = storage.mtimes("status.csv", "timeline.csv", "parties.csv")
    if _cache["mtimes"] == mtimes:
        return _cache["data"]

    parties = defaultdict(list)
    for r in storage.read_csv("parties.csv"):
        parties[r["case_key"]].append(r)
    hearings = defaultdict(list)
    for r in storage.read_csv("timeline.csv"):
        hearings[r["case_key"]].append(r)

    cases = {}
    for r in storage.read_csv("status.csv"):
        key = r["case_key"]
        kind, seq, year = _split_key(key)
        names = lambda role: [p["party"] for p in parties[key] if p["role"] == role]
        hs = sorted(hearings[key], key=lambda h: (h["hearing_date"], h["hearing_time"]), reverse=True)
        for newer, h in zip([None] + hs, hs):  # no date in the outcome text: fall back to the sitting that followed
            h["next_sitting"] = newer["hearing_date"] if newer and newer["hearing_date"] > h["hearing_date"] else ""
        for h, older in zip(hs, hs[1:] + [None]):  # the sitting before this one, if on a different day
            h["prev_sitting"] = older["hearing_date"] if older and older["hearing_date"] < h["hearing_date"] else ""
        cases[key] = {**r, "kind": kind, "seq": seq, "year": year,
                      "complainants": names("complainant"), "respondents": names("respondent"),
                      "hearings": hs, "last_hearing": hs[0]["hearing_date"] if hs else ""}
    ordered = sorted(cases.values(), key=lambda c: (c["year"], c["seq"], c["kind"]), reverse=True)
    by_date = defaultdict(list)  # hearing date -> [(case, hearing)], cases in list order
    for c in ordered:
        for h in c["hearings"]:
            by_date[h["hearing_date"]].append((c, h))
    data = {"cases": cases, "ordered": ordered, "by_date": by_date,
            "years": sorted({c["year"] for c in ordered}, reverse=True),
            "hearing_years": sorted({d[:4] for d in by_date}, reverse=True)}
    _cache.update(mtimes=mtimes, data=data)
    return data


@app.route("/")
def home():
    data = load()
    year = request.args.get("year", type=int)
    cases = [c for c in data["ordered"] if year is None or c["year"] == year]
    return render_template("home.html", cases=cases, years=data["years"], year=year, links=storage.load_links())


@app.route("/hearings/")
def hearings():
    data = load()
    year = request.args.get("year")
    days = [(d, data["by_date"][d]) for d in sorted(data["by_date"], reverse=True) if not year or d.startswith(year)]
    return render_template("hearings.html", days=days, years=data["hearing_years"], year=year)


@app.route("/day/<date>")
def day(date):
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        abort(404)
    entries = load()["by_date"].get(date, [])  # a future next date has no hearings yet
    return render_template("day.html", date=date, entries=entries, pdfs=pdfs_for_date(date))


def pdfs_for_date(date):
    """[(kind label, kind, [file names])] for KMC_Enquiry_<date>.pdf and its _2, _3... versions."""
    stamp = date.replace("-", "")
    pat = re.compile(rf"KMC_Enquiry_{stamp}(?:_(\d+))?\.pdf")
    found = []
    for kind, label in PDF_KINDS.items():
        names = sorted((f.name for f in (PDF_DIR / kind).glob("KMC_Enquiry_*.pdf") if pat.fullmatch(f.name)),
                       key=lambda n: int(pat.fullmatch(n).group(1) or 1))
        if names:
            found.append((label, kind, names))
    return found


def _pdf_path(kind, name):
    if kind not in PDF_KINDS or not re.fullmatch(r"KMC_Enquiry_\d{8}(_\d+)?\.pdf", name):
        abort(404)
    path = PDF_DIR / kind / name
    if not path.is_file():
        abort(404)
    return path


@app.route("/pdf/<kind>/<name>")
def pdf_view(kind, name):
    path = _pdf_path(kind, name)
    with fitz.open(path) as d:
        pages = d.page_count
    return render_template("pdf.html", kind=kind, label=PDF_KINDS[kind], name=name, pages=pages)


@app.route("/pdf/<kind>/<name>/download")
def pdf_download(kind, name):
    _pdf_path(kind, name)
    return send_from_directory(PDF_DIR / kind, name, as_attachment=True)


@app.route("/pdf/<kind>/<name>/page/<int:n>.png")
def pdf_page(kind, name, n):
    path = _pdf_path(kind, name)
    with fitz.open(path) as d:
        if not 1 <= n <= d.page_count:
            abort(404)
        png = d[n - 1].get_pixmap(matrix=fitz.Matrix(PAGE_IMAGE_DPI / 72, PAGE_IMAGE_DPI / 72)).tobytes("png")
    return Response(png, mimetype="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.route("/case/<case_key>")
def case(case_key):
    c = load()["cases"].get(case_key)
    if c is None:
        abort(404)
    return render_template("case.html", c=c, links=storage.load_links().get(case_key, []))


def _link_from_form():
    url = request.form.get("url", "").strip()
    if "://" not in url:
        url = "https://" + url
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        abort(400, "Link must be an http(s) URL")
    return {"url": url, "text": request.form.get("text", "").strip() or url,
            "note": request.form.get("note", "").strip()}


@app.post("/case/<case_key>/links")
def add_link(case_key):
    if case_key not in load()["cases"]:
        abort(404)
    link = _link_from_form()
    links = storage.load_links()
    links.setdefault(case_key, []).append(link)
    storage.save_links(links)
    return redirect(url_for("case", case_key=case_key) + "#links")


@app.post("/case/<case_key>/links/<int:i>/edit")
def edit_link(case_key, i):
    links = storage.load_links()
    if i >= len(links.get(case_key, [])):
        abort(404)
    links[case_key][i] = _link_from_form()
    storage.save_links(links)
    return redirect(url_for("case", case_key=case_key) + "#links")


@app.post("/case/<case_key>/links/<int:i>/delete")
def delete_link(case_key, i):
    links = storage.load_links()
    if i >= len(links.get(case_key, [])):
        abort(404)
    del links[case_key][i]
    if not links[case_key]:
        del links[case_key]
    storage.save_links(links)
    return redirect(url_for("case", case_key=case_key) + "#links")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    app.run(host=args.host, port=args.port)
