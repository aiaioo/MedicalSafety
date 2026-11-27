#!/usr/bin/env python3
"""Web UI over the analysis written by analysis.py (read through progress_storage.py).

  /                  all cases, newest year / highest sequence number first,
                     with status and collapsible links; ?year=YYYY filters by year
                     ?q= searches by party, case number, hospital or advocate (with autocomplete from /suggest)
                     ; on /hearings/ the results are the matching hearing dates
  /hospitals/        hospitals named among the respondents, with case counts
  /hospital/<slug>   the cases against one hospital
  /advocates/        advocates named in the consumer cases (cause lists and case details), with case counts
  /advocate/<slug>   the cases one advocate appeared in, and for which side
  /hearings/         days of hearings, latest first, with a short summary of each
                     case, a number of days to a page; ?year=YYYY filters
  /day/<date>        the cases heard on one day (YYYY-MM-DD), in full
  /pdf/<kind>/<file> a pre- or post-hearing PDF in a continuous page viewer, with a download link
                     (/pdf/<kind>/<file>/download, pages as /pdf/<kind>/<file>/page/<n>.png)
  /case/<case_key>   parties and a timeline of hearings, latest first, plus
                     user-added links (related cases, party identities, ...)

No cases are held in memory. dashboard_stats.py (the daily crawl's last step) builds every case and hearing from the
analysis (build_state) and stores them ready to show (view_rows, the view_* tables), with the dashboards' figures;
each page reads only the rows it shows. A new analysis appears once that step has run.

Shows KMC cases (from analysis.py), CPA -- Consumer Protection Act -- cases from all the consumer courts
(from analysis_consumer.py), or both together; choose with the links at the top of every page (remembered
in a cookie). --court sets the choice for visitors who have not made one.

Consumer cases that case_details.py labelled not_medical (a property dispute, a bank v. a doctor ...; the label is
in the analysis status) are hidden from the lists, hospital counts and search, and shown with a "not medical" badge if the
"show non-medical" link at the top is on (a cookie). A case page opened directly always shows. Cases labelled
"unclear" are shown, with a badge.

Usage:  python viewer.py [--court kmc|dmc|consumer|all] [--host H] [--port P]
On its own the pages are at / (as listed above). The case manager (../case_manager/app.py) mounts the same pages
under /case_tracker/ in its own process, behind its sign-in, by registering this module's `bp` Blueprint.
"""

import argparse
import contextlib
import datetime
import re
import statistics
import sys
import threading
import time
from html import unescape
from pathlib import Path
from collections import Counter, defaultdict
from urllib.parse import parse_qsl, urlencode, urlparse

import fitz  # PyMuPDF
from flask import Blueprint, Flask, Response, g, abort, jsonify, make_response, redirect, render_template, request, send_file, send_from_directory, url_for
from markupsafe import Markup, escape

import progress_storage as storage
from judgements import DISMISSAL_REASONS, JUDGEMENT_CLASSES, ORDER_RE, _iso_date, dismissal_reason, judgement_of
from courts.providers import MATCHER
from metrics import OUTCOMES, REGISTER_COURTS, case_pace, days_to_close, outcome_of
from advocates import advocate_display, advocate_key, advocate_names
from analysis_consumer import ist_date
from courts.ejagriti_case import history_hearing_dates

# The viewer is a Blueprint so the case manager can mount it (app.py registers it at /case_tracker); run on its
# own (`python viewer.py`), it is mounted at "/" in a small Flask app of its own -- see create_app().
bp = Blueprint("case_tracker", __name__, template_folder="templates", static_folder="static")
COURT_LABELS = {"kmc": "KMC", "dmc": "DMC", "consumer": "CPA"}  # kind of court -> short label; an analysed court "consumer-<state>" is of kind "consumer"
CONSUMER_COMMISSION_NAMES = {  # what the portal calls the commission of a case (filedInComissionName) -> its full name, for the ones that are not district commissions
    "KARNATAKA": "Karnataka State Consumer Disputes Redressal Commission",
    "DELHI": "Delhi State Consumer Disputes Redressal Commission", "KERALA": "Kerala State Consumer Disputes Redressal Commission",
    "TAMIL NADU": "Tamil Nadu State Consumer Disputes Redressal Commission",
    "CIRCUIT BENCH MADURAI": "Tamil Nadu State Consumer Disputes Redressal Commission (Madurai Circuit Bench)",
    "CIRCUIT BENCH KALBURGI": "Karnataka State Consumer Disputes Redressal Commission (Kalaburagi Circuit Bench)",
    "CIRCUIT BENCH BELAGAVI": "Karnataka State Consumer Disputes Redressal Commission (Belagavi Circuit Bench)",
    "NCDRC": "National Consumer Disputes Redressal Commission"}
COUNCIL_NAMES = {"kmc": "Karnataka Medical Council", "dmc": "Delhi Medical Council"}  # the medical councils, which have no case documents of the portal's kind
MIN_YEAR = 2000  # case numbers for earlier years are typos (e.g. 1461): never shown as a year
COURT_DESCRIPTIONS = {  # kind of court -> what the court is, shown under its figures in the "At a glance" panel
    "kmc": "KMC: cases before the Karnataka Medical Council.",
    "dmc": "DMC: complaints before the Delhi Medical Council, 2000 to 2022 (the register it published until its site was rebuilt).",
    "consumer": "CPA: medical negligence cases before the consumer courts of {state}{extra} (Consumer Protection Act).",
}
CONSUMER_EXTRAS = {"Delhi": " and the National Commission (NCDRC), whose cases come from all over India"}  # what a state's consumer figures include besides its own courts
COUNCIL_SITES = {  # medical council -> its own website and inquiries page, linked under "More about" it in the panel
    "kmc": {"links": [("Karnataka Medical Council website", "https://www.karnatakamedicalcouncil.com/"),
                      ("Enquiry page", "https://www.karnatakamedicalcouncil.com/Enquiry.aspx")],
            "note": "The Council took its site down for a security audit in November 2026; if these links do not open, that may be why."},
    "dmc": {"links": [("Delhi Medical Council website", "https://delhimedicalcouncil.org/"),
                      ("Status of complaints examined", "https://delhimedicalcouncil.org/status-of-complaints")], "note": ""},
}
OPAQUE_COUNCILS = {  # state -> its medical council that publishes no case records, so the tracker has none to show
    "Kerala": {"name": "Kerala State Medical Councils", "url": "https://medicalcouncil.kerala.gov.in/", "verdict": "These Councils are opaque.",
               "finding": "Their website publishes no list of the complaints they receive, their hearings or the outcomes, only an occasional notice of action against a practitioner."},
    "Tamil Nadu": {"name": "Tamil Nadu Medical Council", "url": "https://www.tamilnadumedicalcouncil.org/", "verdict": "This Council is opaque.",
                   "finding": "Its website publishes no list of the complaints it receives, its hearings or the outcomes."},
}
DEFAULT_STATE = "Karnataka"  # the state shown to a visitor with no choice yet (the first state with analysed courts if there is none of this name)
SELECTIONS = {"kmc": "KMC", "dmc": "DMC", "consumer": "CPA (consumer courts)", "all": "All"}  # what the selector offers
DEFAULT_SELECTION = "all"  # for visitors with no choice yet; set by --court
PDF_COURT = "kmc"  # only the KMC has original PDFs
PDF_DIR = Path(__file__).parent / "pdfs"
PDF_KINDS = {"pre_hearing": "Pre-hearing PDF", "post_hearing": "Post-hearing PDF"}
PAGE_IMAGE_DPI = 150
VIEW_FORMAT = 1  # of the rows view_rows() makes: raise it when they change, so the pages are built again at the next deploy
HEARING_DATES_PER_PAGE = 20
KEEP_TEXTS = False  # build_state() holds each hearing's long texts too (metrics.py works from them)
USE_STORED_METRICS = True  # build_state() takes each consumer case's outcome, gaps and adjournments from the stored metrics (metrics.py) when they are recent; metrics.py itself turns this off
# Variants whose spelling is too far apart to normalise by rule, or that carry a stray person's name. Keys are
# slugs as hospital_tokens() leaves them (apostrophes dropped, "Medical College" before "Hospital" dropped).
HOSPITAL_ALIASES = {
    "bhagwan-mahaveer-jain-hospital": "bhagawan-mahaveer-jain-hospital",
    "bhagavanmahaveer-jain-hospital": "bhagawan-mahaveer-jain-hospital",
    "rajarajeswari-hospital": "rajarajeshwari-hospital",
    "cloudenine-fertility-clodnine-hospital": "cloudnine-hospital",
    "clodnine-hospital": "cloudnine-hospital",
    "poornima-m-gowda-cloudnine-hospital": "cloudnine-hospital",
    "motherhood-women-children-hospital": "motherhood-hospital",
    "narayananethralaya-eye-hospital": "narayana-nethralaya-eye-hospital",
    "bgs-gleneagles-global-hospital": "gleneagles-global-hospital",
    "bowring-lady-curzon-hospital": "lady-curzon-hospital",
    "child-hospital": "children-hospital",
    "ramaiah-leena-hospital": "leena-ramaiah-hospital",
    "ms-ramaiah-memorial-hospital": "ramaiah-memorial-hospital",
    "sumeet-jaiswal-arka-hospital": "arka-hospital",
    "sudhamani-jayanagar-general-hospital": "jayanagar-general-hospital",
    "sowmya-dinesh-hr-ssm-hospital": "ssm-hospital",
    "satish-l-sparsh-hospital": "sparsh-hospital",
    "yenapoya-hospital": "yenepoya-hospital",
    "sinam-inaoton-singha-st-martha-hospital": "st-martha-hospital",
}


# Display names for aliased groups where no member is spelled cleanly
HOSPITAL_DISPLAY = {"arka-hospital": "Arka Hospital", "ssm-hospital": "SSM Hospital",
                    "children-hospital": "Children Hospital", "jayanagar-general-hospital": "Jayanagar General Hospital",
                    "st-martha-hospital": "St. Martha’s Hospital"}


def hospital_tokens(name):
    """The words that identify a hospital, with the spelling differences that do not matter removed: case,
    punctuation and apostrophes ("St. John's" = "St Johns" = "ST.JOHN'S"), a possessive or plural s on a saint's
    name ("St Philomenas" = "St Philomena"), "Saint" = "St", "Hospitals" = "Hospital", "Medical College
    Hospital" = "Hospital", "(Multi/Super) Speciality Hospital" = "Hospital", "M/s", "Ltd"/"Pvt", and what comes before "through its"."""
    s = name.lower()
    t = re.search(r"\b(?:through|thro'?)\s+(?:its\s+)?(.*)$", s)
    if t and re.search(r"\bhospitals?\b", t.group(1)):
        s = t.group(1)
    s = re.sub(r"^belongs to\W*", "", s)
    s = re.sub(r"\bms/\.", "", s)  # a mangled "M/s."
    s = re.sub(r"\b(?:[a-z]\.\s*){2,}", lambda m: re.sub(r"\W", "", m.group()) + " ", s)  # "K.I.M.S." -> "kims"
    s = re.sub(r"['\u2019`]s\b", "", s)  # "John's" -> "John"
    s = re.sub(r"['\u2019`]", "", s)
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"\bappolo\b", "apollo", s)  # common misspellings
    s = re.sub(r"\bcloumbia\b", "columbia", s)
    s = re.sub(r"\bst\.?(?=[a-z])", "st ", s)  # "St.John" -> "St John"
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\bhospitals\b", "hospital", s)
    s = re.sub(r"\b(?:limited|ltd|pvt|private)\b", "", s)
    s = re.sub(r"\b(?:(?:multi|super)\s*)*(?:speciality|specialty|specility|sepeciality|speciallity)\s+(?=hospital\b)", "", s)  # "Super Speciality Hospital" = "Hospital"
    s = re.sub(r"\bmedical college\s+(?:and\s+)?hospital\b", "hospital", s)
    s = re.sub(r"\b(st) (\w{3,}?)s\b", r"\1 \2", s)  # a saint's name with a stray s: "st johns" -> "st john"
    return s.split()


def hospital_key(name):
    """Slug that merges spelling variants: 'Manipal Hospital' and 'M/s. Manipal Hospitals' share one, and so do
    'St. John's Hospital', 'ST.JOHN'S MEDICAL COLLEGE HOSPITAL' and 'X through its St Johns Hospital' (see hospital_tokens).
    Names that differ only by spaces ('Cloud nine', 'Cloudnine') get different slugs here; hospital_slug_map() joins them."""
    slug = "-".join(hospital_tokens(name))
    return HOSPITAL_ALIASES.get(slug, slug)


