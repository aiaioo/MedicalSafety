"""Case details (status, hearing history and the order PDFs) of consumer cases, from e-Jagriti.

ConsumerCaseDownloader calls the portal's APIs directly (they need no login or captcha):
  getCaseStatus                   stage, next date, parties, and one entry per hearing (with the text of the
                                  order and, when a PDF exists, its orderTypeId: 1 = daily order, 2 = final order)
  getCaseDetailForHistoryDashBoard  the history list (hearing, bench, court)
  getDailyOrderJudgementPdf       one PDF per entry that has an order, as the page's "View" buttons do

If the portal starts demanding a captcha, these requests will fail with an HTTP error.

Downloads are incremental (see ConsumerCaseDownloader.sync): a case seen for the first time is fetched in
full; after that only what is new is saved -- orders of new hearings, and a new version of an order that the
portal has changed (earlier versions are kept, as <date>_<kind>_v2.pdf ...). Changed status and history are
likewise kept: the old copy is stored as a version.

What is saved, and where, is up to a CaseDetailsPersister, so the downloader knows nothing about folders:
StoragePersister hands them to progress_storage.py (documents to the database, order files to disk).
"""

import base64
import html
import json
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from functools import lru_cache

import progress_storage as storage

from . import log, make_session
from .ejagriti import API_BASE, TIMEOUT, USER_AGENT
from .medical import CLASSIFIER_VERSION, classify

REQUEST_DELAY = 0.5     # seconds between requests, to go easy on the portal
ORDER_KINDS = {1: "daily_order", 2: "judgement"}  # orderTypeId -> file name part
RECHECK_AFTER = timedelta(days=7)  # a case with no next hearing date (disposed?) is looked at this often
RECENT = timedelta(days=3)  # a hearing this recent with no order is asked about again every run
STALE_AFTER = timedelta(days=3)  # a case with nothing due (hearing still ahead, none recent) is looked at this often anyway
ORDER_EXTENSIONS = ("pdf", "html")  # recent orders are PDFs, older ones HTML pages
UTF8_BOM = b"\xef\xbb\xbf"  # some of the portal's PDFs start with this byte-order mark, which is not part of the PDF


class NoOrderDocument(ValueError):
    """The portal answered with success but an empty document: it lists an order that it has no file for (seen for many
    orders of 2010-2014)."""


