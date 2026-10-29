#!/usr/bin/env python3
"""Web UI over the CSVs written by analysis.py (read through storage.py).

  /                  all cases, newest year / highest sequence number first,
                     with status and collapsible links; ?year=YYYY filters by year
                     ?q= searches by party, case number or hospital (with autocomplete from /suggest)
                     ; on /hearings/ the results are the matching hearing dates
  /hospitals/        hospitals named among the respondents, with case counts
  /hospital/<slug>   the cases against one hospital
  /hearings/         days of hearings, latest first, with a short summary of each
                     case; ?year=YYYY filters
  /day/<date>        the cases heard on one day (YYYY-MM-DD), in full
  /pdf/<kind>/<file> a pre- or post-hearing PDF in a continuous page viewer, with a download link
                     (/pdf/<kind>/<file>/download, pages as /pdf/<kind>/<file>/page/<n>.png)
  /case/<case_key>   parties and a timeline of hearings, latest first, plus
                     user-added links (related cases, party identities, ...)

The CSVs are re-read whenever their modification time changes, so re-running
analysis.py is picked up without restarting.

Shows KMC cases (from analysis.py), CPA -- Consumer Protection Act -- cases from all the consumer courts
(from analysis_consumer.py), or both together; choose with the links at the top of every page (remembered
in a cookie). --court sets the choice for visitors who have not made one.

Usage:  python viewer.py [--court kmc|consumer|all] [--host H] [--port P]
"""

import argparse
import datetime
import re
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse

import fitz  # PyMuPDF
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

import storage

app = Flask(__name__)
COURTS = {"kmc": "KMC", "consumer": "CPA"}  # court id -> short label
SELECTIONS = {"kmc": "KMC", "consumer": "CPA (consumer courts)", "all": "Both"}  # what the selector offers
DEFAULT_SELECTION = "all"  # for visitors with no choice yet; set by --court
LOAD_FILES = ("status.csv", "timeline.csv", "parties.csv")
PDF_COURT = "kmc"  # only the KMC has original PDFs
PDF_DIR = Path(__file__).parent / "pdfs"
PDF_KINDS = {"pre_hearing": "Pre-hearing PDF", "post_hearing": "Post-hearing PDF"}
PAGE_IMAGE_DPI = 150
_cache = {}  # selection -> {"mtimes", "data"}
HOSPITAL_WORD = re.compile(r"\b(?:hospitals?|nursing homes?|clinics?)\b", re.I)
HOSPITAL_SPLIT = re.compile(r"[,;–—/]|\bm\s*/\s*s\b\.?|\bc\s*/\s*o\b\.?|\b(?:of|at|and)\b|\(|\)", re.I)
HOSPITAL_TITLE = re.compile(r"^(?:the\b|dr\b\.?|(?:medical|managing|unit|chief|executive)?\s*"
                            r"(?:administrator|administartor|superintendent|directors?|owners?|head|management|ceo|md)\b|[\s/.,&-])+", re.I)


def hospital_names(party):
    """Hospital/clinic/nursing-home names mentioned in a party string, e.g.
    'Dr.Tarun Patil, C/o Manipal Hospitals, Bangalore.' -> ['Manipal Hospitals']"""
    names = []
    for seg in HOSPITAL_SPLIT.split(party):
        m = HOSPITAL_WORD.search(seg)
        if not m:
            continue
        name = HOSPITAL_TITLE.sub("", seg[:m.end()]).strip(" .-")
        if len(name.split()) > 1:  # a bare "Hospital" names nothing
            names.append(squash(name))
    return names