def hospital_slug_map(slugs_with_counts):
    """{slug: slug to use} joining slugs that differ only by spaces or hyphens ('cloud-nine-hospital' and
    'cloudnine-hospital'): the one used by most cases is kept (ties: the alphabetically first)."""
    groups = defaultdict(list)
    for slug, n in slugs_with_counts.items():
        groups[slug.replace("-", "")].append((-n, slug))
    return {slug: min(members)[1] for members in groups.values() for _, slug in members}


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


def listed_commission(name):
    """A commission as a cause list names it (courts/ejagriti.py), put as the portal puts it in a case's details:
    "Kerala State Commission" -> "Kerala", the National Commission -> "NCDRC", a district commission unchanged."""
    if name.endswith("(NCDRC)"):
        return "NCDRC"
    return name.removesuffix(" State Commission")


def commission_label(commission):
    """The short name of a consumer commission as the portal gives it (filedInComissionName): the state or national
    commission by its full name, a district commission as "<District> District"; "" if not known."""
    commission = " ".join(commission.split())  # the portal doubles a space in some ("West  Delhi")
    if not commission:
        return ""
    name = CONSUMER_COMMISSION_NAMES.get(commission.upper())
    if name:
        return name.replace(" Consumer Disputes Redressal Commission", "")
    return re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), commission.title()) + " District"


def kind_of(court):
    """The kind of an analysed court: "kmc", or "consumer" for "consumer" and "consumer-<state>"."""
    return court.split("-")[0]


_state_forced = threading.local()  # a state worked out for outside a request (the nightly rebuild)
_short = {}  # what -> (when read, value): lookups every request makes, read at most every few seconds


def _briefly(what, read, seconds=15):
    hit = _short.get(what)
    if hit is None or time.time() - hit[0] > seconds:
        hit = _short[what] = (time.time(), read())
    return hit[1]


def states():
    """The states that have analysed courts, Karnataka first."""
    return _briefly("states", storage.analysis_states)


def courts_of(state):
    return _briefly(("courts", state), lambda: storage.analysis_courts(state))


def default_state():
    found = states()
    return DEFAULT_STATE if DEFAULT_STATE in found or not found else found[0]


def state():
    """The state of this request: ?state=, else the visitor's choice (cookie), else the default."""
    forced = getattr(_state_forced, "value", None)
    if forced:
        return forced
    found = states()
    chosen = request.args.get("state")
    if chosen in found:
        return chosen
    chosen = request.cookies.get("state")
    return chosen if chosen in found else default_state()


class in_state:
    """`with in_state("Karnataka"):` -- load() and the pages' tables work for that state, with no request."""
    def __init__(self, st):
        self.st = st

    def __enter__(self):
        _state_forced.value = self.st

    def __exit__(self, *exc):
        _state_forced.value = None


@bp.app_template_global()
def council_site(court):
    """The links of a medical council's own pages (see COUNCIL_SITES), or None for another kind of court. A template
    global, so figures stored before a link changed show the current one."""
    return COUNCIL_SITES.get(court)


@bp.app_template_global()
def opaque_council(st):
    """The medical council of a state that publishes no case records (see OPAQUE_COUNCILS), or None."""
    return OPAQUE_COUNCILS.get(st)


def selections(st=None):
    """{id: label} of what the court selector offers in a state: the kinds of court it has, and "All" if more than one."""
    kinds = sorted({kind_of(c) for c in courts_of(st or state())})
    offered = {k: label for k, label in SELECTIONS.items() if k in kinds}
    return {**offered, "all": SELECTIONS["all"]} if len(offered) > 1 else {"all": SELECTIONS["all"]}


ALL_COURTS_PAGES = {"hospitals", "hospital", "advocates", "advocate", "case"}  # analytics pages (and the case pages they link to) always cover both courts


def all_courts_page():
    return (request.endpoint or "").rpartition(".")[2] in ALL_COURTS_PAGES


def selection():
    """The courts the visitor chose with the selector; the analytics pages ignore it and cover both."""
    if all_courts_page():
        return "all"
    offered = selections()
    sel = request.args.get("court") if request.endpoint == "case_tracker.home" else None  # a link from the analytics: the case list under that choice
    if sel in offered:
        return sel
    sel = request.cookies.get("court")
    return sel if sel in offered else DEFAULT_SELECTION


# The analytics' own years filter, by the year a case was filed: the current block of five years (the years from a multiple of
# five) and onwards, then each earlier block back to FIRST_PERIOD, then everything before that. (key, label, first year, last year);
# a new block appears by itself as the years pass. The panel's stored rows (dashboard_stats) keep a period under the year code
# -(first year * 10000 + last year), with 0 for no first year and 9999 for no last year; 0 is all years.
FIRST_PERIOD = 2015


def period_specs(this_year=None):
    this_year = this_year or datetime.date.today().year
    start = this_year // 5 * 5
    specs = [("all", "All years", None, None), (str(start), f"{start} onwards", start, None)]
    specs += [(str(y), f"{y}\u2013{y + 4}", y, y + 4) for y in range(start - 5, FIRST_PERIOD - 1, -5)]
    return specs + [("old", f"Before {FIRST_PERIOD}", None, FIRST_PERIOD - 1)]


def period_code(spec):
    _, _, lo, hi = spec
    return 0 if lo is None and hi is None else -((lo or 0) * 10000 + (hi or 9999))


def period():
    p = request.args.get("period")
    return p if p in {k for k, *_ in period_specs()} else "all"


def in_period(c, p):
    """Was the case filed in the period (see period_specs)?"""
    for key, _, lo, hi in period_specs():
        if key == p:
            return (lo is None or c["year"] >= lo) and (hi is None or c["year"] <= hi)
    return True


def analytics_page(page, p):
    """The table of the hospitals or advocates page for this state, period and not_medical choice: the stored one (built by
    dashboard_stats.py: one row read, no case data loaded), else worked out from all the state's cases, read for this request."""
    st, nm = state(), show_non_medical()
    doc = storage.load_analytics_page(st, page, p, nm)
    if doc is not None:
        return doc
    return build_analytics_page(page, p, nm, load("all", st))


def build_analytics_page(page, p, non_medical, data):
    """What analytics_page returns, worked out from a state's data (call inside `in_state`, without a request). The
    advocates table counts medical cases only, whatever `non_medical` says."""
    keep = (lambda c: True) if non_medical else (lambda c: not is_non_medical(c))
    if page == "hospitals":
        return hospital_table([c for c in data["ordered"] if keep(c) and in_period(c, p)])
    return [{k: v for k, v in a.items() if k not in ("cases", "sides")} for a in medical_advocates(data, p)]  # no case lists: a page needs only their counts


def show_non_medical():
    return request.cookies.get("non_medical") == "show"


def visible(c):
    """Is this case listed? Cases labelled not_medical are hidden unless the visitor asked for them."""
    return c.get("label") != "not_medical" or show_non_medical()


def court_ids(sel, st=None):
    """The analysed courts of a state (default: this request's) that a selection covers."""
    ids = courts_of(st or state())
    return ids if sel == "all" else [c for c in ids if kind_of(c) == sel]


BLANK_HEARING = {"hearing_time": "", "list": "", "held": "", "purpose_tags": "", "purpose_text": "", "outcome_text": "",
                 "next_date": "", "next_sitting": "", "prev_sitting": "", "postponed_from": "", "raw_text": "",
                 "next_stage_tags": "", "cross_examination_of": ""}


TEXT_FIELDS = ("raw_text", "record")  # a hearing's long texts: as printed on the cause list, and the court's record
EXCERPT = 106  # characters of the record the hearings list needs: more than its truncate(100) looks at
SHORT = 80  # strings shorter than this repeat from hearing to hearing: one copy of each is held


def compact_hearing(h):
    """A hearing as it is held: without its long texts (with_texts() reads them back), keeping the start of the court's
    record, and sharing its short strings with other hearings."""
    share = lambda d: {k: sys.intern(v) if type(v) is str and len(v) < SHORT else v for k, v in d.items() if k not in TEXT_FIELDS}
    out = share(h)
    if out.get("history"):
        out["history"] = share(out["history"])
    out["record_excerpt"] = (h.get("record") or "")[:EXCERPT]
    return out


def with_texts(entries):
    """[(case, hearing)] with each hearing a copy that has its long texts ("raw_text", "record"), read from the database
    for just these cases."""
    keys = sorted({c["case_key"] for c, _ in entries})
    if not keys:
        return []
    records = {k: proceedings_of(doc) for k, doc in storage.load_status_summaries(case_keys=keys).items()}
    printed = defaultdict(list)  # what identifies a cause-list hearing -> the texts of the rows that match, in order
    for r in storage.timeline_texts(keys):
        r["hearing_date"] = ist_date(r["hearing_date"])
        printed[_row_key(r["court"], r["case_key"], r)].append(r["raw_text"])
    used, out = Counter(), []
    for c, h in entries:
        texts, raw = [], ""
        if not (h.get("from_history") or h.get("from_order")):
            key = _row_key(c["court"], c["case_key"], h)
            texts = printed.get(key) or []
            if texts:
                raw = texts[min(used[key], len(texts) - 1)]
                used[key] += 1
        out.append((c, {**h, "raw_text": raw, "record": records.get(c["case_key"], {}).get(h["hearing_date"], "")}))
    return out


def _row_key(court, case_key, h):
    return (court, case_key, h["hearing_date"], h["hearing_time"], h["list"], h["purpose_text"], h["outcome_text"])


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


def hospital_names():
    """{slug: the name shown} of the hospitals of this state, over every court."""
    held = _held_data(state())
    if held is not None:
        return {slug: h["name"] for slug, h in held["hospitals"].items()}
    return storage.view_names(state(), "all", "hospital")


def advocate_case_counts():
    """{slug: number of medical cases} of the advocates of this state, over every court."""
    held = _held_data(state())
    if held is not None:
        return {slug: sum(not is_non_medical(c) for c in a["cases"]) for slug, a in held["advocates"].items()}
    return storage.view_link_counts(state(), "advocate", medical_only=True)


