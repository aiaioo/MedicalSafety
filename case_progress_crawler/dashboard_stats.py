#!/usr/bin/env python3
"""Stores the tracker's "At a glance" numbers ready to show, so the front page and the public page need not walk
every case to draw them.

`python dashboard_stats.py` (the daily crawl runs it last, after judgements.py, because the panel includes the
judgement classes) works out viewer.analytics() for everything a visitor can ask for -- each court selection, with
and without the not_medical cases, for all years and for each year -- and replaces the dashboard_stats table with
them in one transaction. The pages read the stored row (viewer.panel_stats); a search, which changes the cases
counted, is still worked out live, as is any selection with no row.
"""

import time

import progress_storage as storage


def rebuild():
    """Recompute every stored row; returns how many."""
    import viewer  # imported here: the viewer imports the modules the daily crawl runs before this one
    rows = []
    for sel in viewer.SELECTIONS:
        years = viewer.load(sel)["years"]
        for non_medical in (False, True):
            for year in [0] + years:
                rows.append((sel, non_medical, year, viewer.compute_stats(sel, year, non_medical)))
    storage.replace_dashboard_stats(rows)
    return len(rows)


if __name__ == "__main__":
    started = time.time()
    print(f"{rebuild()} rows -> dashboard_stats in {time.time() - started:.0f}s")
