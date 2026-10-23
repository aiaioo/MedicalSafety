#!/usr/bin/env python3
"""Web UI over the CSVs written by analysis.py.

  /                  all cases, newest year / highest sequence number first,
                     with status; ?year=YYYY filters by year
  /case/<case_key>   parties and a timeline of hearings, latest first

The CSVs are re-read whenever their modification time changes, so re-running
analysis.py is picked up without restarting.

Usage:  python viewer.py [--host H] [--port P] [--analysis-dir DIR]
"""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

from flask import Flask, abort, render_template, request

app = Flask(__name__)
app.config["ANALYSIS_DIR"] = Path(__file__).parent / "analysis"

_cache = {"mtimes": None, "data": None}


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _split_key(key):
    """'ENQ_108_2025' -> ('ENQ', (108, ''), 2025); 'EXP_98A_2021' -> ('EXP', (98, 'A'), 2021)"""
    kind, seq, year = key.rsplit("_", 2)
    num, suffix = re.fullmatch(r"(\d+)(.*)", seq).groups()
    return kind, (int(num), suffix), int(year)


def load():
    d = app.config["ANALYSIS_DIR"]
    files = [d / n for n in ("status.csv", "timeline.csv", "parties.csv")]
    mtimes = tuple(p.stat().st_mtime for p in files)
    if _cache["mtimes"] == mtimes:
        return _cache["data"]

    parties = defaultdict(list)
    for r in _read(files[2]):
        parties[r["case_key"]].append(r)
    hearings = defaultdict(list)
    for r in _read(files[1]):
        hearings[r["case_key"]].append(r)

    cases = {}
    for r in _read(files[0]):
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
    data = {"cases": cases, "ordered": ordered,
            "years": sorted({c["year"] for c in ordered}, reverse=True)}
    _cache.update(mtimes=mtimes, data=data)
    return data


@app.route("/")
def home():
    data = load()
    year = request.args.get("year", type=int)
    cases = [c for c in data["ordered"] if year is None or c["year"] == year]
    return render_template("home.html", cases=cases, years=data["years"], year=year)


@app.route("/case/<case_key>")
def case(case_key):
    c = load()["cases"].get(case_key)
    if c is None:
        abort(404)
    return render_template("case.html", c=c)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--analysis-dir", type=Path, default=app.config["ANALYSIS_DIR"])
    args = ap.parse_args()
    app.config["ANALYSIS_DIR"] = args.analysis_dir
    app.run(host=args.host, port=args.port)