def analytics(cases, all_years=None):
    """Numbers for the front page's panel, from the cases shown: per court the count of each outcome, and the
    TOP_HOSPITALS hospitals named in most cases."""
    per_court = {}
    for c in cases:
        row = per_court.setdefault(c["court"], {"court": c["court"], "label": c["court_label"], "description": COURT_DESCRIPTIONS.get(kind_of(c["court"]), "").format(state=c.get("state", ""), extra=CONSUMER_EXTRAS.get(c.get("state"), "")), "total": 0,
                                                "decided": 0, "won_patient": 0, "won_hospital": 0, **{k: 0 for k, _ in OUTCOMES}})
        row["total"] += 1
        row[c["outcome"]] += 1
        verdict = case_verdict(c)  # who won, where the court allowed or dismissed the complaint and the hospital's side is known
        if verdict:
            row["decided"] += 1
            row["won_patient" if verdict == "against" else "won_hospital"] += 1
        row.setdefault("gaps", []).extend(c["gaps"])
        row.setdefault("days_closed", [])
        if c["days_to_close"] is not None:
            row["days_closed"].append(c["days_to_close"])
        row.setdefault("with_hearings", 0)
        row["with_hearings"] += bool(c["gaps"])
    for row in per_court.values():
        years = [d / 365.25 for d in row.pop("days_closed")]
        row["avg_years"] = round(statistics.mean(years), 1) if len(years) >= MIN_TIMING_CASES else None  # average filing-to-close time, from enough closed cases with dates
        row["n_years"] = len(years)
        row["median_gap"] = int(statistics.median(row["gaps"])) if row["gaps"] else None
        row["long_gap_pct"] = round(100 * sum(g > 60 for g in row["gaps"]) / len(row["gaps"])) if row["gaps"] else None
        row["n_gaps"] = len(row["gaps"])
        del row["gaps"]  # kept out of the stored numbers: the page only needs the medians
        closed = row["total"] - row["open"]
        row["closed"] = closed
        enough = row["decided"] >= MIN_TIMING_CASES  # the share of decided cases won by each side, only from enough of them
        row["patient_win_pct"] = round(100 * row["won_patient"] / row["decided"]) if enough else None
        row["hospital_win_pct"] = round(100 * row["won_hospital"] / row["decided"]) if enough else None
        row["bars"] = [(k, label, row[k], round(100 * row[k] / row["total"])) for k, label in OUTCOMES if row[k]]
        row["dismissed_pct"] = round(100 * row["dismissed"] / closed) if closed else None
        row["decided_pct"] = round(100 * (row["disposed"] + row["allowed"]) / closed) if closed else None
    by_commission = {}  # per consumer court: each commission's own outcome counts, for the panel's drop-down
    for c in cases:
        if c.get("commission"):
            row = by_commission.setdefault(c["court"], {}).setdefault(c["commission"], {"name": c["commission"], "total": 0, "decided": 0, "won_patient": 0, "won_hospital": 0, **{k: 0 for k, _ in OUTCOMES}})
            row["total"] += 1
            row[c["outcome"]] += 1
            verdict = case_verdict(c)
            if verdict:
                row["decided"] += 1
                row["won_patient" if verdict == "against" else "won_hospital"] += 1
    for court, rows in by_commission.items():
        out = []
        for row in sorted(rows.values(), key=lambda r: (-r["total"], r["name"])):
            row["closed"] = row["total"] - row["open"]
            enough = row["decided"] >= MIN_TIMING_CASES  # as for the court: percentages only from enough decided cases
            row["patient_win_pct"] = round(100 * row["won_patient"] / row["decided"]) if enough else None
            row["hospital_win_pct"] = round(100 * row["won_hospital"] / row["decided"]) if enough else None
            row["bars"] = [(k, label, row[k], round(100 * row[k] / row["total"])) for k, label in OUTCOMES if row[k]]
            out.append(row)
        per_court[court]["commissions"] = out if len(out) > 1 else []  # nothing to choose between when there is one
    hospitals = defaultdict(lambda: [0, ""])
    best_names = hospital_names()  # each hospital's best spelling, as the hospitals list shows it
    for c in cases:
        for slug, n in c["hospitals"].items():
            if slug:
                hospitals[slug][0] += 1
                hospitals[slug][1] = HOSPITAL_DISPLAY.get(slug) or best_names.get(slug) or hospitals[slug][1] or n
    top = sorted(((n, name, slug) for slug, (n, name) in hospitals.items()), key=lambda t: (-t[0], t[1].lower()))[:TOP_HOSPITALS]
    timing = case_timing(cases)
    sides = side_comparison(cases)
    against = hospital_ranking(cases)
    by_year = {}
    for c in all_years or cases:
        row = by_year.setdefault(c["year"], {"year": c["year"], "total": 0, **{k: 0 for k, _ in OUTCOMES}})
        row["total"] += 1
        row[c["outcome"]] += 1
        row.setdefault("gaps", []).extend(c["gaps"])
    years = []
    this_year = datetime.date.today().year
    for y in sorted(by_year, reverse=True):
        if y > this_year or y < MIN_YEAR:  # a case numbered for a year yet to come, or before MIN_YEAR (a typo): no line for it
            continue
        row = by_year[y]
        row["bars"] = [(k, label, row[k], round(100 * row[k] / row["total"])) for k, label in OUTCOMES if row[k]]
        row["median_gap"] = int(statistics.median(row["gaps"])) if row["gaps"] else None
        del row["gaps"]
        years.append(row)
    counts = defaultdict(int)
    for c in cases:
        j = judgement_of(c["case_key"])
        if j:
            counts[j["class"]] += 1
    reasons = defaultdict(int)
    for c in cases:
        j = judgement_of(c["case_key"])
        if j and j["reason"]:
            reasons[j["reason"]] += 1
    n_dismissed = sum(reasons.values())
    dismissals = {"total": n_dismissed, "max": max(reasons.values(), default=0),
                  "bars": sorted(((k, label, reasons[k], round(100 * reasons[k] / n_dismissed)) for k, label, _ in DISMISSAL_REASONS if reasons[k]),
                                 key=lambda b: (b[0] == "other", -b[2]))}
    n_judgements = sum(counts.values())
    n_read = n_judgements - counts["scanned"]  # a scanned judgement has no text to read, so the shares are of the rest
    judgements = {"total": n_judgements, "read": n_read,
                  "bars": [(k, label, counts[k], round(100 * counts[k] / n_read) if k != "scanned" and n_read else None) for k, label in JUDGEMENT_CLASSES if counts[k]],
                  "max": max(counts.values(), default=0)}
    return {"courts": list(per_court.values()), "total": len(cases), "top_hospitals": top, "years": years, "judgements": judgements, "dismissals": dismissals, "against": against, "timing": timing, "sides": sides}


TOP_HOSPITALS = 15  # hospitals listed on the panel, by allegations of malpractice or failures (cases)
MIN_TIMING_CASES = 20  # a typical time or count is not shown from fewer closed cases than this
TIMING_GROUPS = [("allowed", "Allowed", ("allowed",)), ("dismissed", "Dismissed", ("dismissed",)),
                 ("settled", "Settled or withdrawn", ("settled", "withdrawn"))]


def _quartiles(values):
    """(25th, median, 75th percentile) of a list of numbers."""
    q = statistics.quantiles(values, n=4, method="inclusive")
    return q[0], q[1], q[2]


# the key outcomes shown as a share of closed cases: (key, tile label, what it comes from)
ENDED_OUTCOMES = [("allowed", "allowed (in full or in part)", "outcome"), ("dismissed", "dismissed, for any reason", "outcome"), ("open", "still open", "all"),
                  ("nonpros", "dismissed for non-prosecution", "reason"), ("merits", "dismissed: negligence not proven", "reason"),
                  ("limitation", "dismissed: time-barred or no jurisdiction", "reason"), ("appeal", "appeals dismissed, order upheld", "reason"),
                  ("returned", "returned or transferred", "outcome")]
MIN_REASON_CASES = 3  # a dismissal reason is not shown from fewer cases than this


def ended_shares(cases):
    """How cases ended, for the panel's tiles: still open (a share of all cases), allowed, dismissed (any reason),
    each dismissal reason (non-prosecution, negligence not proven ...), returned. The court's own outcome gives
    allowed, dismissed and returned, as a share of all closed cases (the still-open share is of all cases). A dismissal's reason is known only from the
    judgement PDF, so a reason's share is of the closed cases whose judgement could be read (basis "read", "of" the
    number of them, "read" in the result), not of all closed cases."""
    closed = [c for c in cases if c["outcome"] != "open"]
    if len(closed) < MIN_TIMING_CASES:
        return None
    reasons, read = defaultdict(int), 0
    for c in closed:
        j = judgement_of(c["case_key"])
        if j and j["class"] != "scanned":
            read += 1
        if j and j["reason"]:
            reasons[j["reason"]] += 1
    tiles = []
    for key, label, source in ENDED_OUTCOMES:
        n = len(cases) - len(closed) if source == "all" else sum(c["outcome"] == key for c in closed) if source == "outcome" else reasons[key]
        of = len(cases) if source == "all" else len(closed) if source == "outcome" else read
        if n and of >= MIN_TIMING_CASES and (source == "outcome" or n >= MIN_REASON_CASES):
            pct = 100 * n / of
            tiles.append({"key": key, "label": label, "n": n, "of": of, "pct": round(pct, 1) if pct < 10 else round(pct), "basis": {"all": "all", "outcome": "closed"}.get(source, "read")})
    return {"tiles": tiles, "closed": len(closed), "cases": len(cases), "read": read}


def case_timing(cases):
    """How long cases take, for the panel's "A typical case" tiles: per way of ending (TIMING_GROUPS) the months from
    filing to the last hearing (25th / median / 75th percentile) and the hearings held, each only where at least
    MIN_TIMING_CASES closed cases have the figure; the typical gap between hearings; and the share of closed cases
    that were settled or withdrawn. Only closed cases are counted, so recent filings still open pull the real
    typical time up. Consumer cases only for the months (the KMC's filing dates are not recorded)."""
    groups = []
    for key, label, outcomes in TIMING_GROUPS:
        mine = [c for c in cases if c["outcome"] in outcomes]
        months = [c["days_to_close"] / 30.44 for c in mine if c["days_to_close"] is not None]
        held = [c["n_held"] for c in mine if c["n_held"]]
        g = {"key": key, "label": label, "closed": len(mine), "months": None, "hearings": None}
        if len(months) >= MIN_TIMING_CASES:
            lo, mid, hi = _quartiles(months)
            g["months"] = {"low": round(lo), "mid": round(mid), "high": round(hi), "n": len(months)}
        if len(held) >= MIN_TIMING_CASES:
            g["hearings"] = {"mid": round(statistics.median(held)), "n": len(held)}
        groups.append(g)
    gaps = [x for c in cases for x in c["gaps"]]
    closed = [c for c in cases if c["outcome"] != "open"]
    ended_early = sum(c["outcome"] in ("settled", "withdrawn") for c in closed)
    per_case = {}
    for key, label, subset in (("closed", "closed", closed), ("open", "open", [c for c in cases if c["outcome"] == "open"])):
        held = [c["n_held"] for c in subset if c["n_held"]]
        per_case[key] = {"mid": round(statistics.median(held)), "mean": round(statistics.mean(held)), "n": len(held)} if len(held) >= MIN_TIMING_CASES else None
    return {"groups": groups, "per_case": per_case, "ended": ended_shares(cases),
            "gap_weeks": round(statistics.median(gaps) / 7, 1) if len(gaps) >= MIN_TIMING_CASES else None, "n_gaps": len(gaps),
            "ended_early_pct": round(100 * ended_early / len(closed)) if len(closed) >= MIN_TIMING_CASES else None, "n_closed": len(closed),
            "shown": any(g["months"] or g["hearings"] for g in groups) or any(per_case.values())}


MIN_SIDE_HEARINGS = 200  # hearings, and MIN_SIDE_LAWYERS lawyer appearances per side, before patient-vs-hospital rates are shown
MIN_SIDE_LAWYERS = 20


def side_comparison(cases):
    """Patients against hospitals, for the panel: how often each side asked for time (adjournment rate) and how often
    each was absent (no-show rate), per 100 hearings held, and how many cases the lawyers on each side have handled.

    Time asked for and absence are counted only where the court's recorded text says which side it was (see
    metrics.adjournment_sides), so the real rates are higher than shown; the comparison between the sides is the
    point. Hearings are those of cases where the hospital's side is known. A lawyer's experience is the number of
    cases they appear in anywhere in the tracker (not years at the bar, which no record gives); each lawyer counts once
    per case, on the side they act for, and the figure is the mean and median over those appearances."""
    known = [c for c in cases if c["provider_side"] and not is_non_medical(c)]
    hearings = sum(c["n_held"] for c in known)
    totals = defaultdict(int)
    for c in known:
        for key, n in c["adjournments"].items():
            totals[key] += n
    out = {"hearings": hearings, "cases": len(known), "rates": None, "lawyers": None}
    if hearings >= MIN_SIDE_HEARINGS and any(totals.values()):  # a court whose record never names a side (the KMC) has no rates, not 0.0
        out["rates"] = {kind: {w: {"n": totals[(w, kind)], "per100": round(100 * totals[(w, kind)] / hearings, 1)} for w in ("patient", "hospital")}
                        for kind in ("time", "absent")}
    cases_of = advocate_case_counts()
    seen = defaultdict(list)
    for c in cases:
        for slug, a in c["advocate_info"].items():
            if slug in cases_of:
                for side in {represents(c, r) for r in a["roles"]} & {"patient", "hospital"}:
                    seen[side].append(cases_of[slug])
    if all(len(seen[s]) >= MIN_SIDE_LAWYERS for s in ("patient", "hospital")):
        out["lawyers"] = {s: {"n": len(seen[s]), "mean": round(statistics.mean(seen[s]), 1), "median": round(statistics.median(seen[s]))}
                          for s in ("patient", "hospital")}
    return out


def compute_stats(sel, year, non_medical, st=None):
    """The panel's numbers for a court selection of a state, a year (0 = all years) and whether not_medical cases are shown,
    worked out from every case (dashboard_stats.py stores these; the page falls back to this when nothing is stored)."""
    in_scope = [c for c in load(sel, st)["ordered"] if non_medical or c.get("label") != "not_medical"]
    if year < 0:  # a period (period_code)
        key = next(spec[0] for spec in period_specs() if period_code(spec) == year)
        cases = [c for c in in_scope if in_period(c, key)]
    else:
        cases = [c for c in in_scope if not year or c["year"] == year]
    return analytics(cases, in_scope)


def panel_stats(st, sel, year, non_medical):
    """The panel's numbers for a state, from the dashboard_stats_by_state table when the daily crawl has stored them (one
    lookup, no walk over the cases), else worked out on the spot. Adds "as_of": the date they were worked out."""
    stored = storage.load_dashboard_stats(st, sel, bool(non_medical), year or 0)
    if stored:
        return {**stored["doc"], "as_of": stored["computed_at"].date().isoformat()}
    return {**compute_stats(sel, year or 0, non_medical, st), "as_of": datetime.date.today().isoformat()}


_held = threading.local()  # a state's data held while dashboard_stats.py works through it (see holding)


