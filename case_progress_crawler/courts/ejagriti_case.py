"""Case details (status, hearing history and the order PDFs) of consumer cases, from e-Jagriti.

ConsumerCaseDownloader calls the portal's APIs directly (they need no login or captcha):
  getCaseStatus                   stage, next date, parties, and one entry per hearing (with the text of the
                                  order and, when a PDF exists, its orderTypeId: 1 = daily order, 2 = final order)
  getCaseDetailForHistoryDashBoard  the history list (hearing, bench, court)
  getDailyOrderJudgementPdf       one PDF per entry that has an order, as the page's "View" buttons do

If the portal starts demanding a captcha, these requests will fail with an HTTP error.

What is saved, and where, is up to a CaseDetailsPersister, so the downloader knows nothing about folders:
DiskPersister files them under case_details/consumer/<case key>/.
"""

import base64
import os
import time
from abc import ABC, abstractmethod

from . import BASE_DIR, log, make_session
from .ejagriti import API_BASE, TIMEOUT, USER_AGENT

REQUEST_DELAY = 0.5     # seconds between PDF requests
ORDER_KINDS = {1: "daily_order", 2: "judgement"}  # orderTypeId -> file name part


class CaseDetailsPersister(ABC):
    """Where a case's downloaded details go. `name` is a file name within the case, e.g. "status.json"."""

    @abstractmethod
    def exists(self, case_number, name):
        """Has this file already been saved?"""

    @abstractmethod
    def save_json(self, case_number, name, data):
        """Save a JSON-able value; return where it was saved."""

    @abstractmethod
    def save_file(self, case_number, name, content):
        """Save the bytes of an order (PDF or HTML); return where it was saved."""


class DiskPersister(CaseDetailsPersister):
    """case_details/consumer/<case key>/status.json, history.json and orders/<date>_<kind>.pdf or .html (written atomically)."""

    def __init__(self, root=None):
        self.root = root or os.path.join(BASE_DIR, "case_details", "consumer")

    def _path(self, case_number, name):
        from analysis_consumer import case_key  # the same key the analysis and the viewer use
        return os.path.join(self.root, case_key(case_number), *name.split("/"))

    def exists(self, case_number, name):
        return os.path.exists(self._path(case_number, name))

    def _write(self, case_number, name, write, mode):
        dest = self._path(case_number, name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
            write(f)
        os.replace(tmp, dest)
        return dest

    def save_json(self, case_number, name, data):
        import json
        return self._write(case_number, name, lambda f: json.dump(data, f, indent=1, ensure_ascii=False), "w")

    def save_file(self, case_number, name, content):
        return self._write(case_number, name, lambda f: f.write(content), "wb")


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
        older ones are an HTML page in the same field. The path keeps the double slash the portal's own page uses."""
        data = self._get_data("//courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf",
                              filingReferenceNumber=filing_reference_number, dateOfHearing=date_of_hearing,
                              orderTypeId=order_type_id)
        text = data.get("dailyOrderPdf") or ""
        if text.lstrip().lower().startswith(("<html", "<!doctype")):
            return text.encode("utf-8"), "html"
        pdf = base64.b64decode(text)
        if not pdf.startswith(b"%PDF"):
            raise ValueError("order is neither a PDF nor HTML")
        return pdf, "pdf"

    def download(self, case_number):
        """Get one case's details and orders and hand them to the persister.
        Returns {"saved": [...], "skipped": [...], "failed": [...]} of file names."""
        out = {"saved": [], "skipped": [], "failed": []}
        status = self._get_data("/case/caseFilingService/v2/getCaseStatus", caseNumber=case_number)
        history = self._get_data("/case/caseFilingService/v2/getCaseDetailForHistoryDashBoard", caseNumber=case_number)
        for name, data in (("status.json", status), ("history.json", history)):
            self.persister.save_json(case_number, name, data)  # always: they change as the case moves
            out["saved"].append(name)
        for h in status.get("caseHearingDetails") or []:
            kind = ORDER_KINDS.get(h.get("orderTypeId"))
            if not (h.get("dailyOrderStatus") and h.get("dateOfHearing") and kind):
                continue
            base = f"orders/{h['dateOfHearing']}_{kind}"
            have = next((n for n in (f"{base}.pdf", f"{base}.html") if self.persister.exists(case_number, n)), None)
            if have:
                out["skipped"].append(have)  # an order does not change once issued
                continue
            try:
                content, ext = self.fetch_order(status["fillingReferenceNumber"], h["dateOfHearing"], h["orderTypeId"])
                name = f"{base}.{ext}"
                self.persister.save_file(case_number, name, content)
                out["saved"].append(name)
            except Exception as e:
                log(f"  FAILED {base}: {e}")
                out["failed"].append(base)
            time.sleep(REQUEST_DELAY)
        return out

