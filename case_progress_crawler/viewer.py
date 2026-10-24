#!/usr/bin/env python3
"""Web UI over the CSVs written by analysis.py (read through storage.py).

  /                  all cases, newest year / highest sequence number first,
                     with status and collapsible links; ?year=YYYY filters by year
  /hearings/         days of hearings, latest first, with a short summary of each
                     case; ?year=YYYY filters
  /day/<date>        the cases heard on one day (YYYY-MM-DD), in full
  /case/<case_key>   parties and a timeline of hearings, latest first, plus
                     user-added links (related cases, party identities, ...)

The CSVs are re-read whenever their modification time changes, so re-running
analysis.py is picked up without restarting.

Usage:  python viewer.py [--host H] [--port P]
"""

import argparse
import re
from collections import defaultdict
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for

import storage

app = Flask(__name__)
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
    entries = load()["by_date"].get(date)
    if entries is None:
        abort(404)
    return render_template("day.html", date=date, entries=entries)


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