@contextlib.contextmanager
def holding(st, data):
    """`with holding(state, build_state(state)):` -- load() and the names and counts above come from that data, not from
    the stored pages."""
    _held.value = (st, data, {})
    try:
        yield
    finally:
        _held.value = None


def _held_data(st, sel="all"):
    held = getattr(_held, "value", None)
    if held is None or held[0] != st:
        return None
    if sel == "all":
        return held[1]
    if sel not in held[2]:
        held[2][sel] = _selected(held[1], sel, st)
    return held[2][sel]


def _selected(data, sel, st):
    """The data of the courts a selection covers, drawn from that of all of a state's courts."""
    ids = court_ids(sel, st)
    return _indexed([c for c in data["ordered"] if c["court"] in ids])


def load(sel=None, st=None):
    """The cases of a court selection of a state (default: this request's), indexed (see _indexed), without their
    hearings: read from the stored pages afresh each time, for what has to walk over all of them. A page that shows
    only some cases reads just those (cases_by_key)."""
    st = st or state()
    sel = sel or selection()
    held = _held_data(st, sel)
    if held is not None:
        return held
    data = _indexed([_case_from(doc) for doc in storage.view_case_docs(state=st)])
    return data if sel == "all" else _selected(data, sel, st)


def build_state(st):
    """Every case of a state, built from the analysis with its hearings, and indexed: what the stored pages
    (view_rows), the dashboards and metrics.py are made from. This takes a good deal of memory, so only the daily crawl's
    steps (and a deploy that changes VIEW_FORMAT) call it, never a request."""
    return _read(st, courts_of(st))


def cases_by_key(keys):
    """The stored cases with these keys, in that order."""
    return [_case_from(doc) for doc in storage.view_case_docs(keys)] if keys else []


class CaseList:
    """A list of cases known by their keys, of which only the ones asked for are read: what a paginated table needs."""

    def __init__(self, keys):
        self.keys = keys

    def __len__(self):
        return len(self.keys)

    def __getitem__(self, index):
        return cases_by_key(self.keys[index]) if isinstance(index, slice) else cases_by_key([self.keys[index]])[0]


def _case_doc(c):
    """A case as it is stored for the pages: without its hearings, and in what JSON can hold."""
    doc = {k: v for k, v in c.items() if k != "hearings"}
    doc["adjournments"] = [[who, kind, n] for (who, kind), n in c["adjournments"].items()]
    doc["advocate_info"] = {slug: {"names": dict(a["names"]), "roles": sorted(a["roles"])} for slug, a in c["advocate_info"].items()}
    return doc


def _case_from(doc):
    doc["seq"] = tuple(doc["seq"])
    doc["adjournments"] = Counter({(who, kind): n for who, kind, n in doc["adjournments"]})
    for a in doc["advocate_info"].values():
        a["names"], a["roles"] = defaultdict(int, a["names"]), set(a["roles"])
    return doc


def view_rows(st, data):
    """The rows that hold a state's pages (progress_storage.replace_views), from its built data."""
    cases, hearings, links, terms = [], [], [], set()
    for ord_, c in enumerate(data["ordered"]):
        names = (c["complainants"] + c["respondents"] + list(c["hospitals"].values())
                 + c["advocates"]["complainant"] + c["advocates"]["respondent"])
        ids = "".join(f" {t}" for t in id_tokens(c["enquiry_id"]) + id_tokens(c["case_key"]))
        label = c.get("label") or ""
        cases.append((c["case_key"], ord_, c["court"], c["year"], c["outcome"], label, c["enquiry_id"],
                      _number_key(c["enquiry_id"]), " ".join(names).lower(), ids, _case_doc(c)))
        hearings += [(c["case_key"], n, h["hearing_date"], h) for n, h in enumerate(c["hearings"])]
        links += [("hospital", slug, c["case_key"]) for slug in c["hospitals"] if slug]
        links += [("advocate", slug, c["case_key"]) for slug in c["advocate_info"] if slug]
        terms.update((c["court"], label, t) for t in [c["enquiry_id"], *c["complainants"], *c["respondents"]])
    shown_names = []
    for sel in selections(st):
        of_sel = data if sel == "all" else _selected(data, sel, st)
        for kind, key in (("hospital", "hospitals"), ("advocate", "advocates")):
            shown_names += [(sel, kind, slug, x["name"]) for slug, x in of_sel[key].items()]
    return cases, hearings, links, shown_names, sorted(terms)


def _read(st, ids):
    """Every case of the courts `ids` of state `st`, built and indexed (see _indexed)."""
    parties = defaultdict(list)
    advocate_rows = defaultdict(list)
    hearings = defaultdict(list)
    status_rows = []  # case keys are distinct across courts
    histories, statuses = storage.load_case_documents("history.json", ids), storage.load_status_summaries(ids)
    stored = {c: storage.stored_case_metrics(c) for c in ids if kind_of(c) == "consumer"} if USE_STORED_METRICS else {}  # the medical councils are small, and their outcome rests on later steps
    listed = {}  # (court, enquiry id) -> the commission whose cause list named the case
    for c in ids:
        if kind_of(c) == "consumer":
            listed.update({(c, eid): name for eid, name in storage.load_listed_commissions(c).items()})
        analysis = storage.load_analysis(c, texts=KEEP_TEXTS)
        for r in analysis["parties"]:
            parties[r["case_key"]].append(r)
        for r in analysis["advocates"]:
            advocate_rows[r["case_key"]].append(r)
        for r in analysis["timeline"]:
            r["hearing_date"] = ist_date(r["hearing_date"])  # older data holds the portal's UTC timestamps
            hearings[r["case_key"]].append(r)
        status_rows += [{**r, "court": c, "court_label": COURT_LABELS[kind_of(c)], "state": st} for r in analysis["status"]]

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
        hospitals, provider_hospitals = {}, {}
        for pr in parties[key]:
            if pr["role"] == "respondent":
                for n in MATCHER.hospital_names(pr["party"]):
                    hospitals.setdefault(hospital_key(n), n)
        provider = MATCHER.provider_side(names("complainant"), names("respondent"))
        for pr in parties[key]:  # the hospitals on the provider's side: respondents normally, the appellant or complainant when the hospital brought the case
            if provider and pr["role"] == provider:
                for n in MATCHER.hospital_names(pr["party"]):
                    provider_hospitals.setdefault(hospital_key(n), n)
        today = datetime.date.today().isoformat()
        fast = (stored.get(r["court"]) or {}).get(key)  # the stored figures for this case, if there are any
        if fast:
            gaps = list(fast["gaps"])
            adjourned = Counter({(who, kind): fast[f"{col}_{who}"] for who, kind, col in
                                 (("patient", "time", "time_asked"), ("hospital", "time", "time_asked"), ("patient", "absent", "absent"), ("hospital", "absent", "absent")) if fast[f"{col}_{who}"]})
        else:
            gaps, adjourned = case_pace([] if r["court"] in REGISTER_COURTS else hs, provider, today)
        held_dates = [h["hearing_date"] for h in hs if h["hearing_date"] and h["hearing_date"] <= today]
        outcome = fast["outcome"] if fast else outcome_of(r, statuses.get(key))
        filing_date = ist_date((statuses.get(key) or {}).get("caseFilingDate") or "")[:10]
        end_date = max(held_dates, default="")
        if r["court"] in REGISTER_COURTS and not filing_date:  # a register gives the date a complaint was received (its first timeline row) and, once disposed, the order's date
            dates = sorted(h["hearing_date"] for h in hs if h["hearing_date"])
            filing_date = dates[0] if dates else ""
            end_date = r.get("status_since") or (dates[-1] if len(dates) > 1 else "")
        commission = ((statuses.get(key) or {}).get("filedInComissionName") or "").strip() if kind_of(r["court"]) == "consumer" else ""
        if not commission:  # no details downloaded yet: the commission that listed the case
            commission = listed_commission(listed.get((r["court"], r["enquiry_id"]), ""))
        dismissal_text = dismissal_reason_from(hs) if outcome == "dismissed" else None  # while the texts are at hand
        if not KEEP_TEXTS:
            hs = [compact_hearing(h) for h in hs]
        cases[key] = {**r, "outcome": outcome, "commission": commission_label(commission) if kind_of(r["court"]) == "consumer" else "", "filing_date": filing_date, "n_held": 0 if r["court"] in REGISTER_COURTS else len(held_dates),
                      "days_to_close": days_to_close(filing_date, end_date, outcome), "hospitals": hospitals, "provider_hospitals": provider_hospitals, "provider_side": provider,
                      "gaps": gaps, "adjournments": adjourned, "advocate_info": case_advocates(advocate_rows[key]), "kind": kind, "seq": seq, "year": year,
                      "complainants": names("complainant"), "respondents": names("respondent"),
                      "hearings": hs, "last_hearing": hs[0]["hearing_date"] if hs else "", "dismissal_by_hearing": dismissal_text}
    for c in cases.values():  # whom each side's lawyers act for, worked out once (represents() is called a great many times)
        c["party_of"] = {role: MATCHER.party_of(c["complainants"], c["respondents"], role) for role in ("complainant", "respondent")}
    slug_uses = defaultdict(int)
    for c in cases.values():
        for slug in c["hospitals"]:
            slug_uses[slug] += 1
    slug_map = hospital_slug_map(slug_uses)  # "cloud-nine-hospital" and "cloudnine-hospital" are one hospital
    for c in cases.values():
        c["hospitals"] = {slug_map[slug]: n for slug, n in c["hospitals"].items()}
        c["provider_hospitals"] = {slug_map.get(slug, slug): n for slug, n in c["provider_hospitals"].items()}
    for c in cases.values():
        c["advocate_links"] = {role: sorted(((slug, advocate_display(a["names"])) for slug, a in c["advocate_info"].items()
                                             if role in a["roles"] and slug), key=lambda x: x[1].lower())
                               for role in ("complainant", "respondent")}
        c["advocates"] = {role: [n for _, n in links] for role, links in c["advocate_links"].items()}
    return _indexed(sorted(cases.values(), key=lambda c: (c["year"], c["seq"], c["kind"]), reverse=True))


def _indexed(ordered):
    """The data of a set of cases, in list order: them by key, and by hospital and advocate."""
    hospitals = defaultdict(lambda: {"names": defaultdict(int), "cases": []})
    for c in ordered:
        for slug, n in c["hospitals"].items():
            hospitals[slug]["names"][n] += 1
            hospitals[slug]["cases"].append(c)
    hospitals = {s: {"slug": s, "name": HOSPITAL_DISPLAY.get(s) or max(h["names"], key=lambda n: (hospital_key(n) == s, not (n.islower() or n.isupper()), h["names"][n], -len(n))), "cases": h["cases"]}
                 for s, h in hospitals.items() if s}
    advocates = defaultdict(lambda: {"names": defaultdict(int), "cases": [], "sides": {}})
    for c in ordered:
        for slug, a in c["advocate_info"].items():
            adv = advocates[slug]
            for n, k in a["names"].items():
                adv["names"][n] += k
            adv["cases"].append(c)
            adv["sides"][c["case_key"]] = sorted(a["roles"])
    advocates = {s: {"slug": s, "name": advocate_display(a["names"]), "cases": a["cases"], "sides": a["sides"]}
                 for s, a in advocates.items() if s}
    return {"cases": {c["case_key"]: c for c in ordered}, "hospitals": hospitals, "advocates": advocates, "ordered": ordered,
            "years": sorted({c["year"] for c in ordered if c["year"] >= MIN_YEAR}, reverse=True)}


def can_manage_links():
    """Only the case manager's administrator sees or changes the hand-entered links on cases. (Run on its own, the
    viewer has no sign-in, so there is no g.user and the links are open, as they always were.)"""
    user = getattr(g, "user", None)
    return user is None or bool(user.is_admin)


def court_links(sel=None):
    """Hand-entered links of the selected courts, by case key -- none for anyone but the administrator."""
    links = {}
    if not can_manage_links():
        return links
    for c in court_ids(sel or selection()):
        links.update(storage.load_links(c))
    return links


def court_of(case_key):
    """Which court a case belongs to, or 404."""
    c = case_data(case_key)
    if c is None:
        abort(404)
    return c["court"]


def case_data(case_key):
    """The case with this key (without its hearings), or None."""
    found = cases_by_key([case_key])
    return found[0] if found else None


def _cookie_path():
    """Where the viewer's cookies apply: "/" on its own, "/case_tracker/" when the case manager mounts it."""
    return url_for("case_tracker.home")


