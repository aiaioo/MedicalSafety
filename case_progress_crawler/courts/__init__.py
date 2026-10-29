"""Courts (and other tribunals) whose case progress is crawled.

Each court is a Court subclass in its own module here, with a short `id` that names its
sub-folder in pdfs/, storage/ and manually_downloaded/, so one court's data never mixes
with another's. To add a court: write the subclass, then list it in COURTS below.
"""

import os
import urllib3
from abc import ABC, abstractmethod
from datetime import datetime

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def make_session(user_agent):
    """Requests session with TLS verification off, so an expired or misconfigured
    certificate on a court's site does not stop the daily run."""
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    session.verify = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


class Court(ABC):
    id: str    # folder name under pdfs/, storage/ and manually_downloaded/; also the --court value
    name: str  # for display
    city: str  # where the court sits, e.g. "Bangalore"
    state: str
    in_court_list = True  # False for a crawler that covers many courts, which court_list.py lists itself

    @property
    def pdf_dir(self):
        return os.path.join(BASE_DIR, "pdfs", self.id)

    @property
    def manual_dir(self):
        return os.path.join(BASE_DIR, "manually_downloaded", self.id)

    @abstractmethod
    def download(self, dry_run=False, back_days=None):
        """Fetch whatever is new from the court into pdfs/<id>/, remembering what has been
        fetched (storage.load_state / save_state) so nothing is downloaded twice. back_days asks a
        court that publishes by date to go back that far; the others ignore it.
        Return the number of failures (0 = success)."""


def all_courts():
    from .consumer import EJagritiConsumer
    from .kmc import KMC
    return {c.id: c for c in (KMC(), EJagritiConsumer())}  # register new courts here


def get_court(court_id):
    return all_courts()[court_id]
