#!/usr/bin/env python3
"""Stores the tracker's analytics ready to show, one state at a time, so the pages need not walk every case to draw them.

`python dashboard_stats.py [STATE ...]` (the daily crawl runs it last, after judgements.py, because the panel includes the
judgement classes) works out, for each state (default: every state with analysed courts):
  * dashboard_stats_by_state: viewer.analytics() -- the "At a glance" panel -- for each court selection of the state, with
    and without the not_medical cases, for all years, for each year and (for the both-courts panel) for each period of years;
  * analytics_pages: the hospitals and advocates tables of the state, for each period of years, with and without the
    not_medical cases.
Each state's rows replace its old ones in one transaction. A state is loaded, digested and dropped before the next, so
the memory used is that of the largest state, not of all of them; and a page reads one stored row, whatever the number of states.
A search, which changes the cases counted, is still worked out live, as is any state with no row.
"""

import sys
import time

import progress_storage as storage


def rebuild_state(viewer, state):
    """Recompute and store one state's rows; returns how many."""
    stats, pages = [], []
    with viewer.in_state(state):
        for sel in viewer.selections(state):
            years = viewer.load(sel, state)["years"]
            periods = [viewer.period_code(spec) for spec in viewer.period_specs()[1:]] if sel == "all" else []
            for non_medical in (False, True):
                for year in [0] + years + periods:
                    stats.append((sel, non_medical, year, viewer.compute_stats(sel, year, non_medical, state)))
        data = viewer.load("all", state)
        for page in ("hospitals", "advocates"):
            for key, *_ in viewer.period_specs():
                for non_medical in (False, True):
                    pages.append((page, key, non_medical, viewer.build_analytics_page(page, key, non_medical, data)))
    storage.replace_dashboard_stats(state, stats)
    storage.replace_analytics_pages(state, pages)
    viewer._cache.clear()  # the next state is not held beside this one
    return len(stats) + len(pages)


def rebuild(only=None):
    """Recompute every stored row of the given states (default all); returns how many."""
    import viewer  # imported here: the viewer imports the modules the daily crawl runs before this one
    viewer._short.clear()
    return sum(rebuild_state(viewer, st) for st in storage.analysis_states() if not only or st in only)


if __name__ == "__main__":
    started = time.time()
    print(f"{rebuild(sys.argv[1:] or None)} rows -> dashboard_stats_by_state, analytics_pages in {time.time() - started:.0f}s")
