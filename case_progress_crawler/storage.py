"""Single access point for the crawler's stored JSON and CSV data (all in ./storage).

  state.json       downloader.py: which pre- and post-hearing PDFs have been seen
  enquiries.json   analysis.py:   everything, one object per enquiry
  status.csv       analysis.py:   one row per enquiry
  parties.csv      analysis.py:   one row per party
  timeline.csv     analysis.py:   one row per enquiry per hearing date
  links.json       viewer.py:     hand-entered links per case

Writes are atomic (temp file + rename). Running this file lists what is stored.
"""

import csv
import json
import os
from pathlib import Path

STORAGE_DIR = Path(__file__).parent / "storage"


def path(name):
    return STORAGE_DIR / name


def mtimes(*names):
    """Modification times of the named files, for cache invalidation."""
    return tuple(path(n).stat().st_mtime for n in names)


def _atomic_write(name, write, newline=None):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    dest = path(name)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "w", newline=newline, encoding="utf-8") as f:
        write(f)
    os.replace(tmp, dest)


def read_json(name, default=None):
    try:
        return json.loads(path(name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(name, data, **kwargs):
    _atomic_write(name, lambda f: json.dump(data, f, indent=2, ensure_ascii=False, **kwargs))


def read_csv(name):
    with open(path(name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(name, header, rows):
    def write(f):
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    _atomic_write(name, write, newline="")


def load_state():
    return read_json("state.json", {"seen": {}})


def save_state(state):
    write_json("state.json", state, sort_keys=True)


def load_links():
    return read_json("links.json", {})


def save_links(links):
    write_json("links.json", links)


if __name__ == "__main__":
    for p in sorted(STORAGE_DIR.glob("*")):
        print(f"{p.name:16} {p.stat().st_size:>10,} bytes")