def _back_to(pages, prefixes=()):
    """The page the visitor came from (path and query) if it is one of `pages`, or starts with one of `prefixes`
    (paths relative to the viewer's own root, so they hold wherever it is mounted); otherwise the viewer's home."""
    ref = urlparse(request.referrer or "")
    root = _cookie_path()
    rel = "/" + ref.path[len(root):] if ref.path.startswith(root) else None
    if rel is not None and (rel in pages or rel.startswith(prefixes)):
        return ref.path + ("?" + ref.query if ref.query else "")
    return url_for("case_tracker.home")


@bp.before_request
def signed_in_accounts_only():
    """Inside the case manager (which has already checked that someone is signed in, and set g.user) a key-session
    guest -- someone holding a secret key but no account -- is sent to sign in: the tracker is not part of what a key
    covers. On its own there is no g.user, so everyone gets in."""
    user = getattr(g, "user", None)
    if user is not None and user.is_guest:
        return redirect(url_for("auth.signin"))


@bp.context_processor
def selector():
    st = state()
    hidden = _briefly(("hidden", st), lambda: storage.count_label(courts_of(st), "not_medical"))  # counted in the database: no case data is loaded for a page
    return {"selections": selections(st), "selected": selection(), "states": states(), "state": st, "state_url": lambda x: url_for("case_tracker.select_state", st=x), "periods": [(k, label) for k, label, *_ in period_specs()], "period": period(), "period_url": lambda k: url_for(request.endpoint, **{**request.view_args, **request.args.to_dict(), "period": None if k == "all" else k}),
            "court_choice": not all_courts_page() and not is_public_visitor(), "non_medical_count": hidden,
            "show_non_medical": show_non_medical(), "can_edit_links": can_manage_links()}


@bp.route("/non-medical/<choice>")
def non_medical(choice):
    if choice not in ("show", "hide"):
        abort(404)
    back = _back_to(("/", "/hearings/", "/hospitals/", "/advocates/"), ("/day/", "/hospital/", "/advocate/"))
    resp = redirect(back)
    resp.set_cookie("non_medical", choice, max_age=365 * 86400, samesite="Lax", path=_cookie_path())
    return resp


@bp.after_request
def remember_linked_court(resp):
    """A case list opened by a link carrying ?court= keeps that choice for the visitor, as a click on a pill would."""
    sel = request.args.get("court")
    if request.endpoint == "case_tracker.home" and sel in selections() and request.cookies.get("court") != sel:
        resp.set_cookie("court", sel, max_age=365 * 86400, samesite="Lax", path=_cookie_path())
    chosen = request.args.get("state")
    if chosen in states() and request.cookies.get("state") != chosen:
        resp.set_cookie("state", chosen, max_age=365 * 86400, samesite="Lax", path=_cookie_path())
    return resp


@bp.route("/state/<st>")
def select_state(st):
    if st not in states():
        abort(404)
    back = urlparse(_back_to(("/", "/hearings/", "/hospitals/", "/advocates/")))
    query = urlencode([(k, v) for k, v in parse_qsl(back.query) if k not in ("state", "court")])  # a ?state= in the page's own address would override the choice just made
    resp = redirect(back.path + ("?" + query if query else ""))
    resp.set_cookie("state", st, max_age=365 * 86400, samesite="Lax", path=_cookie_path())
    resp.delete_cookie("court", path=_cookie_path())  # the courts offered differ by state
    return resp


@bp.route("/court/<sel>")
def select_court(sel):
    if sel not in selections():
        abort(404)
    # back to the list the visitor was on; a case or hospital page may not exist under the new choice
    back = _back_to(("/", "/hearings/", "/hospitals/", "/advocates/"))
    resp = redirect(back)
    resp.set_cookie("court", sel, max_age=365 * 86400, samesite="Lax", path=_cookie_path())
    return resp


def wants_list():
    """True when the page's script asks for only its list, to put in place of the one shown (templates/case_tracker/_list_ajax.html)."""
    return request.headers.get("X-List") == "1"


def list_only(template, **context):
    """Just the list's part of a page, kept out of caches: the same address also serves the whole page."""
    resp = make_response(render_template(template, **context))
    resp.headers["Cache-Control"] = "no-store"
    resp.vary.add("X-List")
    return resp


PAGE_SIZE = 50