# Variants whose spelling is too far apart to normalise by rule, or that carry a stray person's name
HOSPITAL_ALIASES = {
    "bhagwan-mahaveer-jain-hospital": "bhagawan-mahaveer-jain-hospital",
    "bhagavanmahaveer-jain-hospital": "bhagawan-mahaveer-jain-hospital",
    "rajarajeswari-medical-college-hospital": "rajarajeshwari-medical-college-hospital",
    "cloudenine-fertility-clodnine-hospital": "cloudnine-hospital",
    "clodnine-hospital": "cloudnine-hospital",
    "poornima-m-gowda-cloudnine-hospital": "cloudnine-hospital",
    "motherhood-women-children-s-hospital": "motherhood-hospital",
    "narayananethralaya-eye-hospital": "narayana-nethralaya-eye-hospital",
    "bgs-gleneagles-global-hospital": "gleneagles-global-hospital",
    "bowring-lady-curzon-hospital": "lady-curzon-hospital",
    "child-hospital": "children-hospital",
    "ramaiah-leena-hospital": "leena-ramaiah-hospital",
    "ms-ramaiah-memorial-hospital": "ramaiah-memorial-hospital",
    "sumeet-jaiswal-arka-hospital": "arka-hospital",
    "sudhamani-jayanagar-general-hospital": "jayanagar-general-hospital",
    "sowmya-dinesh-hr-ssm-hospital": "ssm-hospital",
    "satish-l-sparsh-super-speciality-hospital": "sparsh-hospital",
    "sinam-inaoton-singha-st-martha-s-hospital": "st-martha-s-hospital",
}


# Display names for aliased groups where no member is spelled cleanly
HOSPITAL_DISPLAY = {"arka-hospital": "Arka Hospital", "ssm-hospital": "SSM Hospital",
                    "children-hospital": "Children Hospital", "jayanagar-general-hospital": "Jayanagar General Hospital",
                    "st-martha-s-hospital": "St. Martha’s Hospital"}


def hospital_key(name):
    """Slug that merges spelling variants: 'Manipal Hospital' and 'M/s. Manipal Hospitals' share one."""
    s = re.sub(r"^belongs to\W*", "", name.lower())
    s = re.sub(r"\bms/\.", "", s)  # a mangled "M/s."
    s = re.sub(r"\b(?:[a-z]\.\s*){2,}", lambda m: re.sub(r"\W", "", m.group()) + " ", s)  # "K.I.M.S." -> "kims"
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\bhospitals\b", "hospital", s)
    s = re.sub(r"\b(?:limited|ltd|pvt|private)\b", "", s)
    s = re.sub(r"\bmedical college\s+(?:and\s+)?hospital\b", "medical college hospital", s)
    slug = "-".join(s.split())
    return HOSPITAL_ALIASES.get(slug, slug)


def squash(s):
    return re.sub(r"\s+", " ", s).strip()


def id_tokens(s):
    """'KMC/ENQ/01/2017' -> ['kmc', 'enq', '1', '2017']"""
    return [t.lstrip("0") or "0" if t.isdigit() else t for t in re.findall(r"[a-z]+|\d+", s.lower())]


def _split_key(key):
    """'ENQ_108_2025' -> ('ENQ', (108, ''), 2025); 'EXP_98A_2021' -> ('EXP', (98, 'A'), 2021)"""
    kind, seq, year = key.rsplit("_", 2)
    num, suffix = re.fullmatch(r"(\d+)(.*)", seq).groups()
    return kind, (int(num), suffix), int(year)


def selection():
    sel = request.cookies.get("court")
    return sel if sel in SELECTIONS else DEFAULT_SELECTION


def court_ids(sel):
    """The courts a selection covers that have been analysed (a court with no data yet is skipped)."""
    ids = list(COURTS) if sel == "all" else [sel]
    return [c for c in ids if all(storage.path(c, n).exists() for n in LOAD_FILES)]


