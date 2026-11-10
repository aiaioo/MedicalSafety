#!/usr/bin/env python3
"""One-off: copy the crawler's old data files into the PostgreSQL database (through storage.py).

Reads storage/ (courts.json, <court>/state.json, links.json, the analysis CSVs and enquiries.json), causelists/consumer/
and the JSON documents of case_details/consumer/<case key>/ (status, history, meta, classification, no_orders and
versions/). The order PDFs and HTML pages stay where they are. Safe to run again: it overwrites what it wrote.

Usage:  python migrate_files_to_db.py [--dry-run]
"""

import argparse
import csv
import json
import re
from pathlib import Path

import storage

OLD_STORAGE = storage.BASE_DIR / "storage"
CAUSELISTS = storage.BASE_DIR / "causelists" / "consumer"
CASE_DETAILS = storage.CASE_DETAILS_DIR
LIST_FILE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:_(\d+))?\.json$")
VERSION_FILE = re.compile(r"(.+)_(\d{8}T\d{6})\.json$")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def migrate_court(court_dir, dry):
    court = court_dir.name
    out = []
    if (court_dir / "state.json").exists():
        state = read_json(court_dir / "state.json")
        out.append(f"{len(state['seen'])} crawl-state entries")
        if not dry:
            storage.save_state(court, state)
    if (court_dir / "links.json").exists():
        links = read_json(court_dir / "links.json")
        out.append(f"links of {len(links)} cases")
        if not dry:
            for case_key, items in links.items():
                storage.save_case_links(court, case_key, items)
    if (court_dir / "status.csv").exists():
        rows = {t: read_csv(court_dir / f"{t}.csv") if (court_dir / f"{t}.csv").exists() else []
                for t in storage.ANALYSIS_TABLES}
        tables = {t: [[r.get(c, "") for c in storage.ANALYSIS_TABLES[t]] for r in rs] for t, rs in rows.items()}
        enquiries = read_json(court_dir / "enquiries.json") if (court_dir / "enquiries.json").exists() else {}
        out.append(f"analysis: {', '.join(f'{len(v)} {t}' for t, v in tables.items())}, {len(enquiries)} enquiries")
        if not dry:
            storage.replace_analysis(court, enquiries, **tables)
    print(f"{court}: {'; '.join(out) or 'nothing'}")


def migrate_cause_lists(dry):
    n = 0
    for path in sorted(CAUSELISTS.glob("*/*.json")):
        m = LIST_FILE.search(path.name)
        if not m:
            continue
        n += 1
        if not dry:
            storage.save_cause_list(path.parent.name, m.group(1), int(m.group(2) or 1), read_json(path))
    print(f"cause lists: {n}")


def migrate_case_details(dry):
    docs = versions = 0
    for case_dir in sorted(p for p in CASE_DETAILS.iterdir() if p.is_dir()):
        for path in case_dir.glob("*.json"):
            docs += 1
            if not dry:
                storage.save_case_document(case_dir.name, path.name, read_json(path))
        for path in (case_dir / "versions").glob("*.json"):
            m = VERSION_FILE.fullmatch(path.name)
            if m:
                versions += 1
                if not dry:
                    storage.save_case_document_version(case_dir.name, f"{m.group(1)}.json", m.group(2), read_json(path))
    print(f"case documents: {docs}, earlier versions: {versions}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="count what would be copied")
    dry = ap.parse_args().dry_run
    if not dry:
        storage.init_db()
    if (OLD_STORAGE / "courts.json").exists():
        courts = read_json(OLD_STORAGE / "courts.json")
        print(f"courts: {len(courts)}")
        if not dry:
            storage.save_courts(courts)
    for court_dir in sorted(p for p in OLD_STORAGE.iterdir() if p.is_dir()):
        migrate_court(court_dir, dry)
    migrate_cause_lists(dry)
    migrate_case_details(dry)


if __name__ == "__main__":
    main()
