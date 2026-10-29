"""Case details (status, hearing history and the order PDFs) of consumer cases, from e-Jagriti.

The portal shows these on https://e-jagriti.gov.in/case-history-case-status only after a captcha, which
a person has to read. ConsumerCaseDetailsDownloader therefore follows the same steps as the page, with the
person in the loop: it fetches a captcha, hands the image to `prompt` for a human to read, verifies the
answer with the portal and, only if the portal accepts it, fetches that one case's details. Every case needs
its own captcha; a wrong answer gets a fresh captcha (up to MAX_ATTEMPTS), and an empty answer abandons the case.

For a case it fetches
  getCaseStatus                   stage, next date, parties, and one entry per hearing (with the text of the
                                  order and, when a PDF exists, its orderTypeId: 1 = daily order, 2 = final order)
  getCaseDetailForHistoryDashBoard  the history list (hearing, bench, court)
  getDailyOrderJudgementPdf       one PDF per entry that has an order, as the page's "View" buttons do

What is saved, and where, is up to a CaseDetailsPersister, so the downloader knows nothing about folders:
DiskPersister files them under case_details/consumer/<case key>/.
"""

import base64
import os
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod

from . import BASE_DIR, log, make_session
from .ejagriti import API_BASE, TIMEOUT, USER_AGENT

MAX_ATTEMPTS = 3        # captchas offered per case before giving up
REQUEST_DELAY = 0.5     # seconds between PDF requests
ORDER_KINDS = {1: "daily_order", 2: "judgement"}  # orderTypeId -> file name part


class CaptchaAbandoned(Exception):
    """The person gave no answer, or every answer offered was rejected."""


class CaseDetailsPersister(ABC):
    """Where a case's downloaded details go. `name` is a file name within the case, e.g. "status.json"."""

    @abstractmethod
    def exists(self, case_number, name):
        """Has this file already been saved?"""

    @abstractmethod
    def save_json(self, case_number, name, data):
        """Save a JSON-able value; return where it was saved."""

    @abstractmethod
    def save_pdf(self, case_number, name, content):
        """Save the bytes of a PDF; return where it was saved."""


class DiskPersister(CaseDetailsPersister):
    """case_details/consumer/<case key>/status.json, history.json and orders/<date>_<kind>.pdf (written atomically)."""

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

    def save_pdf(self, case_number, name, content):
        return self._write(case_number, name, lambda f: f.write(content), "wb")


class ConsoleCaptchaPrompt:
    """Shows the captcha to the person at the terminal: saves the image, opens it where it can (macOS `open`)
    and asks for what it says. Returns "" if they just press Enter."""

    def __call__(self, png):
        path = os.path.join(tempfile.gettempdir(), "ejagriti-captcha.png")
        with open(path, "wb") as f:
            f.write(png)
        try:
            subprocess.run(["open", path], check=False, capture_output=True)
        except OSError:
            pass
        print(f"Captcha image: {path}")
        return input("Characters in the captcha (Enter to skip this case): ").strip()


class ConsumerCaseDetailsDownloader:
    def __init__(self, persister, prompt, session=None):
        self.persister = persister
        self.prompt = prompt  # callable(png bytes) -> the characters a person read; "" abandons
        self.session = session or make_session(USER_AGENT)

    def _request(self, method, path, **kwargs):
        resp = self.session.request(method, f"{API_BASE}{path}", timeout=TIMEOUT, **kwargs)
        return resp

    def verify_captcha(self):
        """Offer captchas until the portal accepts a person's answer. Raises CaptchaAbandoned."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = self._request("GET", "/user/auth/v2/generateCaptcha")
            resp.raise_for_status()
            image = resp.json()["data"]["base64Image"]
            png = base64.b64decode(image.split(",", 1)[-1])
            answer = self.prompt(png)
            if not answer:
                raise CaptchaAbandoned("no answer given")
            resp = self._request("POST", "/user/auth/v2/verifyCaptcha", json={"captcha": answer, "onlyVerifyCaptcha": False})
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.ok and body.get("status") == 200:
                return
            log(f"  captcha rejected ({body.get('message') or resp.status_code}); attempt {attempt} of {MAX_ATTEMPTS}")
        raise CaptchaAbandoned(f"{MAX_ATTEMPTS} captchas rejected")

    def _get_data(self, path, **params):
        resp = self._request("GET", path, params=params)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != 200:
            raise ValueError(f"{path}: {body.get('message')}")
        return body["data"]

    def fetch_order(self, filing_reference_number, date_of_hearing, order_type_id):
        """The PDF of one order, as bytes. The portal returns it base64-encoded inside JSON; the path keeps the
        double slash the portal's own page uses."""
        data = self._get_data("//courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf",
                              filingReferenceNumber=filing_reference_number, dateOfHearing=date_of_hearing,
                              orderTypeId=order_type_id)
        pdf = base64.b64decode(data["dailyOrderPdf"])
        if not pdf.startswith(b"%PDF"):
            raise ValueError("order is not a PDF")
        return pdf

    def download(self, case_number):
        """Get one case's details and orders and hand them to the persister. Needs a person to read a captcha.
        Returns {"saved": [...], "skipped": [...], "failed": [...]} of file names."""
        self.verify_captcha()
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
            name = f"orders/{h['dateOfHearing']}_{kind}.pdf"
            if self.persister.exists(case_number, name):
                out["skipped"].append(name)  # an order does not change once issued
                continue
            try:
                pdf = self.fetch_order(status["fillingReferenceNumber"], h["dateOfHearing"], h["orderTypeId"])
                self.persister.save_pdf(case_number, name, pdf)
                out["saved"].append(name)
            except Exception as e:
                log(f"  FAILED {name}: {e}")
                out["failed"].append(name)
            time.sleep(REQUEST_DELAY)
        return out
