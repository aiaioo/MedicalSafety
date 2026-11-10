#!/usr/bin/env python3
"""Web UI over the analysis written by analysis.py (read through storage.py).

  /                  all cases, newest year / highest sequence number first,
                     with status and collapsible links; ?year=YYYY filters by year
                     ?q= searches by party, case number, hospital or advocate (with autocomplete from /suggest)
                     ; on /hearings/ the results are the matching hearing dates
  /hospitals/        hospitals named among the respondents, with case counts
  /hospital/<slug>   the cases against one hospital
  /advocates/        advocates named in the consumer cases (cause lists and case details), with case counts
  /advocate/<slug>   the cases one advocate appeared in, and for which side
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

Consumer cases that case_details.py labelled not_medical (a property dispute, a bank v. a doctor ...; the label is
in the analysis status) are hidden from the lists, hospital counts and search, and shown with a "not medical" badge if the
"show non-medical" link at the top is on (a cookie). A case page opened directly always shows. Cases labelled
"unclear" are shown, with a badge.

Usage:  python viewer.py [--court kmc|consumer|all] [--host H] [--port P]
"""

import argparse
import datetime
import re
from html import unescape
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse

import fitz  # PyMuPDF
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

import storage
from courts.providers import MATCHER
from advocates import advocate_display, advocate_key, advocate_names
from analysis_consumer import ist_date
from courts.ejagriti_case import history_hearing_dates

app = Flask(__name__)
COURTS = {"kmc": "KMC", "consumer": "CPA"}  # court id -> short label
SELECTIONS = {"kmc": "KMC", "consumer": "CPA (consumer courts)", "all": "Both"}  # what the selector offers
DEFAULT_SELECTION = "all"  # for visitors with no choice yet; set by --court
PDF_COURT = "kmc"  # only the KMC has original PDFs
PDF_DIR = Path(__file__).parent / "pdfs"
PDF_KINDS = {"pre_hearing": "Pre-hearing PDF", "post_hearing": "Post-hearing PDF"}
PAGE_IMAGE_DPI = 150
_cache = {}  # selection -> {"mtimes", "data"}
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


def show_non_medical():
    return request.cookies.get("non_medical") == "show"


def visible(c):
    """Is this case listed? Cases labelled not_medical are hidden unless the visitor asked for them."""
    return c.get("label") != "not_medical" or show_non_medical()


def court_ids(sel):
    """The courts a selection covers that have been analysed (a court with no data yet is skipped)."""
    ids = list(COURTS) if sel == "all" else [sel]
    return [c for c in ids if storage.has_analysis(c)]


BLANK_HEARING = {"hearing_time": "", "list": "", "held": "", "purpose_tags": "", "purpose_text": "", "outcome_text": "",
                 "next_date": "", "next_sitting": "", "prev_sitting": "", "postponed_from": "", "raw_text": "",
                 "next_stage_tags": "", "cross_examination_of": ""}


def history_of(raw):
    """{hearing date: {"label", "judges", "next"}} for the held hearings of a case's portal history (its history document)."""
    found = {}
    for e in raw or []:
        if e.get("dateOfAction") and (e.get("actionTaken") or "").startswith("Hearing"):
            nxt = e.get("dateOfNextHearing") or ""
            found[history_hearing_dates([e])[0]] = {
                "label": e["actionTaken"], "judges": (e.get("judgesName") or "").replace(",", ", "),
                "next": history_hearing_dates([{"dateOfAction": nxt, "actionTaken": "Hearing"}])[0] if nxt else ""}
    return found


def with_history(raw_history, status, hearings):
    """The cause-list hearings of a case plus one entry (from_history) for every hearing of the portal's history
    not on a cause list -- the history reaches back before the cause lists were crawled. Every entry gets its
    history (label, bench, next date) and the court's record of the hearing."""
    history, record = history_of(raw_history), proceedings_of(status)
    out = [{**h, "history": history.get(h["hearing_date"]), "record": record.get(h["hearing_date"], "")} for h in hearings]
    known = {h["hearing_date"] for h in out}
    out += [{**BLANK_HEARING, "hearing_date": d, "history": history.get(d), "record": record.get(d, ""), "from_history": True}
            for d in sorted(set(history) - known)]
    return out


