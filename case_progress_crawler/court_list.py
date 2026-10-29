#!/usr/bin/env python3
"""Refresh storage/courts.json, the list of courts the crawler knows about, by city.

It holds the crawler's own courts (courts/, e.g. the Karnataka Medical Council) and the
consumer courts of Bangalore read from the e-Jagriti portal (courts/ejagriti.py). Courts
come and go on the portal, so each run records when a court was first and last seen; one
that disappears is kept, marked "active": false, rather than dropped.

Usage:  python court_list.py
"""

import sys
from datetime import date

import storage
from courts import all_courts, ejagriti, log


def local_courts():
    return [{"id": c.id, "name": c.name, "kind": "medical council", "city": c.city, "state": c.state,
             "source": "courts/"} for c in all_courts().values()]


def merge(old, current, today):
    """Add or update the current courts; mark those no longer listed inactive."""
    merged = {}
    for c in current:
        prev = old.get(c["id"], {})
        merged[c["id"]] = {**c, "active": True, "first_seen": prev.get("first_seen", today), "last_seen": today}
    for cid, prev in old.items():
        if cid not in merged:
            merged[cid] = {**prev, "active": False}
    return merged


def main():
    today = date.today().isoformat()
    old = storage.load_courts()
    current = local_courts()
    try:
        current += ejagriti.bangalore_courts()
        fetched = True
    except Exception as e:
        log(f"ERROR fetching consumer courts from e-Jagriti: {e}")
        fetched = False
    if not fetched:  # keep what we had rather than marking every consumer court gone
        current += [c for cid, c in old.items() if c["source"] != "courts/"]
    merged = merge(old, current, today)
    storage.save_courts(merged)

    for city in sorted({c["city"] for c in merged.values()}):
        log(f"{city}:")
        for c in merged.values():
            if c["city"] == city:
                log(f"  {c['name']}  [{c['kind']}]{'' if c['active'] else '  (no longer listed)'}")
    return 0 if fetched else 1


if __name__ == "__main__":
    sys.exit(main())