def load(sel=None):
    sel = sel or selection()
    ids = court_ids(sel)
    mtimes = tuple(storage.mtimes(c, *LOAD_FILES) for c in ids)
    if sel in _cache and _cache[sel]["mtimes"] == mtimes:
        return _cache[sel]["data"]

    parties = defaultdict(list)
    hearings = defaultdict(list)
    status_rows = []  # case keys are distinct across courts
    for c in ids:
        for r in storage.read_csv(c, "parties.csv"):
            parties[r["case_key"]].append(r)
        for r in storage.read_csv(c, "timeline.csv"):
            hearings[r["case_key"]].append(r)
        status_rows += [{**r, "court": c, "court_label": COURTS[c]} for r in storage.read_csv(c, "status.csv")]

    cases = {}
    for r in status_rows:
        key = r["case_key"]
        kind, seq, year = _split_key(key)
        names = lambda role: [p["party"] for p in parties[key] if p["role"] == role]
        hs = sorted(hearings[key], key=lambda h: (h["hearing_date"], h["hearing_time"]), reverse=True)
        for newer, h in zip([None] + hs, hs):  # no date in the outcome text: fall back to the sitting that followed
            h["next_sitting"] = newer["hearing_date"] if newer and newer["hearing_date"] > h["hearing_date"] else ""
        for h, older in zip(hs, hs[1:] + [None]):  # the sitting before this one, if on a different day
            h["prev_sitting"] = older["hearing_date"] if older and older["hearing_date"] < h["hearing_date"] else ""
        hospitals = {}
        for pr in parties[key]:
            if pr["role"] == "respondent":
                for n in hospital_names(pr["party"]):
                    hospitals.setdefault(hospital_key(n), n)
        cases[key] = {**r, "hospitals": hospitals, "kind": kind, "seq": seq, "year": year,
                      "complainants": names("complainant"), "respondents": names("respondent"),
                      "hearings": hs, "last_hearing": hs[0]["hearing_date"] if hs else ""}
    ordered = sorted(cases.values(), key=lambda c: (c["year"], c["seq"], c["kind"]), reverse=True)
    by_date = defaultdict(list)  # hearing date -> [(case, hearing)], cases in list order
    for c in ordered:
        for h in c["hearings"]:
            by_date[h["hearing_date"]].append((c, h))
    hospitals = defaultdict(lambda: {"names": defaultdict(int), "cases": []})
    for c in ordered:
        for slug, n in c["hospitals"].items():
            hospitals[slug]["names"][n] += 1
            hospitals[slug]["cases"].append(c)
    hospitals = {s: {"slug": s, "name": HOSPITAL_DISPLAY.get(s) or max(h["names"], key=lambda n: (hospital_key(n) == s, h["names"][n], -len(n))), "cases": h["cases"]}
                 for s, h in hospitals.items() if s}
    data = {"cases": cases, "hospitals": hospitals, "ordered": ordered, "by_date": by_date,
            "years": sorted({c["year"] for c in ordered}, reverse=True),
            "hearing_years": sorted({d[:4] for d in by_date}, reverse=True)}
    _cache[sel] = {"mtimes": mtimes, "data": data}
    return data


def court_links(sel=None):
    """Hand-entered links of the selected courts, by case key."""
    links = {}
    for c in court_ids(sel or selection()):
        links.update(storage.load_links(c))
    return links


def court_of(case_key):
    """Which court a case belongs to, or 404."""
    c = load("all")["cases"].get(case_key)
    if c is None:
        abort(404)
    return c["court"]


@app.context_processor
def selector():
    return {"selections": SELECTIONS, "selected": selection()}


@app.route("/court/<sel>")
def select_court(sel):
    if sel not in SELECTIONS:
        abort(404)
    ref = urlparse(request.referrer or "")
    # back to the list the visitor was on; a case or hospital page may not exist under the new choice
    back = ref.path + ("?" + ref.query if ref.query else "") if ref.path in ("/", "/hearings/", "/hospitals/") else url_for("home")
    resp = redirect(back)
    resp.set_cookie("court", sel, max_age=365 * 86400, samesite="Lax")
    return resp


def matches(c, q):
    """Does a case match a search by party, case number or hospital?"""
    words = q.lower().split()
    haystack = " ".join(c["complainants"] + c["respondents"] + list(c["hospitals"].values())).lower()
    if all(w in haystack for w in words):
        return True
    ids = id_tokens(c["enquiry_id"]) + id_tokens(c["case_key"])
    return all(any(t.startswith(w) for t in ids) for w in id_tokens(q))


@app.route("/")
def home():
    data = load()
    year = request.args.get("year", type=int)
    q = request.args.get("q", "").strip()
    cases = [c for c in data["ordered"] if (year is None or c["year"] == year) and (not q or matches(c, q))]
    return render_template("home.html", cases=cases, years=data["years"], year=year, q=q, links=court_links())


