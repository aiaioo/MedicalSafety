"""Single access point for the crawler's stored JSON and CSV data (all in ./storage).

Each court (see courts/) has its own folder, storage/<court>/, e.g. for "kmc":

  state.json       downloader.py: which pre- and post-hearing PDFs have been seen
  enquiries.json   analysis.py:   everything, one object per enquiry
  status.csv       analysis.py:   one row per enquiry
  parties.csv      analysis.py:   one row per party
  timeline.csv     analysis.py:   one row per enquiry per hearing date
  links.json       viewer.py:     hand-entered links per case

courts.json (court_list.py: every known court, by city) is shared, so it lives directly in
storage/ (court id "", see load_courts).

Every function below takes the court id first. Writes are atomic (temp file + rename). Running this file lists what is stored.
"""

import csv
import json
import os
from pathlib import Path

STORAGE_DIR = Path(__file__).parent / "storage"


def court_dir(court):
    return STORAGE_DIR / court


def path(court, name):
    return court_dir(court) / name


def mtimes(court, *names):
    """Modification times of the named files, for cache invalidation."""
    return tuple(path(court, n).stat().st_mtime for n in names)


def _atomic_write(court, name, write, newline=None):
    court_dir(court).mkdir(parents=True, exist_ok=True)
    dest = path(court, name)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "w", newline=newline, encoding="utf-8") as f:
        write(f)
    os.replace(tmp, dest)


def read_json(court, name, default=None):
    try:
        return json.loads(path(court, name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(court, name, data, **kwargs):
    _atomic_write(court, name, lambda f: json.dump(data, f, indent=2, ensure_ascii=False, **kwargs))


def read_csv(court, name):
    with open(path(court, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(court, name, header, rows):
    def write(f):
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    _atomic_write(court, name, write, newline="")


def load_state(court):
    return read_json(court, "state.json", {"seen": {}})


def save_state(court, state):
    write_json(court, "state.json", state, sort_keys=True)


def load_courts():
    return read_json("", "courts.json", {})


def save_courts(courts):
    write_json("", "courts.json", courts)


def load_links(court):
    return read_json(court, "links.json", {})


def save_links(court, links):
    write_json(court, "links.json", links)


if __name__ == "__main__":
    for p in sorted(STORAGE_DIR.glob("*/*")):
        print(f"{p.parent.name}/{p.name:16} {p.stat().st_size:>10,} bytes")