@bp.app_template_global()
def paginate(cases):
    """One page (?page=N) of a case list, and the links to the others."""
    pages = max(1, -(-len(cases) // PAGE_SIZE))
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    link = lambda n: url_for(request.endpoint, **{**request.view_args, **request.args.to_dict(), "page": n})
    return {"items": cases[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], "page": page, "pages": pages, "total": len(cases),
            "first": (page - 1) * PAGE_SIZE + 1, "last": min(page * PAGE_SIZE, len(cases)),
            "prev": link(page - 1) if page > 1 else None, "next": link(page + 1) if page < pages else None,
            "numbers": [(n, link(n)) for n in sorted({1, pages, *range(page - 2, page + 3)}) if 1 <= n <= pages]}


@bp.route("/")
def home():
    st, sel = state(), selection()
    ids = court_ids(sel, st)
    year = request.args.get("year", type=int)
    q = request.args.get("q", "").strip()
    outcome = request.args.get("outcome")
    if outcome not in {k for k, _ in OUTCOMES}:
        outcome = None
    listed = {"medical_only": not show_non_medical(), **search(q)}
    keys = storage.view_case_keys(st, ids, year=year, outcome=outcome, **listed)
    judgement = request.args.get("judgement")
    if judgement != "all" and judgement not in dict(JUDGEMENT_CLASSES):
        judgement = None
    if judgement:  # only the cases with a judgement PDF (of the chosen class)
        keys = [k for k in keys if (j := judgement_of(k)) and judgement in ("all", j["class"])]
    reason = request.args.get("reason")
    if reason in dict((k, l) for k, l, _ in DISMISSAL_REASONS):
        keys = [k for k in keys if (j := judgement_of(k)) and j["reason"] == reason]
    else:
        reason = None
    if wants_list():
        return list_only("case_tracker/_cases_table.html", cases=CaseList(keys), links=court_links())
    p = period()  # the panel has its own years filter and always covers both courts
    if q:  # a search changes the cases, so it is worked out live
        everything = cases_by_key(storage.view_case_keys(st, court_ids("all", st), **listed))
        stats = {**analytics([c for c in everything if in_period(c, p)], everything), "as_of": datetime.date.today().isoformat()}
    else:
        stats = panel_stats(st, "all", period_code(next(x for x in period_specs() if x[0] == p)), show_non_medical())
    judgement_label = "All judgements" if judgement == "all" else dict(JUDGEMENT_CLASSES).get(judgement)
    reason_label = {k: l for k, l, _ in DISMISSAL_REASONS}.get(reason)
    years = sorted((y for y in storage.view_years(st, ids) if y >= MIN_YEAR), reverse=True)
    return render_template("case_tracker/home.html", cases=CaseList(keys), outcome=outcome, outcome_label=dict(OUTCOMES).get(outcome), years=years, year=year, plabel=next(x[1] for x in period_specs() if x[0] == p) if p != "all" else None, q=q, links=court_links(), stats=stats,
                           judgement=judgement, judgement_label=judgement_label, reason=reason, reason_label=reason_label)


def search(q):
    """The filter (progress_storage._view_filter) for a search by party, case number, hospital or advocate."""
    return {"words": q.lower().split(), "tokens": id_tokens(q)} if q else {}


def front_page_stats(period_key="all", st=None):
    """The front page's "At a glance" numbers over every court of a state, for a page outside the tracker (the case manager's
    public page): the visitor's court and non-medical cookies don't apply there, so cases labelled not_medical are
    left out."""
    st = st if st in states() else default_state()
    return panel_stats(st, "all", period_code(next((x for x in period_specs() if x[0] == period_key), period_specs()[0])), False)


def _number_key(s):
    """A case number as one comparable string, ignoring case, punctuation, spaces and leading zeros:
    'KMC/ENQ/01/2027' and 'kmc enq 1 2027' both give 'kmcenq12027'."""
    return "".join(id_tokens(s or ""))


def _parties_line(c):
    first = lambda names: (names[0] if names else "").strip()
    return " v. ".join(p for p in (first(c["complainants"]), first(c["respondents"])) if p)


def suggest_cases(q, limit=10):
    """Case numbers in the tracker containing `q` (for the case manager's new-case box): [{"case_number", "court", "parties"}],
    those that start with it first, then the shortest, newest numbers."""
    key = _number_key(q)
    if len(key) < 2:
        return []
    return [{"case_number": c["enquiry_id"], "court": c["court_label"], "parties": _parties_line(c)}
            for c in storage.view_cases_by_number(key, states(), limit=limit)]


def stage_label(tags, text=""):
    """A hearing's stage for display: its tags ("cross_examination, arguments") as words, else the start of the listing text."""
    words = [t.strip().replace("_", " ") for t in (tags or "").split(",") if t.strip()]
    if words:
        out = ", ".join(words)
        return out[:1].upper() + out[1:]
    return ((text or "").split(" -- ")[0].strip())[:120]


def case_prefill(number, with_court=True):
    """What the tracker knows of the case with this number (spelt any way that _number_key makes equal), for filling in
    a case of the case manager, or None if it is not in the tracker:
    {"case_number", "court_label" ("KMC" / "CPA"), "court", "complainants", "respondents",
     "hearings": [{"date", "title", "order_text"}], "next_hearing" (ISO date or "")} -- one entry per hearing day, oldest first, whose order_text is what the
    tracker records of the day (outcome or order, then the next date). `with_court=False` skips finding the court's full
    name (a database read for a consumer case) when only the hearings are wanted."""
    key = _number_key(number)
    found = storage.view_cases_by_number(key, states(), limit=1, exact=True) if key else []
    if not found:
        return None
    c = _case_from(found[0])
    c["hearings"] = storage.view_case_hearings(c["case_key"])
    court = ""
    if with_court:
        if c["court"] in COUNCIL_NAMES:
            court = COUNCIL_NAMES[c["court"]]
        else:
            commission = ((storage.load_case_document(c["case_key"], "status.json") or {}).get("filedInComissionName") or "").strip()
            if commission:
                court = (CONSUMER_COMMISSION_NAMES.get(commission.upper())
                         or f"{commission} District Consumer Disputes Redressal Commission")
            elif c["commission"]:  # no details yet: as the stored page names it, from the cause list
                court = next((n for n in CONSUMER_COMMISSION_NAMES.values() if n.replace(" Consumer Disputes Redressal Commission", "") == c["commission"]),
                             f"{c['commission']} Consumer Disputes Redressal Commission")
    days = {}  # a day can have more than one list entry: they become one hearing
    read = [h for _, h in with_texts([(c, h) for h in c["hearings"]])]
    for h in sorted(read, key=lambda h: (h["hearing_date"], h["hearing_time"])):
        if not h["hearing_date"]:
            continue
        text = [x for x in (h.get("outcome_text"), h.get("record")) if x]
        if h.get("next_date"):
            text.append(f"Next hearing: {h['next_date']}")
        day = days.setdefault(h["hearing_date"], {"date": h["hearing_date"], "title": "", "texts": []})
        day["title"] = day["title"] or (h.get("purpose_text") or "").split(" -- ")[0].strip()
        for t in text:
            if t not in day["texts"]:
                day["texts"].append(t)
    stages = {}  # what each day was listed for
    for h in sorted(c["hearings"], key=lambda h: (h["hearing_date"], h["hearing_time"])):
        st = stage_label(h.get("purpose_tags"), h.get("purpose_text"))
        if h["hearing_date"] and st:
            stages.setdefault(h["hearing_date"], st)
    hearings = [{"date": d["date"], "title": d["title"] or "Hearing", "order_text": "\n".join(d["texts"]),
                 "stage": stages.get(d["date"], "")} for d in days.values()]
    # The next hearing: the date the latest hearing says the case is posted to (from the court's record, else the portal history).
    held = [h for h in c["hearings"] if h["hearing_date"]]
    latest = max(held, key=lambda h: (h["hearing_date"], h["hearing_time"]), default=None)
    next_hearing, next_stage, next_from = "", "", ""
    if latest:
        next_hearing = latest.get("next_date") or (latest.get("history") or {}).get("next") or ""
        if next_hearing <= latest["hearing_date"]:
            next_hearing = ""
        else:
            next_stage = stage_label(latest.get("next_stage_tags"))
            next_from = latest["hearing_date"]  # the hearing whose record (and PDF) names the next date
    return {"case_key": c["case_key"], "court_id": c["court"], "case_number": c["enquiry_id"], "court_label": c["court_label"], "court": court,
            "complainants": list(c["complainants"]), "respondents": list(c["respondents"]), "hearings": hearings,
            "next_hearing": next_hearing, "next_stage": next_stage, "next_from": next_from}


def hearing_files(case_key, court_id, date):
    """[(path, file name)] of the documents the court put out for a hearing day, to attach to an alert: for the KMC its post-hearing
    PDF(s); for a consumer commission the day's daily order (PDF or HTML; a judgement too). The medical councils publish only a
    final order, not one per hearing, so nothing."""
    found = []
    if court_id and kind_of(court_id) == "kmc":
        for _, kind, names in pdfs_for_date(date):
            if kind == "post_hearing":
                found += [(PDF_DIR / PDF_COURT / kind / n, n) for n in names]
    elif court_id and kind_of(court_id) == "consumer":
        for o in storage.order_files(case_key):
            m = ORDER_RE.fullmatch(o.name)
            if m and _iso_date(m["date"]) == date:
                found.append((o.path, o.name))
    return found


@bp.route("/suggest")
def suggest():
    """Autocomplete: up to 12 party, hospital and advocate names and case numbers containing the query."""
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2 or (is_public_visitor() and request.args.get("kind") not in ("advocate", "hospital")):  # party names and case numbers are for signed-in users
        return jsonify([])
    if request.args.get("kind") in ("advocate", "hospital"):  # a list page's filter box: that list's names only, from the stored table
        pool = {x["name"] for x in analytics_page("advocates" if request.args["kind"] == "advocate" else "hospitals", "all")}
        hits = sorted((s for s in pool if q in s.lower()), key=lambda s: (not s.lower().startswith(q), len(s), s))
        return jsonify(hits[:12])
    st, sel = state(), selection()
    return jsonify(storage.view_suggestions(st, sel, court_ids(sel, st), q, not show_non_medical(), 12))


MIN_CLOSED_FOR_RANKING = 3


def is_non_medical(c):
    """Is the case labelled as not about medical negligence? It may be listed (see visible), but it has no patient's or
    hospital's side: nobody wins it for either, and it counts in no lawyer's or hospital's record."""
    return c.get("label") == "not_medical"


def case_verdict(c):
    """'for' / 'against' the hospital, from who brought the case and how the court decided it: the court allowing
    it favours the complainant (or appellant), dismissing it favours the respondent. None when the case is open,
    ended some other way, the hospital's side is unknown, or it is not a medical case."""
    if is_non_medical(c):
        return None
    side = c.get("provider_side")
    if side not in ("complainant", "respondent") or c["outcome"] not in ("allowed", "dismissed"):
        return None
    return "for" if (c["outcome"] == "allowed") == (side == "complainant") else "against"


def hospital_ranking(cases):
    """Hospitals with at least MIN_CLOSED_FOR_RANKING closed cases among `cases` (as respondent, or as complainant
    or appellant), ranked by the share of those closed cases decided against them, highest first."""
    best = hospital_names()
    closed = defaultdict(list)
    for c in cases:
        if c["outcome"] != "open":
            for slug in c["provider_hospitals"]:
                if slug and not is_non_medical(c):
                    closed[slug].append(c)
    rows = []
    for slug, cs in closed.items():
        if len(cs) >= MIN_CLOSED_FOR_RANKING:
            against = sum(case_verdict(c) == "against" for c in cs)
            name = HOSPITAL_DISPLAY.get(slug) or best.get(slug) or next(iter(cs[0]["provider_hospitals"].values()))
            rows.append({"slug": slug, "name": name, "closed": len(cs), "against": against, "ratio": against / len(cs)})
    return sorted(rows, key=lambda r: (-r["ratio"], -r["closed"], r["name"].lower()))


def hospital_table(cases):
    """One row per hospital in `cases` (visible ones): {"slug", "name", "cases", "closed", "against", "ratio" (share of the closed
    cases, counted as hospital_ranking does, decided against it; None below MIN_CLOSED_FOR_RANKING closed), "years" and "hearings"
    (averages over its closed cases with a filing date / a hearing held), "adjourned" (% of its side's hearings where the record
    says it asked for time, None if unknown), "lawyers" (its side's most used: [(slug, name, cases)], up to 3)}."""
    best = hospital_names()
    by = defaultdict(dict)
    for c in cases:
        for slug in {**c["hospitals"], **c["provider_hospitals"]}:
            if slug:
                by[slug][c["case_key"]] = c
    mean = lambda xs, nd: round(sum(xs) / len(xs), nd) if xs else None
    rows = []
    for slug, cs in by.items():
        cs = list(cs.values())
        medical = [c for c in cs if not is_non_medical(c)]
        prov = [c for c in medical if slug in c["provider_hospitals"] and c["outcome"] != "open"]
        against = sum(case_verdict(c) == "against" for c in prov)
        closed = [c for c in cs if c["outcome"] != "open"]
        asked = held = 0
        lawyers = defaultdict(lambda: [0, ""])
        for c in cs:
            side = c["provider_side"] or "respondent"
            if c["provider_side"] and not is_non_medical(c):
                asked += c["adjournments"][(side, "time")]
                held += c["n_held"]
            for aslug, name in [] if is_non_medical(c) else c["advocate_links"][side]:
                lawyers[aslug][0] += 1
                lawyers[aslug][1] = name
        name = HOSPITAL_DISPLAY.get(slug) or best.get(slug) or next(iter({**cs[0]["hospitals"], **cs[0]["provider_hospitals"]}.values()))
        rows.append({"slug": slug, "name": name, "cases": len(cs), "closed": len(prov), "n_closed": len(closed), "against": against,
                     "ratio": against / len(prov) if len(prov) >= MIN_CLOSED_FOR_RANKING else None,
                     "ratio_all": against / len(medical) if len(medical) >= MIN_CLOSED_FOR_RANKING else None,
                     "years": mean([c["days_to_close"] / 365.25 for c in closed if c["days_to_close"] is not None], 1),
                     "hearings": mean([c["n_held"] for c in closed if c["n_held"]], 1),
                     "adjourned": round(100 * asked / held) if held else None,
                     "lawyers": [(a, n, k) for a, (k, n) in sorted(lawyers.items(), key=lambda t: (-t[1][0], t[1][1].lower()))[:3]]})
    return rows


HOSPITAL_SORTS = [("cases", "Number of cases"), ("against", "Share of closed cases decided against the hospital"), ("against_all", "Cases decided against, as a share of all its cases"),
                  ("years", "Average years per closed case"), ("hearings", "Average hearings per closed case"),
                  ("adjourned", "Share of hearings adjourned at its request")]
# sort key -> (value of a row or None if it has no rank, whether the largest comes first, how the value reads)
HOSPITAL_RANKS = {
    "against": (lambda r: r["ratio"], True, lambda r: f"{round(100 * r['ratio'])}% ({r['against']}/{r['closed']})"),
    "against_all": (lambda r: r["ratio_all"], True, lambda r: f"{round(100 * r['ratio_all'])}% ({r['against']}/{r['cases']})"),
    "cases": (lambda r: r["cases"], True, lambda r: f"{r['cases']} cases"),
    "years": (lambda r: r["years"] if r["n_closed"] >= MIN_CLOSED_FOR_RANKING else None, True, lambda r: f"{r['years']} years ({r['n_closed']} closed)"),
    "hearings": (lambda r: r["hearings"] if r["n_closed"] >= MIN_CLOSED_FOR_RANKING else None, True, lambda r: f"{r['hearings']} hearings ({r['n_closed']} closed)"),
    "adjourned": (lambda r: r["adjourned"] if r["cases"] >= MIN_CLOSED_FOR_RANKING else None, True, lambda r: f"{r['adjourned']}% adjourned ({r['cases']} cases)"),
}
HOSPITAL_BOARDS = [("outcome", "Most cases", "cases"), ("outcome", "Most often decided against", "against"),
                   ("outcome", "Most often against, adjusted for volume", "against_all"),
                   ("pace", "Slowest: average years from filing to the end", "years"), ("pace", "Most hearings per closed case", "hearings")]


def rank_hospitals(rows, sort):
    """(ranked rows, the rest): the rows that have the value `sort` ranks by, largest first, and those that do not, by cases."""
    key = HOSPITAL_RANKS[sort][0]
    ranked = sorted((r for r in rows if key(r) is not None), key=lambda r: (-key(r), -r["cases"], r["name"].lower()))
    rest = sorted((r for r in rows if key(r) is None), key=lambda r: (-r["cases"], r["name"].lower()))
    return ranked, rest


@bp.route("/hospitals/")
def hospitals():
    q = request.args.get("q", "").strip().lower()
    sort = request.args.get("sort")
    sort = sort if sort in HOSPITAL_RANKS else "cases"
    rows = analytics_page("hospitals", period())
    found = [r for r in rows if not q or q in r["name"].lower()]
    ranked, below = rank_hospitals(found, sort)
    ranked_total, below_total = len(ranked), len(below)
    found_total = ranked_total + below_total
    pages = max(1, -(-found_total // TABLE_PAGE_SIZE))  # the ranked hospitals, then the unranked, as one list in pages
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    lo, hi = (page - 1) * TABLE_PAGE_SIZE, page * TABLE_PAGE_SIZE
    ranked, below = ranked[lo:hi], below[max(0, lo - ranked_total):max(0, hi - ranked_total)]
    table = dict(hospitals=ranked, below=below, below_total=below_total, q=q, sort=sort, sorts=HOSPITAL_SORTS,
                 page=page, pages=pages, first_rank=lo + 1, found_total=found_total, min_closed=MIN_CLOSED_FOR_RANKING)
    if wants_list():
        return list_only("case_tracker/_hospital_ranking.html", **table)
    boards = [{"group": g, "title": t, "rows": [{**r, "text": HOSPITAL_RANKS[k][2](r)} for r in rank_hospitals(found, k)[0][:10]]} for g, t, k in HOSPITAL_BOARDS]
    return render_template("case_tracker/hospitals.html", boards=boards, total=len(rows), **table)


@bp.route("/hospital/<slug>")
def hospital(slug):
    st, sel = state(), selection()
    ids = court_ids(sel, st)
    name = storage.view_name(st, sel, "hospital", slug)
    if name is None:
        abort(404)
    keys = storage.view_linked_keys(st, ids, "hospital", slug, medical_only=not show_non_medical())
    if wants_list():
        return list_only("case_tracker/_cases_table.html", cases=CaseList(keys), links=court_links())
    return render_template("case_tracker/hospital.html", h={"slug": slug, "name": name, "cases": CaseList(keys)}, links=court_links())


def represents(case, role):
    """Whom an advocate on `role`'s side of the case acts for: 'hospital', 'patient', 'insurer', 'organisation',
    'mixed' or '' (see ProviderMatcher.party_of); 'not_medical' in a case that is not a medical one."""
    if is_non_medical(case):
        return "not_medical"
    stored = case.get("party_of")
    return stored[role] if stored and role in stored else MATCHER.party_of(case["complainants"], case["respondents"], role)


def is_public_visitor():
    """True for a visitor who has not signed in to the case manager that mounts the viewer (g.user is None there). The standalone
    viewer never sets g.user: everyone there is trusted, as ever."""
    return hasattr(g, "user") and g.user is None


@bp.context_processor
def helpers():
    def ct_link(endpoint, slug, text):
        """A link to a hospital's or advocate's page (which lists its cases), or just the name for a visitor who is not signed in."""
        if is_public_visitor():
            return escape(text)
        return Markup('<a href="{}">{}</a>').format(url_for(f"case_tracker.{endpoint}", slug=slug), text)
    return {"represents": represents, "ct_link": ct_link, "public": is_public_visitor()}


EARLY_DISMISSALS = {"nonpros", "admission"}  # reasons a dismissal says little about how the lawyer fared: nobody appeared, or the complaint was turned away at admission


def is_complaint(c):
    """Is the case a complaint (or an enquiry into one) -- not an appeal, review, revision, transfer or execution application? Of the
    consumer courts' case types only "CC" is; every case of a medical council is."""
    return kind_of(c["court"]) != "consumer" or c["kind"].split("-")[-1].upper() == "CC"


def dismissal_reason_of(c):
    """Why a dismissed case ended, as a key of DISMISSAL_REASONS: the judgement's reason, else what the court's record of the last
    hearing says; None if the case was not dismissed."""
    if c["outcome"] != "dismissed":
        return None
    j = judgement_of(c["case_key"])
    if j and j["reason"] and j["reason"] != "other":
        return j["reason"]
    return c["dismissal_by_hearing"]


def dismissal_reason_from(hearings):
    """The reason for a dismissal that the record of the last hearing (newest first) gives, or "other"."""
    last = hearings[0] if hearings else {}
    text = f"{last.get('record') or ''} {last.get('outcome_text') or ''}"
    return dismissal_reason(text) if text.strip() else "other"


def early_dismissal(c):
    """A dismissal that says little about how the lawyer fared: nobody appeared, the complaint was turned away at admission, or the
    case ended at its first hearing."""
    return c["outcome"] == "dismissed" and (dismissal_reason_of(c) in EARLY_DISMISSALS or c["n_held"] <= 1)


def advocate_metrics(cases, sides):
    """How an advocate's cases went, from their `cases` and `sides` ({case_key: roles they appeared in}):
    closed / closed_pct (closed cases of all their cases); win_done_pct (the cases won, on whichever side, of their
    completed cases); won_hearings (the median hearings held in the cases they won, n_won_held of them) and won_years (the median years
    from filing to the last hearing in those, n_won_time of them: consumer cases only, which have a filing date) -- of complaints only
    (not appeals or other applications) and not counting dismissals for non-appearance or at admission, or at the first hearing, which
    say little about the lawyer's work; won_patient / won_hospital (the cases they won for a
    patient, or for a hospital: complaint allowed, or dismissed) with their shares of ALL their cases; won / decided / won_pct (of the cases the court allowed or
    dismissed, how many the advocate's client won: a patient's lawyer wins when the complaint is allowed, a hospital's
    when it is dismissed; settled, withdrawn, returned and open cases are not decisions); time_n / time_of /
    time_pct and absent_n / absent_of / absent_pct (of the hearings held in their cases where the hospital's side is
    known, those where the record says the advocate's side asked for time, or was absent). The record names the
    side, not the lawyer, so the last two are the side's rates. hearings_closed / hearings_open are the median
    hearings held per closed and per open case (with the number of cases, n_hearings_*), and gap_days the median days
    between consecutive hearing days across all their cases (n_gaps of them). Each pct is None where there is nothing to divide by.
    Only patient-side and hospital-side appearances count for these; an insurer's or organisation's lawyer does not.
    Cases that are not medical ones are left out of every figure, "cases" included, even where they are listed."""
    cases = [c for c in cases if not is_non_medical(c)]
    m = {"cases": len(cases), "closed": sum(c["outcome"] != "open" for c in cases), "won": 0, "decided": 0,
         "won_patient": 0, "won_hospital": 0, "patient_cases": 0, "hospital_cases": 0, "closed_patient": 0, "closed_hospital": 0, "time_n": 0, "time_of": 0, "absent_n": 0, "absent_of": 0}
    won_held, won_days = [], []  # hearings held in, and days from filing to the end of, each case they won, on whichever side
    for c in cases:
        mine = {represents(c, r) for r in sides[c["case_key"]]} & {"patient", "hospital"}
        for side in mine:  # the cases they act in for each side, and how many of those have ended
            m[f"{side}_cases"] += 1
            m[f"closed_{side}"] += c["outcome"] != "open"
        verdict = case_verdict(c)
        if verdict and mine:
            m["decided"] += 1
            won = any((side == "hospital") == (verdict == "for") for side in mine)
            m["won"] += won
            if won and is_complaint(c) and not early_dismissal(c):
                if c["n_held"]:
                    won_held.append(c["n_held"])
                if c["days_to_close"] is not None:
                    won_days.append(c["days_to_close"])
            m["won_patient"] += "patient" in mine and verdict == "against"  # the complaint was allowed against the hospital
            m["won_hospital"] += "hospital" in mine and verdict == "for"
        if c["provider_side"]:
            for side in mine:
                m["time_of"] += c["n_held"]
                m["absent_of"] += c["n_held"]
                m["time_n"] += c["adjournments"][(side, "time")]
                m["absent_n"] += c["adjournments"][(side, "absent")]
    pct = lambda n, d: round(100 * n / d) if d else None
    median = lambda xs: round(statistics.median(xs)) if xs else None
    held = lambda closed: [c["n_held"] for c in cases if (c["outcome"] != "open") == closed and c["n_held"]]
    gaps = [g for c in cases for g in c["gaps"]]
    m.update(won_hearings=median(won_held), n_won_held=len(won_held),
             won_years=round(statistics.median(won_days) / 365.25, 1) if won_days else None, n_won_time=len(won_days))
    m.update(hearings_closed=median(held(True)), n_hearings_closed=len(held(True)),
             hearings_open=median(held(False)), n_hearings_open=len(held(False)), gap_days=median(gaps), n_gaps=len(gaps))
    return {**m, "closed_pct": pct(m["closed"], m["cases"]), "won_pct": pct(m["won"], m["decided"]),
            "closed_patient_pct": pct(m["closed_patient"], m["patient_cases"]), "closed_hospital_pct": pct(m["closed_hospital"], m["hospital_cases"]),
            "won_patient_pct": pct(m["won_patient"], m["cases"]), "won_hospital_pct": pct(m["won_hospital"], m["cases"]),
            "win_done_pct": pct(m["won"], m["closed"]), "won_patient_done_pct": pct(m["won_patient"], m["closed"]), "won_hospital_done_pct": pct(m["won_hospital"], m["closed"]),
            "time_pct": pct(m["time_n"], m["time_of"]), "absent_pct": pct(m["absent_n"], m["absent_of"])}


def medical_advocates(data, p="all"):
    """The advocates with their medical cases only (an advocate left with none is dropped), and the count of
    those cases for the complainant side and for the respondent side."""
    out = []
    for a in data["advocates"].values():
        cases = [c for c in a["cases"] if not is_non_medical(c) and in_period(c, p)]
        if cases:
            sides = [a["sides"][c["case_key"]] for c in cases]
            out.append({**a, "cases": cases, "complainant": sum("complainant" in s for s in sides),
                        "respondent": sum("respondent" in s for s in sides)})
            roles = [(c, r) for c in cases for r in a["sides"][c["case_key"]]]
            out[-1]["patient"] = sum(represents(c, r) == "patient" for c, r in roles)
            out[-1]["hospital"] = sum(represents(c, r) == "hospital" for c, r in roles)
            out[-1]["other_org"] = sum(represents(c, r) in ("insurer", "organisation") for c, r in roles)
            out[-1]["metrics"] = advocate_metrics(cases, a["sides"])
            out[-1]["n"] = len(cases)
    return out


# The rankings of lawyers: key -> what is ranked. "count" lists rank by a number of cases; "share" lists by won / (all cases or
# completed cases), where `won` is the cases won for patients (the complaint allowed) or for hospitals (dismissed).
RANKINGS = {
    "cases": {"title": "Most cases", "sub": "cases in the tracker", "group": "volume", "kind": "count"},
    "patient_cases": {"title": "Most patient cases", "sub": "cases acting for the patient", "group": "patients", "kind": "count"},
    "hospital_cases": {"title": "Most hospital cases", "sub": "cases acting for the hospital or doctor", "group": "hospitals", "kind": "count"},
    "patient": {"title": "Best for patients", "sub": "won for patients ÷ all cases", "group": "patients", "kind": "share", "won": "won_patient", "of": "cases"},
    "hospital": {"title": "Best for hospitals", "sub": "won for hospitals ÷ all cases", "group": "hospitals", "kind": "share", "won": "won_hospital", "of": "cases"},
    "patient_done": {"title": "Best for patients", "sub": "won for patients ÷ completed cases", "group": "patients", "kind": "share", "won": "won_patient", "of": "closed"},
    "hospital_done": {"title": "Best for hospitals", "sub": "won for hospitals ÷ completed cases", "group": "hospitals", "kind": "share", "won": "won_hospital", "of": "closed"},
    "patient_close": {"title": "Close rate for patients", "sub": "closed ÷ patient cases", "group": "patients", "kind": "share", "won": "closed_patient", "of": "patient_cases", "closing": True},
    "hospital_close": {"title": "Close rate for hospitals", "sub": "closed ÷ hospital cases", "group": "hospitals", "kind": "share", "won": "closed_hospital", "of": "hospital_cases", "closing": True},
    "win_done": {"title": "Win rate", "sub": "cases won for either side ÷ completed cases", "group": "wins", "kind": "share", "won": "won", "of": "closed"},
    "won_years": {"title": "Won fastest", "sub": "shortest time to win, median years per case won", "group": "speed", "kind": "speed",
                  "value": "won_years", "n": "n_won_time"},
    "won_hearings": {"title": "Won in fewest hearings", "sub": "median hearings per case won", "group": "speed", "kind": "speed",
                     "value": "won_hearings", "n": "n_won_held"},
}
GROUP_ORDER = ["patients", "hospitals", "volume", "wins", "speed"]  # the patients' lists together, the hospitals' right after, then the rest
ADVOCATE_SORTS = [(k, f"{r['title']} ({r['sub']})") for k, r in sorted(RANKINGS.items(), key=lambda kv: GROUP_ORDER.index(kv[1]["group"]))]
ADVOCATE_MIN_CASES = [1, 2, 3, 5, 10]  # the minimum number of cases (completed cases, for the completed-case rankings) to be ranked by a win share
DEFAULT_MIN_CASES = 2
TABLE_PAGE_SIZE = 100  # rows of a full table (hospitals, lawyers) to a page


def _count(a, key):
    """The number a "count" ranking orders by."""
    return {"cases": a["n"], "patient_cases": a["patient"], "hospital_cases": a["hospital"]}[key]


def _share_parts(a, spec):
    """(won, of) for a "share" ranking: cases won for the side, and all their cases or all their completed cases."""
    of = spec["of"]
    return a["metrics"].get(spec["won"], 0), (a["n"] if of == "cases" else a["metrics"]["closed"] if of == "closed" else a["metrics"].get(of, 0))  # .get: rows stored before a measure was added lack it until the nightly rebuild


def _speed_key(a, spec):
    """Order for the "won fastest" rankings: the smallest median (years, or hearings, in the cases they won) first (ties:
    more wins, then name); lawyers with no won case to measure last."""
    m = a["metrics"]
    return (m[spec["value"]] if m[spec["value"]] is not None else 10 ** 9, -m["won"], a["name"].lower())


def rank_advocates(advs, sort, min_cases):
    """(ranked, below): the advocates in the order of RANKINGS[sort]. A "count" ranking puts those with most cases first
    (ties: more cases overall, then name), all ranked. A "share" ranking orders by the share won, highest first (ties:
    more wins, then more of the denominator), and ranks only lawyers whose denominator (all cases, or completed cases) is
    at least `min_cases`, so one lucky case does not put a lawyer at the top; the rest come back as `below`, in the same
    order, to be listed after the ranking."""
    spec = RANKINGS[sort]
    if spec["kind"] == "speed":  # the minimum here is a number of won cases that have the measure
        ok = lambda a: a["metrics"][spec["value"]] is not None and a["metrics"][spec["n"]] >= min_cases
        return (sorted((a for a in advs if ok(a)), key=lambda a: _speed_key(a, spec)),
                sorted((a for a in advs if not ok(a)), key=lambda a: _speed_key(a, spec)))
    if spec["kind"] == "count":
        return sorted(advs, key=lambda a: (-_count(a, sort), -a["n"], a["name"].lower())), []
    def key(a):
        won, of = _share_parts(a, spec)
        return (-(won / of if of else 0), -won, -of, a["name"].lower())
    return (sorted((a for a in advs if _share_parts(a, spec)[1] >= min_cases), key=key),
            sorted((a for a in advs if _share_parts(a, spec)[1] < min_cases), key=key))


BOARD_SIZE = 10


def advocate_boards(advs, min_cases):
    """Every ranking side by side, top BOARD_SIZE of each, for the advocates page. A share ranking lists only lawyers
    with at least `min_cases` cases (completed cases, for the completed-case rankings) and at least one win. Each board
    has its key, title, subtitle, group and rows of {name, slug, text}."""
    boards = []
    for key, spec in RANKINGS.items():
        rows = []
        for a in rank_advocates(advs, key, min_cases)[0]:
            if len(rows) == BOARD_SIZE:
                break
            if spec["kind"] == "speed":
                m = a["metrics"]
                parts = [f"{m['won_years']} years" if m["won_years"] is not None else None,
                         f"{m['won_hearings']} hearings" if m["won_hearings"] is not None else None]
                text = f"{', '.join(x for x in parts if x)} ({m[spec['n']]} won)"
            elif spec["kind"] == "count":
                n = _count(a, key)
                if not n:
                    break  # ordered by this number, so nobody after has any
                text = f"{n} cases, {a['metrics']['closed']} closed" if key == "cases" else f"{n} cases"
            else:
                won, of = _share_parts(a, spec)
                if not won:
                    break  # ordered by the share, so nobody after has a win: not a "best" for anyone
                text = f"{round(100 * won / of)}% ({won}/{of})"
            rows.append({"name": a["name"], "slug": a["slug"], "text": text})
        boards.append({"key": key, "title": spec["title"], "sub": spec["sub"], "group": spec["group"], "kind": spec["kind"],
                       "completed": spec.get("of") == "closed", "wins": spec["kind"] == "speed", "closing": bool(spec.get("closing")), "rows": rows})
    return sorted(boards, key=lambda b: GROUP_ORDER.index(b["group"]))


@bp.route("/advocates/")
def advocates():
    q = request.args.get("q", "").strip().lower()
    sort = request.args.get("sort")
    sort = sort if sort in dict(ADVOCATE_SORTS) else "cases"
    min_cases = request.args.get("min", type=int)
    min_cases = min_cases if min_cases in ADVOCATE_MIN_CASES else DEFAULT_MIN_CASES
    found = [a for a in analytics_page("advocates", period()) if not q or q in a["name"].lower()]
    advs, below = rank_advocates(found, sort, min_cases)
    ranked_total, below_total = len(advs), len(below)
    total = ranked_total + below_total
    pages = max(1, -(-total // TABLE_PAGE_SIZE))  # the ranked lawyers, then the unranked, as one list in pages
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    lo, hi = (page - 1) * TABLE_PAGE_SIZE, page * TABLE_PAGE_SIZE
    advs, below, first_rank = advs[lo:hi], below[max(0, lo - ranked_total):max(0, hi - ranked_total)], lo + 1
    table = dict(advocates=advs, below=below, total=total, below_total=below_total, page=page, pages=pages, first_rank=first_rank, page_size=TABLE_PAGE_SIZE, q=q, sort=sort, sorts=ADVOCATE_SORTS,
                 sort_kind=RANKINGS[sort]["kind"], min_unit="completed cases" if RANKINGS[sort].get("of") == "closed" else "won cases" if RANKINGS[sort]["kind"] == "speed" else "cases",
                 min_cases=min_cases)
    if wants_list():
        return list_only("case_tracker/_advocate_ranking.html", **table)
    return render_template("case_tracker/advocates.html", boards=advocate_boards(found, min_cases), min_options=ADVOCATE_MIN_CASES, **table)


@bp.route("/advocate/<slug>")
def advocate(slug):
    st, sel = state(), selection()
    name = storage.view_name(st, sel, "advocate", slug)
    if name is None:
        abort(404)
    theirs = cases_by_key(storage.view_linked_keys(st, court_ids(sel, st), "advocate", slug))
    a = {"slug": slug, "name": name, "sides": {c["case_key"]: sorted(c["advocate_info"][slug]["roles"]) for c in theirs}}
    cases = [c for c in theirs if visible(c)]
    if wants_list():
        return list_only("case_tracker/_cases_table.html", cases=cases, sides=a["sides"], links=court_links())
    medical = [c for c in cases if not is_non_medical(c)]  # the figures count these; the list may show the others too
    return render_template("case_tracker/advocate.html", a={**a, "cases": cases}, links=court_links(), m=advocate_metrics(medical, a["sides"]),
                           medical=len(medical), others=len(theirs) - len(medical),
                           complainant=sum("complainant" in a["sides"][c["case_key"]] for c in medical),
                           respondent=sum("respondent" in a["sides"][c["case_key"]] for c in medical),
                           patient=sum(represents(c, r) == "patient" for c in cases for r in a["sides"][c["case_key"]]),
                           hospital=sum(represents(c, r) == "hospital" for c in cases for r in a["sides"][c["case_key"]]),
                           other_org=sum(represents(c, r) in ("insurer", "organisation") for c in cases for r in a["sides"][c["case_key"]]))


@bp.route("/hearings/")
def hearings():
    st, sel = state(), selection()
    ids = court_ids(sel, st)
    year = request.args.get("year")
    q = request.args.get("q", "").strip()
    listed = {"medical_only": not show_non_medical(), **search(q)}
    dates = storage.view_hearing_dates(st, ids, year=year, **listed)
    pages = max(1, -(-len(dates) // HEARING_DATES_PER_PAGE))
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    shown = [d for d, _ in dates[(page - 1) * HEARING_DATES_PER_PAGE:page * HEARING_DATES_PER_PAGE]]
    entries = defaultdict(list)
    for date, c, h in storage.view_hearings_on(st, ids, shown, **listed):
        entries[date].append((c, h))
    listing = dict(days=[(d, entries[d]) for d in shown], total=len(dates), page=page, pages=pages,
                   first=(page - 1) * HEARING_DATES_PER_PAGE + 1, year=year, q=q)
    if wants_list():
        return list_only("case_tracker/_hearing_dates.html", **listing)
    return render_template("case_tracker/hearings.html", years=sorted(storage.view_hearing_years(st, ids), reverse=True), **listing)


@bp.route("/day/<date>")
def day(date):
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        abort(404)
    st, sel = state(), selection()
    ids = court_ids(sel, st)
    found = storage.view_hearings_on(st, ids, [date], medical_only=not show_non_medical())  # a future next date has no hearings yet
    entries = with_texts([(c, h) for _, c, h in found])
    return render_template("case_tracker/day.html", date=date, entries=entries,
                           pdfs=pdfs_for_date(date) if PDF_COURT in ids else [])


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


@bp.route("/pdf/<kind>/<name>")
def pdf_view(kind, name):
    path = _pdf_path(kind, name)
    with fitz.open(path) as d:
        pages = d.page_count
    return render_template("case_tracker/pdf.html", kind=kind, label=PDF_KINDS[kind], name=name, pages=pages)


@bp.route("/pdf/<kind>/<name>/download")
def pdf_download(kind, name):
    _pdf_path(kind, name)
    return send_from_directory(PDF_DIR / PDF_COURT / kind, name, as_attachment=True)


@bp.route("/pdf/<kind>/<name>/page/<int:n>.png")
def pdf_page(kind, name, n):
    path = _pdf_path(kind, name)
    with fitz.open(path) as d:
        if not 1 <= n <= d.page_count:
            abort(404)
        png = d[n - 1].get_pixmap(matrix=fitz.Matrix(PAGE_IMAGE_DPI / 72, PAGE_IMAGE_DPI / 72)).tobytes("png")
    return Response(png, mimetype="image/png", headers={"Cache-Control": "private, max-age=3600"})


ORDER_KINDS = {"daily_order": "Daily order", "judgement": "Judgement"}
_text_cache = {}  # (path, mtime) -> extracted text


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
    for o in storage.order_files(case_key):
        m = ORDER_RE.fullmatch(o.name)
        if m:
            label = ORDER_KINDS[m["kind"]] + (f" (version {m['v']})" if m["v"] else "")
            try:
                text = order_text(o.path)
            except Exception:  # an unreadable file still gets its link
                text = ""
            found[_iso_date(m["date"])].append({"date": _iso_date(m["date"]), "name": o.name, "label": label, "ext": m["ext"], "text": text})
    for lst in found.values():
        lst.sort(key=lambda o: (o["label"].split()[0], int(re.search(r"\d+", o["label"]).group()) if "version" in o["label"] else 1))
    return found


@bp.app_template_global()
def dismissal_line(c):
    """Why a dismissed case ended, for the case list, where no judgement says so (judgement_line gives the reason when there is one):
    the reason read from the court's record of the last hearing ("Dismissed at admission", "Non-prosecution ..."), or None."""
    j = judgement_of(c["case_key"])
    if j and j["reason"]:
        return None
    reason = dismissal_reason_of(c)
    return dict((k, label) for k, label, _ in DISMISSAL_REASONS).get(reason) if reason and reason != "other" else None


@bp.app_template_global()
def judgement_line(c):
    """What the case list shows for a case's judgement: (file name, text), or None. The text is the classification and,
    where the hospital's side is known, which way it went for the hospital."""
    j = judgement_of(c["case_key"])
    if not j:
        return None
    text = j["label"].lower()
    if j["reason"]:
        text += ": " + dict((k, label) for k, label, _ in DISMISSAL_REASONS)[j["reason"]].lower()
    side = c.get("provider_side")  # the side the hospital is on: complainant or respondent
    if side in ("complainant", "respondent") and j["class"] in ("dismissed", "part", "full") and not is_non_medical(c):
        for_hospital = (j["class"] == "dismissed") == (side == "respondent")  # dismissal favours the respondent
        text += ", " + ("partly " if j["class"] == "part" else "") + ("for hospital" if for_hospital else "against hospital")
    return j["name"], text


COUNCIL_ORDER_DIRS = {"dmc": PDF_DIR / "dmc" / "orders"}  # where a medical council's order files are (the original downloads, Word or PDF)
COUNCIL_ORDER_RE = re.compile(r"[\w.-]+\.(?:docx?|pdf|rtf)", re.IGNORECASE)


@bp.route("/council-order/<court>/<name>")
def council_order(court, name):
    """A final order of a medical council (the DMC's), as the Council published it: Word documents download, PDFs open."""
    if court not in COUNCIL_ORDER_DIRS or not COUNCIL_ORDER_RE.fullmatch(name):
        abort(404)
    return send_from_directory(COUNCIL_ORDER_DIRS[court], name, as_attachment=not name.lower().endswith(".pdf"))


@bp.app_template_global()
def judgement_href(c, name):
    """(link, kind shown on it) of a case's judgement file: a consumer court's PDF is one of the case's orders, a medical
    council's order is in the council's folder."""
    if c["court"] in COUNCIL_ORDER_DIRS:
        return url_for("case_tracker.council_order", court=c["court"], name=name), name.rsplit(".", 1)[-1].upper()
    return url_for("case_tracker.order_file", case_key=c["case_key"], name=name), "PDF"


@bp.route("/order/<case_key>/<name>")
def order_file(case_key, name):
    """A saved order; HTML orders come from the court's portal, so they are sandboxed (no scripts)."""
    if not ORDER_RE.fullmatch(name) or not re.fullmatch(r"[\w-]+", case_key):
        abort(404)
    order = storage.find_order_file(case_key, name)
    if order is None or not order.path.is_file():
        abort(404)
    resp = send_file(order.path, download_name=name)  # the stored file is named by its content: send the order's name
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


@bp.route("/case/<case_key>")
def case(case_key):
    c = case_data(case_key)
    if c is None:
        abort(404)
    # The hearings (from the cause lists and the portal's history, see with_history) are the timeline. An order
    # dated on a day absent from both still gets an entry of its own (from_order).
    orders = orders_of(case_key)
    stored = storage.view_case_hearings(case_key)
    known = {h["hearing_date"] for h in stored}
    hearings = [h for _, h in with_texts([(c, h) for h in stored])]
    hearings += [{**BLANK_HEARING, "hearing_date": d, "history": None, "record": "", "from_order": True}
                 for d in orders if d not in known]
    hearings.sort(key=lambda h: (h["hearing_date"], h["hearing_time"]), reverse=True)
    return render_template("case_tracker/case.html", c=c, hearings=hearings, links=court_links().get(case_key, []), orders=orders)


def _link_from_form():
    url = request.form.get("url", "").strip()
    if "://" not in url:
        url = "https://" + url
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        abort(400, "Link must be an http(s) URL")
    return {"url": url, "text": request.form.get("text", "").strip() or url,
            "note": request.form.get("note", "").strip()}


@bp.post("/case/<case_key>/links")
def add_link(case_key):
    if not can_manage_links():
        abort(403)
    if case_data(case_key) is None:
        abort(404)
    link = _link_from_form()
    court = court_of(case_key)
    storage.save_case_links(court, case_key, storage.load_links(court).get(case_key, []) + [link])
    return redirect(url_for("case_tracker.case", case_key=case_key) + "#links")


@bp.post("/case/<case_key>/links/<int:i>/edit")
def edit_link(case_key, i):
    if not can_manage_links():
        abort(403)
    court = court_of(case_key)
    links = storage.load_links(court).get(case_key, [])
    if i >= len(links):
        abort(404)
    links[i] = _link_from_form()
    storage.save_case_links(court, case_key, links)
    return redirect(url_for("case_tracker.case", case_key=case_key) + "#links")


@bp.post("/case/<case_key>/links/<int:i>/delete")
def delete_link(case_key, i):
    if not can_manage_links():
        abort(403)
    court = court_of(case_key)
    links = storage.load_links(court).get(case_key, [])
    if i >= len(links):
        abort(404)
    del links[i]
    storage.save_case_links(court, case_key, links)
    return redirect(url_for("case_tracker.case", case_key=case_key) + "#links")


def create_app():
    """The viewer on its own, at "/" (the case manager instead registers `bp` under /case_tracker)."""
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--court", default=DEFAULT_SELECTION, choices=sorted(SELECTIONS))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    DEFAULT_SELECTION = args.court
    create_app().run(host=args.host, port=args.port)