@app.route("/suggest")
def suggest():
    """Autocomplete: up to 12 party names, hospital names and case numbers containing the query."""
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify([])
    data = load()
    pool = {c["enquiry_id"] for c in data["ordered"]}
    pool |= {p for c in data["ordered"] for p in c["complainants"] + c["respondents"]}
    pool |= {h["name"] for h in data["hospitals"].values()}
    hits = sorted((s for s in pool if q in s.lower()), key=lambda s: (not s.lower().startswith(q), len(s), s))
    return jsonify(hits[:12])


@app.route("/hospitals/")
def hospitals():
    hs = sorted(load()["hospitals"].values(), key=lambda h: (-len(h["cases"]), h["name"].lower()))
    return render_template("hospitals.html", hospitals=hs)


@app.route("/hospital/<slug>")
def hospital(slug):
    h = load()["hospitals"].get(slug)
    if h is None:
        abort(404)
    return render_template("hospital.html", h=h, links=court_links())


@app.route("/hearings/")
def hearings():
    data = load()
    year = request.args.get("year")
    q = request.args.get("q", "").strip()
    days = []
    for d in sorted(data["by_date"], reverse=True):
        if year and not d.startswith(year):
            continue
        entries = [(c, h) for c, h in data["by_date"][d] if not q or matches(c, q)]
        if entries:
            days.append((d, entries))
    return render_template("hearings.html", days=days, years=data["hearing_years"], year=year, q=q)


@app.route("/day/<date>")
def day(date):
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        abort(404)
    entries = load()["by_date"].get(date, [])  # a future next date has no hearings yet
    return render_template("day.html", date=date, entries=entries,
                           pdfs=pdfs_for_date(date) if PDF_COURT in court_ids(selection()) else [])


def pdfs_for_date(date):
    """[(kind label, kind, [file names])] for KMC_Enquiry_<date>.pdf and its _2, _3... versions."""
    stamp = date.replace("-", "")
    pat = re.compile(rf"KMC_Enquiry_{stamp}(?:_(\d+))?\.pdf")
    found = []
    for kind, label in PDF_KINDS.items():
        names = sorted((f.name for f in (PDF_DIR / PDF_COURT / kind).glob("KMC_Enquiry_*.pdf") if pat.fullmatch(f.name)),
                       key=lambda n: int(pat.fullmatch(n).group(1) or 1))
        if names:
            found.append((label, kind, names))
    return found


def _pdf_path(kind, name):
    if kind not in PDF_KINDS or not re.fullmatch(r"KMC_Enquiry_\d{8}(_\d+)?\.pdf", name):
        abort(404)
    path = PDF_DIR / PDF_COURT / kind / name
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
    return send_from_directory(PDF_DIR / PDF_COURT / kind, name, as_attachment=True)


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
    return render_template("case.html", c=c, links=court_links().get(case_key, []))


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
    court = court_of(case_key)
    links = storage.load_links(court)
    links.setdefault(case_key, []).append(link)
    storage.save_links(court, links)
    return redirect(url_for("case", case_key=case_key) + "#links")


@app.post("/case/<case_key>/links/<int:i>/edit")
def edit_link(case_key, i):
    court = court_of(case_key)
    links = storage.load_links(court)
    if i >= len(links.get(case_key, [])):
        abort(404)
    links[case_key][i] = _link_from_form()
    storage.save_links(court, links)
    return redirect(url_for("case", case_key=case_key) + "#links")


@app.post("/case/<case_key>/links/<int:i>/delete")
def delete_link(case_key, i):
    court = court_of(case_key)
    links = storage.load_links(court)
    if i >= len(links.get(case_key, [])):
        abort(404)
    del links[case_key][i]
    if not links[case_key]:
        del links[case_key]
    storage.save_links(court, links)
    return redirect(url_for("case", case_key=case_key) + "#links")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--court", default=DEFAULT_SELECTION, choices=sorted(SELECTIONS))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    DEFAULT_SELECTION = args.court
    app.run(host=args.host, port=args.port)