def case_advocates(rows):
    """{slug: {"name", "roles"}} for one case's advocates rows; roles are the sides the advocate appeared for."""
    found = {}
    for r in rows:
        for n in advocate_names(r["advocate"]):
            a = found.setdefault(advocate_key(n), {"names": defaultdict(int), "roles": set()})
            a["names"][n] += 1
            a["roles"].add(r["role"])
    return found


def load(sel=None):
    sel = sel or selection()
    ids = court_ids(sel)
    mtimes = tuple(storage.analysis_version(c) for c in ids) + (storage.case_documents_version(),)
    if sel in _cache and _cache[sel]["mtimes"] == mtimes:
        return _cache[sel]["data"]

    parties = defaultdict(list)
    advocate_rows = defaultdict(list)
    hearings = defaultdict(list)
    status_rows = []  # case keys are distinct across courts
    histories, statuses = storage.load_case_documents("history.json"), storage.load_case_documents("status.json")
    for c in ids:
        analysis = storage.load_analysis(c)
        for r in analysis["parties"]:
            parties[r["case_key"]].append(r)
        for r in analysis["advocates"]:
            advocate_rows[r["case_key"]].append(r)
        for r in analysis["timeline"]:
            r["hearing_date"] = ist_date(r["hearing_date"])  # older data holds the portal's UTC timestamps
            hearings[r["case_key"]].append(r)
        status_rows += [{**r, "court": c, "court_label": COURTS[c]} for r in analysis["status"]]

    cases = {}
    for r in status_rows:
        key = r["case_key"]
        kind, seq, year = _split_key(key)
        names = lambda role: [p["party"] for p in parties[key] if p["role"] == role]
        hs = sorted(with_history(histories.get(key), statuses.get(key), hearings[key]), key=lambda h: (h["hearing_date"], h["hearing_time"]), reverse=True)
        for newer, h in zip([None] + hs, hs):  # no date in the outcome text: fall back to the sitting that followed
            h["next_sitting"] = newer["hearing_date"] if newer and newer["hearing_date"] > h["hearing_date"] else ""
        for h, older in zip(hs, hs[1:] + [None]):  # the sitting before this one, if on a different day
            h["prev_sitting"] = older["hearing_date"] if older and older["hearing_date"] < h["hearing_date"] else ""
        hospitals = {}
        for pr in parties[key]:
            if pr["role"] == "respondent":
                for n in MATCHER.hospital_names(pr["party"]):
                    hospitals.setdefault(hospital_key(n), n)
        provider = MATCHER.provider_side(names("complainant"), names("respondent"))
        cases[key] = {**r, "hospitals": hospitals, "provider_side": provider, "advocate_info": case_advocates(advocate_rows[key]), "kind": kind, "seq": seq, "year": year,
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
    advocates = defaultdict(lambda: {"names": defaultdict(int), "cases": [], "sides": {}})
    for c in ordered:
        for slug, a in c["advocate_info"].items():
            adv = advocates[slug]
            for n, k in a["names"].items():
                adv["names"][n] += k
            adv["cases"].append(c)
            adv["sides"][c["case_key"]] = sorted(a["roles"])
        c["advocate_links"] = {role: sorted(((slug, advocate_display(a["names"])) for slug, a in c["advocate_info"].items()
                                             if role in a["roles"] and slug), key=lambda x: x[1].lower())
                               for role in ("complainant", "respondent")}
        c["advocates"] = {role: [n for _, n in links] for role, links in c["advocate_links"].items()}
    advocates = {s: {"slug": s, "name": advocate_display(a["names"]), "cases": a["cases"], "sides": a["sides"]}
                 for s, a in advocates.items() if s}
    data = {"cases": cases, "hospitals": hospitals, "advocates": advocates, "ordered": ordered, "by_date": by_date,
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
    data = load()
    hidden = sum(1 for c in data["ordered"] if c.get("label") == "not_medical")
    return {"selections": SELECTIONS, "selected": selection(), "non_medical_count": hidden,
            "show_non_medical": show_non_medical()}


@app.route("/non-medical/<choice>")
def non_medical(choice):
    if choice not in ("show", "hide"):
        abort(404)
    ref = urlparse(request.referrer or "")
    back = ref.path + ("?" + ref.query if ref.query else "") if ref.path in ("/", "/hearings/", "/hospitals/", "/advocates/") or ref.path.startswith(("/day/", "/hospital/", "/advocate/")) else url_for("home")
    resp = redirect(back)
    resp.set_cookie("non_medical", choice, max_age=365 * 86400, samesite="Lax")
    return resp


@app.route("/court/<sel>")
def select_court(sel):
    if sel not in SELECTIONS:
        abort(404)
    ref = urlparse(request.referrer or "")
    # back to the list the visitor was on; a case or hospital page may not exist under the new choice
    back = ref.path + ("?" + ref.query if ref.query else "") if ref.path in ("/", "/hearings/", "/hospitals/", "/advocates/") else url_for("home")
    resp = redirect(back)
    resp.set_cookie("court", sel, max_age=365 * 86400, samesite="Lax")
    return resp


def matches(c, q):
    """Does a case match a search by party, case number or hospital?"""
    words = q.lower().split()
    haystack = " ".join(c["complainants"] + c["respondents"] + list(c["hospitals"].values())
                        + c["advocates"]["complainant"] + c["advocates"]["respondent"]).lower()
    if all(w in haystack for w in words):
        return True
    ids = id_tokens(c["enquiry_id"]) + id_tokens(c["case_key"])
    return all(any(t.startswith(w) for t in ids) for w in id_tokens(q))


@app.route("/")
def home():
    data = load()
    year = request.args.get("year", type=int)
    q = request.args.get("q", "").strip()
    cases = [c for c in data["ordered"] if visible(c) and (year is None or c["year"] == year) and (not q or matches(c, q))]
    return render_template("home.html", cases=cases, years=data["years"], year=year, q=q, links=court_links())


@app.route("/suggest")
def suggest():
    """Autocomplete: up to 12 party, hospital and advocate names and case numbers containing the query."""
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify([])
    data = load()
    shown = [c for c in data["ordered"] if visible(c)]
    pool = {c["enquiry_id"] for c in shown}
    pool |= {p for c in shown for p in c["complainants"] + c["respondents"]}
    pool |= {h["name"] for h in visible_hospitals(data)}
    pool |= {a["name"] for a in visible_advocates(data)}
    hits = sorted((s for s in pool if q in s.lower()), key=lambda s: (not s.lower().startswith(q), len(s), s))
    return jsonify(hits[:12])


def visible_hospitals(data):
    """The hospitals with their visible cases only (a hospital left with none is dropped)."""
    out = []
    for h in data["hospitals"].values():
        cases = [c for c in h["cases"] if visible(c)]
        if cases:
            out.append({**h, "cases": cases})
    return out


@app.route("/hospitals/")
def hospitals():
    hs = sorted(visible_hospitals(load()), key=lambda h: (-len(h["cases"]), h["name"].lower()))
    return render_template("hospitals.html", hospitals=hs)


@app.route("/hospital/<slug>")
def hospital(slug):
    h = load()["hospitals"].get(slug)
    if h is None:
        abort(404)
    return render_template("hospital.html", h={**h, "cases": [c for c in h["cases"] if visible(c)]}, links=court_links())


def represents(case, role):
    """Whom an advocate on `role`'s side of the case acts for: 'hospital', 'patient', 'insurer', 'organisation',
    'mixed' or '' (see ProviderMatcher.party_of)."""
    return MATCHER.party_of(case["complainants"], case["respondents"], role)


@app.context_processor
def helpers():
    return {"represents": represents}


def visible_advocates(data):
    """The advocates with their visible cases only (an advocate left with none is dropped), and the count of
    those cases for the complainant side and for the respondent side."""
    out = []
    for a in data["advocates"].values():
        cases = [c for c in a["cases"] if visible(c)]
        if cases:
            sides = [a["sides"][c["case_key"]] for c in cases]
            out.append({**a, "cases": cases, "complainant": sum("complainant" in s for s in sides),
                        "respondent": sum("respondent" in s for s in sides)})
            roles = [(c, r) for c in cases for r in a["sides"][c["case_key"]]]
            out[-1]["patient"] = sum(represents(c, r) == "patient" for c, r in roles)
            out[-1]["hospital"] = sum(represents(c, r) == "hospital" for c, r in roles)
            out[-1]["other_org"] = sum(represents(c, r) in ("insurer", "organisation") for c, r in roles)
    return out


@app.route("/advocates/")
def advocates():
    q = request.args.get("q", "").strip().lower()
    advs = [a for a in visible_advocates(load()) if not q or q in a["name"].lower()]
    advs.sort(key=lambda a: (-len(a["cases"]), a["name"].lower()))
    return render_template("advocates.html", advocates=advs, q=q)


@app.route("/advocate/<slug>")
def advocate(slug):
    a = load()["advocates"].get(slug)
    if a is None:
        abort(404)
    cases = [c for c in a["cases"] if visible(c)]
    return render_template("advocate.html", a={**a, "cases": cases}, links=court_links(),
                           complainant=sum("complainant" in a["sides"][c["case_key"]] for c in cases),
                           respondent=sum("respondent" in a["sides"][c["case_key"]] for c in cases),
                           patient=sum(represents(c, r) == "patient" for c in cases for r in a["sides"][c["case_key"]]),
                           hospital=sum(represents(c, r) == "hospital" for c in cases for r in a["sides"][c["case_key"]]),
                           other_org=sum(represents(c, r) in ("insurer", "organisation") for c in cases for r in a["sides"][c["case_key"]]))


@app.route("/hearings/")
def hearings():
    data = load()
    year = request.args.get("year")
    q = request.args.get("q", "").strip()
    days = []
    for d in sorted(data["by_date"], reverse=True):
        if year and not d.startswith(year):
            continue
        entries = [(c, h) for c, h in data["by_date"][d] if visible(c) and (not q or matches(c, q))]
        if entries:
            days.append((d, entries))
    return render_template("hearings.html", days=days, years=data["hearing_years"], year=year, q=q)


@app.route("/day/<date>")
def day(date):
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        abort(404)
    entries = [(c, h) for c, h in load()["by_date"].get(date, []) if visible(c)]  # a future next date has no hearings yet
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


ORDER_RE = re.compile(r"(?P<date>[0-9-]+)_(?P<kind>daily_order|judgement)(?:_v(?P<v>\d+))?\.(?P<ext>pdf|html)")
ORDER_KINDS = {"daily_order": "Daily order", "judgement": "Judgement"}
_text_cache = {}  # (path, mtime) -> extracted text


def _iso_date(s):
    """The portal's date as YYYY-MM-DD (it may come as DD-MM-YYYY); unchanged if unrecognised."""
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", s)
    return f"{m[3]}-{m[2]}-{m[1]}" if m else s


def order_text(path):
    """The text of a saved order, PDF or HTML ('' if a scanned PDF has no text layer)."""
    key = (str(path), path.stat().st_mtime)
    if key not in _text_cache:
        if path.suffix == ".pdf":
            with fitz.open(path) as d:
                text = "\n".join(p.get_text() for p in d)
        else:
            html = path.read_text(encoding="utf-8", errors="replace")
            html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
            html = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", html)
            text = unescape(re.sub(r"<[^>]+>", " ", html))
            text = "\n".join(" ".join(line.split()) for line in text.splitlines())
        _text_cache[key] = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _text_cache[key]


def orders_of(case_key):
    """{hearing date: [order]} for a case's saved orders, oldest version first; an order is
    {"date", "name", "label", "ext", "text"}."""
    found = defaultdict(list)
    for p in storage.order_files(case_key):
        m = ORDER_RE.fullmatch(p.name)
        if m:
            label = ORDER_KINDS[m["kind"]] + (f" (version {m['v']})" if m["v"] else "")
            try:
                text = order_text(p)
            except Exception:  # an unreadable file still gets its link
                text = ""
            found[_iso_date(m["date"])].append({"date": _iso_date(m["date"]), "name": p.name, "label": label, "ext": m["ext"], "text": text})
    for lst in found.values():
        lst.sort(key=lambda o: (o["label"].split()[0], int(re.search(r"\d+", o["label"]).group()) if "version" in o["label"] else 1))
    return found


@app.route("/order/<case_key>/<name>")
def order_file(case_key, name):
    """A saved order; HTML orders come from the court's portal, so they are sandboxed (no scripts)."""
    if not ORDER_RE.fullmatch(name) or not re.fullmatch(r"[\w-]+", case_key):
        abort(404)
    resp = send_from_directory(storage.order_dir(case_key), name)
    if name.endswith(".html"):  # not on PDFs: a sandbox stops the browser's PDF viewer
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    return resp


def proceedings_of(status):
    """{hearing date: text} of what the court recorded for each hearing (proceedingText in the case's portal status
    document), as plain text; hearings with nothing recorded are left out."""
    found = {}
    for h in (status or {}).get("caseHearingDetails") or []:
        html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", h.get("proceedingText") or "")
        html = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", html)
        text = unescape(re.sub(r"<[^>]+>", " ", html))
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(" ".join(line.split()) for line in text.splitlines())).strip()
        if h.get("dateOfHearing") and text:
            found[h["dateOfHearing"]] = text
    return found


@app.route("/case/<case_key>")
def case(case_key):
    c = load()["cases"].get(case_key)
    if c is None:
        abort(404)
    # The hearings (from the cause lists and the portal's history, see with_history) are the timeline. An order
    # dated on a day absent from both still gets an entry of its own (from_order).
    orders = orders_of(case_key)
    known = {h["hearing_date"] for h in c["hearings"]}
    hearings = c["hearings"] + [{**BLANK_HEARING, "hearing_date": d, "history": None, "record": "", "from_order": True}
                                for d in orders if d not in known]
    hearings.sort(key=lambda h: (h["hearing_date"], h["hearing_time"]), reverse=True)
    return render_template("case.html", c=c, hearings=hearings, links=court_links().get(case_key, []), orders=orders)


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
    storage.save_case_links(court, case_key, storage.load_links(court).get(case_key, []) + [link])
    return redirect(url_for("case", case_key=case_key) + "#links")


@app.post("/case/<case_key>/links/<int:i>/edit")
def edit_link(case_key, i):
    court = court_of(case_key)
    links = storage.load_links(court).get(case_key, [])
    if i >= len(links):
        abort(404)
    links[i] = _link_from_form()
    storage.save_case_links(court, case_key, links)
    return redirect(url_for("case", case_key=case_key) + "#links")


@app.post("/case/<case_key>/links/<int:i>/delete")
def delete_link(case_key, i):
    court = court_of(case_key)
    links = storage.load_links(court).get(case_key, [])
    if i >= len(links):
        abort(404)
    del links[i]
    storage.save_case_links(court, case_key, links)
    return redirect(url_for("case", case_key=case_key) + "#links")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--court", default=DEFAULT_SELECTION, choices=sorted(SELECTIONS))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    DEFAULT_SELECTION = args.court
    app.run(host=args.host, port=args.port)