@lru_cache(maxsize=4096)
def _india_date(utc_timestamp):
    return (datetime.strptime(utc_timestamp, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def history_hearing_dates(history):
    """The dates (YYYY-MM-DD, India time) of the held hearings in a history list; the portal gives UTC
    timestamps, 5:30 behind, e.g. 2026-11-04T18:30:00.000Z is the hearing of 2026-11-05."""
    return [_india_date(h["dateOfAction"][:19])
            for h in history or [] if h.get("dateOfAction") and (h.get("actionTaken") or "").startswith("Hearing")]


def _asked_lately(checked, day, now):
    """Is it too soon to ask again for an order the portal did not have when asked at `checked` (an ISO time, or None if
    it was not)? It is asked again after RECHECK_AFTER, and every time while the hearing (`day`) is recent."""
    return bool(checked) and now - datetime.fromisoformat(checked) < RECHECK_AFTER \
        and now.date() - datetime.strptime(day, "%Y-%m-%d").date() > RECENT


OLD_DISTRICT_NUMBER_RE = re.compile(r"AB0(\d)/(\d+)/(.+)")
_PARTY_WORD_RE = re.compile(r"[a-z]{4,}")
_COMMON_PARTY_WORDS = {"also", "limited", "private", "hospital", "pvt", "ltd", "medical", "services"}


def alias_number(case_number):
    """Some cause lists give a district case an old-style number, AB0n/<commission>/<type>/<no>/<year>, which the
    portal's case lookup answers with 400 ("Case Details are not available"); it knows the case as
    DC/AB<n-1>/<commission>/... (AB03/525/CC/10/2024 is DC/AB2/525/CC/10/2024). None for any other number."""
    m = OLD_DISTRICT_NUMBER_RE.fullmatch(case_number)
    return f"DC/AB{int(m.group(1)) - 1}/{m.group(2)}/{m.group(3)}" if m and int(m.group(1)) > 1 else None


def _party_words(names):
    return {w for w in _PARTY_WORD_RE.findall(" ".join(names).lower()) if w not in _COMMON_PARTY_WORDS}


def same_parties(status, parties):
    """Is the portal's case (its status) the one on the cause list? Both a complainant and a respondent must share a word with
    the listed ones; a number alone is not enough, as another commission's case may have it."""
    return bool(_party_words([status.get("complainant") or ""]) & _party_words(parties.get("complainants") or [])
                and _party_words([status.get("respondent") or ""]) & _party_words(parties.get("respondents") or []))


def _same_side(a, b):
    """Do two lists of names share a telling word -- or, where one has none ("ESI Hospital"), a whole name?"""
    x, y = _party_words(a), _party_words(b)
    if x and y:
        return bool(x & y)
    plain = lambda names: {re.sub(r"\W", "", n.lower()) for n in names} - {""}
    return bool(plain(a) & plain(b))


def transfers(cases):
    """{circuit bench's number: the State Commission's number} of the cases listed under both: sent to a bench, a case goes
    on under a new number (analysis_consumer.number_before_transfer) while the old one stops. `cases` is {case number:
    {"complainants": [...], "respondents": [...]}}; both sides' parties must match, as a bench gives new cases of its
    own numbers that look the same."""
    from analysis_consumer import number_before_transfer  # a top-level module; imported late as it imports this package
    found = {}
    for number, case in cases.items():
        old = number_before_transfer(number)
        if old in cases and all(_same_side(case[side], cases[old][side]) for side in ("complainants", "respondents")):
            found[number] = old
    return found


def carried_over(old_status, old_history, status, history):
    """(status, history) of a transferred case with the hearings its record under the old number had: the new record's
    hearings start at the transfer, so those the old one had before the first of them are kept in front. (The new history
    also has the filing, dated long before, which is why only hearings are compared.)"""
    def before(old, new, field, is_hearing=lambda e: True):
        first = min((e[field] for e in new if is_hearing(e) and e.get(field)), default=None)
        return [e for e in old if is_hearing(e) and e.get(field) and (first is None or e[field] < first)] + new
    hearings = before((old_status or {}).get("caseHearingDetails") or [], status.get("caseHearingDetails") or [], "dateOfHearing")
    return {**status, "caseHearingDetails": hearings}, before(
        old_history or [], history or [], "dateOfAction", lambda e: (e.get("actionTaken") or "").startswith("Hearing"))


def tracked_cases():
    """{case number: {"respondents": [...], "complainants": [...], "case_type": ..., "listed_as": {...}}} for every consumer
    case that has appeared on a saved cause list (storage.iter_latest_cause_lists); a later list's parties replace an earlier one's.
    The case number is in its standard form (analysis_consumer.standard_number); "listed_as" holds the ways the lists
    wrote it, since the portal may know the case only by one of those. A case sent to a circuit bench stays under its
    old number, with the new one in "listed_as" and in "moved_to" (see transfers).
    The lists are read one at a time, so only the small result is held in memory, not every list."""
    from analysis_consumer import standard_number  # a top-level module; imported late as it imports this package
    cases = {}
    for cause_list in storage.iter_latest_cause_lists():
        for row in cause_list["doc"]["causelists"]:
            for c in row["cases"]:
                listed = " ".join((c.get("caseNumber") or "").split())
                number = standard_number(listed)
                if number:  # None for a mistyped number ("DC/545/CC/2013/344")
                    cases[number] = {
                        "respondents": [r.get("respondant_name") or "" for r in c.get("respondantList") or []],
                        "complainants": [c2.get("complainant_name") or "" for c2 in c.get("complainantList") or []],
                        "case_type": c.get("caseTypeName") or "",
                        "listed_as": cases.get(number, {}).get("listed_as", set()) | {listed}}
    for moved, old in transfers(cases).items():
        new = cases.pop(moved)
        cases[old] = {**new, "case_type": cases[old]["case_type"], "listed_as": cases[old]["listed_as"] | new["listed_as"],
                      "moved_to": cases[old].get("moved_to", set()) | new["listed_as"]}
    return cases


def tracked_case_numbers():
    return sorted(tracked_cases())


class CaseDetailsPersister(ABC):
    """Where a case's downloaded details go. `name` is a file name within the case, e.g. "status.json"."""

    @abstractmethod
    def exists(self, case_number, name):
        """Has this file already been saved?"""

    @abstractmethod
    def read(self, case_number, name):
        """The bytes of a saved file, or None if there is none."""

    @abstractmethod
    def save_json(self, case_number, name, data):
        """Save a JSON-able value; return where it was saved."""

    @abstractmethod
    def save_file(self, case_number, name, content):
        """Save the bytes of an order (PDF or HTML); return where it was saved."""


class StoragePersister(CaseDetailsPersister):
    """Keeps a case's details with progress_storage.py: the JSON documents (status.json, history.json, meta.json,
    classification.json, no_orders.json) and their earlier versions (versions/<stem>_<stamp>.json) in the
    database, the orders (orders/<date>_<kind>[_vN].pdf|html) as files in the shared order store."""

    def _key(self, case_number):
        from analysis_consumer import case_key  # the same key the analysis and the viewer use
        return case_key(case_number)

    @staticmethod
    def _version(name):
        """('status.json', '20261110T103000') for 'versions/status_20261110T103000.json'."""
        stem, stamp = name[len("versions/"):-len(".json")].rsplit("_", 1)
        return f"{stem}.json", stamp

    def exists(self, case_number, name):
        if name.startswith("orders/"):
            return storage.order_exists(self._key(case_number), name[len("orders/"):])
        return self.read(case_number, name) is not None

    def read(self, case_number, name):
        key = self._key(case_number)
        if name.startswith("orders/"):
            return storage.read_order_file(key, name[len("orders/"):])
        doc = storage.load_case_document(key, name)
        return None if doc is None else json.dumps(doc, ensure_ascii=False).encode("utf-8")

    def save_json(self, case_number, name, data):
        storage.save_case_document(self._key(case_number), name, data)
        return f"{self._key(case_number)}/{name}"

    def save_file(self, case_number, name, content):
        key = self._key(case_number)
        if name.startswith("versions/"):
            doc_name, stamp = self._version(name)
            storage.save_case_document_version(key, doc_name, stamp, json.loads(content))
            return f"{key}/{name}"
        if name.startswith("orders/"):
            return str(storage.save_order_file(key, name[len("orders/"):], content))
        raise ValueError(f"not a file of a case: {name}")


class ConsumerCaseDownloader:
    def __init__(self, persister, session=None):
        self.persister = persister
        self.session = session or make_session(USER_AGENT)

    def _request(self, method, path, **kwargs):
        return self.session.request(method, f"{API_BASE}{path}", timeout=TIMEOUT, **kwargs)

    def _get_data(self, path, **params):
        resp = self._request("GET", path, params=params)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != 200:
            raise ValueError(f"{path}: {body.get('message')}")
        return body["data"]

    def fetch_order(self, filing_reference_number, date_of_hearing, order_type_id):
        """One order as (bytes, extension). Recent orders come back as a base64-encoded PDF inside the JSON;
        older ones are an HTML page in the same field. NoOrderDocument if the field is empty. The path keeps the double
        slash the portal's own page uses."""
        data = self._get_data("//courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf",
                              filingReferenceNumber=filing_reference_number, dateOfHearing=date_of_hearing,
                              orderTypeId=order_type_id)
        text = data.pop("dailyOrderPdf", None) or ""
        del data
        if not text.strip():
            raise NoOrderDocument("the portal has no document for this order")
        if text.lstrip().lower().startswith(("<html", "<!doctype")):
            return text.encode("utf-8"), "html"
        pdf = base64.b64decode(text)
        del text  # the base64 copy is a third larger than the PDF; don't keep both
        pdf = pdf.removeprefix(UTF8_BOM)
        if not pdf.startswith(b"%PDF"):
            raise ValueError("order is neither a PDF nor HTML")
        return pdf, "pdf"

    def _json(self, case_number, name):
        raw = self.persister.read(case_number, name)
        return json.loads(raw) if raw else None

    def _versions(self, case_number, base):
        """File names of the saved versions of an order, oldest first: <base>.pdf, <base>_v2.pdf ..."""
        found, n = [], 1
        while True:
            stem = base if n == 1 else f"{base}_v{n}"
            name = next((f"{stem}.{e}" for e in ORDER_EXTENSIONS if self.persister.exists(case_number, f"{stem}.{e}")), None)
            if not name:
                return found
            found.append(name)
            n += 1

    def _save_json_versioned(self, case_number, name, data, stamp, out):
        """Save data as `name`; if that changes what was there, the old copy goes to versions/<stem>_<stamp>.json."""
        old = self.persister.read(case_number, name)
        if old is not None:
            if json.loads(old) == data:
                return
            self.persister.save_file(case_number, f"versions/{name[:-5]}_{stamp}.json", old)
        self.persister.save_json(case_number, name, data)
        out["saved"].append(name)

    def _classify(self, case_number, parties, status, now, out):
        """Label the case from its parties and the order text, and save the label if it is new or has changed."""
        texts = [re.sub(r"<[^>]+>", " ", html.unescape(h.get("proceedingText") or ""))
                 for h in status.get("caseHearingDetails") or []]
        result = classify(parties["respondents"], parties["complainants"], parties["case_type"], texts)
        old = self._json(case_number, "classification.json")
        if old and old.get("version") == CLASSIFIER_VERSION and (old["label"], old["reason"]) == (result["label"], result["reason"]):
            return
        self.persister.save_json(case_number, "classification.json",
                                 {**result, "version": CLASSIFIER_VERSION, "classified_at": now.isoformat(timespec="seconds")})
        out["saved"].append("classification.json")

    def relabel(self, case_number, parties, now=None):
        """Label a case again, from its saved status, if its label was made by older rules: nothing is asked of the
        portal. True if it was."""
        old = self._json(case_number, "classification.json")
        status = self._json(case_number, "status.json")
        if not status or (old and old.get("version") == CLASSIFIER_VERSION):
            return False
        self._classify(case_number, parties, status, now or datetime.now(), {"saved": []})
        return True

    def due(self, case_number, now=None):
        """Is it time to ask the portal about this case? Not every day: a case is asked about when its next
        hearing is today or past (the order and a new date are expected), when its last hearing was within RECENT
        (orders are uploaded late), or when it was last looked at STALE_AFTER ago (a date moved, a late order).
        A case with no next hearing date (a finished one) is only rechecked weekly for a late judgement."""
        now = now or datetime.now()
        meta = self._json(case_number, "meta.json")
        if not meta:
            return True
        label = self._json(case_number, "classification.json")
        if not label or label.get("version") != CLASSIFIER_VERSION:
            return True  # labelled by older rules (or not at all)
        age = now - datetime.fromisoformat(meta["fetched_at"])
        if not meta.get("next_hearing"):
            return age >= RECHECK_AFTER
        if "last_hearing" not in meta:
            return True  # meta.json from before last_hearing was kept; asked once more to fill it in
        today = now.date()
        if meta["next_hearing"] <= today.isoformat():
            return True
        if meta["last_hearing"] and today - datetime.strptime(meta["last_hearing"], "%Y-%m-%d").date() <= RECENT:
            return True
        return age >= STALE_AFTER

    def _find_status(self, case_number, parties):
        """(status, the number the portal knows the case by). The numbers the cause lists wrote it as are asked first,
        those of a circuit bench it was sent to leading, then the standard one if it is among them; then numbers worked out from it -- the standard form of a case
        only ever listed another way, and alias_number() -- which are used only if the parties match. The portal answers
        400 for a number it does not know; any other error is raised at once, as is the first 400 if none is known."""
        listed = (parties or {}).get("listed_as") or {case_number}
        moved = (parties or {}).get("moved_to") or set()
        numbers = [(n, False) for n in sorted(listed, key=lambda n: (n not in moved, n != case_number, n))]
        if parties:
            derived = [case_number] if case_number not in listed else []
            numbers += [(n, True) for n in derived + [alias_number(case_number)] if n]
        unknown = None
        for number, check in numbers:
            if unknown:
                time.sleep(REQUEST_DELAY)
            try:
                status = self._get_data("/case/caseFilingService/v2/getCaseStatus", caseNumber=number)
            except Exception as e:
                if getattr(getattr(e, "response", None), "status_code", None) != 400:
                    raise
                unknown = unknown or e
                continue
            if check and not same_parties(status, parties):
                raise ValueError(f"{number} is not the same case as {case_number}") from unknown
            return status, number
        raise unknown

    def sync(self, case_number, now=None, parties=None):
        """Bring one case up to date. A case not seen before is fetched in full. For one already held: the status
        and history are fetched again (two requests); an order is fetched only if it is not saved yet, or if its
        hearing entry in the status has changed since last time (the court re-issued the order or moved the next
        date), in which case it is saved as a new version if it really differs. Nothing is overwritten.
        With `parties` (an entry of tracked_cases()) the case is also labelled medical / not_medical / unclear
        (courts/medical.py classify) in classification.json; it is kept either way.
        Returns {"saved": [...], "skipped": [...], "failed": [...]} of file names."""
        now = now or datetime.now()
        stamp = f"{now:%Y%m%dT%H%M%S}"
        out = {"saved": [], "skipped": [], "failed": []}
        previous = self._json(case_number, "status.json")
        # Orders the portal did not have: {"<date>" (daily order) or "<date>_judgement": when it was last asked}
        misses = self._json(case_number, "no_orders.json") or {}
        misses_before = dict(misses)
        # by hearing and kind of order: a hearing's daily order and judgement are two entries with the same courtRoomHearingId
        entry_key = lambda h: (h.get("courtRoomHearingId"), h.get("orderTypeId"))
        old_entries = {entry_key(h): h for h in (previous or {}).get("caseHearingDetails") or []}
        status, portal_number = self._find_status(case_number, parties)  # everything is saved under case_number
        history = self._get_data("/case/caseFilingService/v2/getCaseDetailForHistoryDashBoard", caseNumber=portal_number)
        recheck_failed = False
        for h in status.get("caseHearingDetails") or []:
            kind = ORDER_KINDS.get(h.get("orderTypeId"))
            if not (h.get("dailyOrderStatus") and h.get("dateOfHearing") and kind):
                continue
            base = f"orders/{h['dateOfHearing']}_{kind}"
            versions = self._versions(case_number, base)
            old = old_entries.get(entry_key(h))
            recheck = bool(versions) and old is not None and old != h
            if versions and not recheck:
                out["skipped"].append(versions[-1])
                continue
            day = h["dateOfHearing"]
            miss = day if kind == "daily_order" else f"{day}_{kind}"
            # an order not held that the portal lacked lately is left for now, unless its hearing entry is new or changed
            if not versions and old == h and _asked_lately(misses.get(miss), day, now):
                continue
            try:
                content, ext = self.fetch_order(status["fillingReferenceNumber"], h["dateOfHearing"], h["orderTypeId"])
                if not versions:
                    name = f"{base}.{ext}"
                elif self.persister.read(case_number, versions[-1]) != content:
                    name = f"{base}_v{len(versions) + 1}.{ext}"
                    log(f"  new version of {base}")
                else:
                    out["skipped"].append(versions[-1])
                    continue
                self.persister.save_file(case_number, name, content)
                out["saved"].append(name)
                misses.pop(miss, None)
            except Exception as e:
                if isinstance(e, NoOrderDocument) and not versions:
                    misses[miss] = now.isoformat(timespec="seconds")
                else:  # including an empty answer for an order already held, which should not happen
                    log(f"  FAILED {case_number} {base}: {e}")
                    out["failed"].append(base)
                    recheck_failed = recheck_failed or recheck
            time.sleep(REQUEST_DELAY)
        # Every hearing in the history should have its order too; the status lists most of them, so try the
        # ones it does not (a daily order, as the portal's "View" button does). A hearing the portal has no
        # order for (HTTP 400, or an empty document) is noted in no_orders.json and only asked about again after
        # RECHECK_AFTER, or daily while the hearing is recent (orders are uploaded a little late). Other errors
        # are not noted.
        in_status = {h.get("dateOfHearing") for h in status.get("caseHearingDetails") or []}
        for day in history_hearing_dates(history):
            if day in in_status or self._versions(case_number, f"orders/{day}_daily_order"):
                continue
            if _asked_lately(misses.get(day), day, now):
                continue
            try:
                content, ext = self.fetch_order(status["fillingReferenceNumber"], day, 1)
                name = f"orders/{day}_daily_order.{ext}"
                self.persister.save_file(case_number, name, content)
                out["saved"].append(name)
                misses.pop(day, None)
            except Exception as e:
                no_order = isinstance(e, ValueError) or getattr(getattr(e, "response", None), "status_code", None) == 400
                if no_order:
                    misses[day] = now.isoformat(timespec="seconds")
                else:
                    log(f"  FAILED {case_number} order of history hearing {day}: {e}")
                    out["failed"].append(f"orders/{day}_daily_order")
            time.sleep(REQUEST_DELAY)
        if misses != misses_before:
            self.persister.save_json(case_number, "no_orders.json", dict(sorted(misses.items())))
        if portal_number in ((parties or {}).get("moved_to") or ()):
            status, history = carried_over(previous, self._json(case_number, "history.json"), status, history)
        # A failed re-check keeps the old status, so that the change is noticed again next time.
        if previous is None or not recheck_failed:
            self._save_json_versioned(case_number, "status.json", status, stamp, out)
            self._save_json_versioned(case_number, "history.json", history, stamp, out)
        if parties:
            self._classify(case_number, parties, status, now, out)
        next_hearing = (status.get("dateOfNextearing") or "")[:10] or None
        held = history_hearing_dates(history)
        self.persister.save_json(case_number, "meta.json",
                                 {"fetched_at": now.isoformat(timespec="seconds"), "next_hearing": next_hearing,
                                  "last_hearing": max(held) if held else None})
        return out

    def sync_all(self, cases, dry_run=False):
        """sync() each case that is due; `cases` is tracked_cases(). One case failing does not stop the rest.
        Returns the number of cases that had a failure."""
        case_numbers = sorted(cases)
        if not dry_run:
            relabelled = sum(self.relabel(number, cases[number]) for number in case_numbers)
            if relabelled:
                log(f"case details: {relabelled} cases labelled again under the current rules")
        failures = new = changed = idle = 0
        for number in case_numbers:
            if not self.due(number):
                idle += 1
                continue
            if dry_run:
                log(f"  would check {number}")
                continue
            try:
                had = self.persister.exists(number, "meta.json")
                result = self.sync(number, parties=cases[number])
            except Exception as e:
                failures += 1
                log(f"  FAILED {number}: {e}")
                time.sleep(REQUEST_DELAY)
                continue
            if result["failed"]:
                failures += 1
            if result["saved"]:
                if had:
                    changed += 1
                else:
                    new += 1
                log(f"  {number}: saved {', '.join(result['saved'])}")
            time.sleep(REQUEST_DELAY)
        log(f"case details: {len(case_numbers)} cases, {new} new, {changed} updated, {idle} not due, {failures} failed")
        return failures
