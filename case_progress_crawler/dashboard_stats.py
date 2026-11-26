#!/usr/bin/env python3
"""Stores the tracker's pages and analytics ready to show, one state at a time, so the web app need not hold any cases.

`python dashboard_stats.py [STATE ...]` (the daily crawl runs it last, after judgements.py, because the panel includes the
judgement classes) builds each state's cases from the analysis (viewer.build_state) and works out, for each state
(default: every state with analysed courts):
  * the view_* tables: every case and hearing as the tracker's pages show them (viewer.view_rows);
  * dashboard_stats_by_state: viewer.analytics() -- the "At a glance" panel -- for each court selection of the state, with
    and without the not_medical cases, for all years, for each year and (for the both-courts panel) for each period of years;
  * analytics_pages: the hospitals and advocates tables of the state, for each period of years, with and without the
    not_medical cases.
Each state's rows replace its old ones in one transaction, the pages first. A state is loaded, digested and dropped before the next, so
the memory used is that of the largest state, not of all of them; and a page reads one stored row, whatever the number of states.
A search, which changes the cases counted, is still worked out live, as is any state with no row.

`python dashboard_stats.py --pages-if-needed` (the deploy runs it) builds only the view_* tables, and only of the
states that have none or were built in an older format (viewer.VIEW_FORMAT).
"""

import sys
import time

import progress_storage as storage


def build_pages(viewer, state):
    """Build a state's cases from the analysis and store the rows of its pages; returns the built data."""
    data = viewer.build_state(state)
    storage.replace_views(state, viewer.VIEW_FORMAT, *viewer.view_rows(state, data))
    return data


def rebuild_state(viewer, state):
    """Rebuild one state's pages and recompute its stored analytics; returns how many analytics rows."""
    stats, pages = [], []
    with viewer.in_state(state):
        with viewer.holding(state, build_pages(viewer, state)):
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
    return len(stats) + len(pages)


def rebuild(only=None):
    """Rebuild the pages and recompute every stored row of the given states (default all); returns how many rows."""
    import viewer  # imported here: the viewer imports the modules the daily crawl runs before this one
    viewer._short.clear()
    return sum(rebuild_state(viewer, st) for st in storage.analysis_states() if not only or st in only)


def pages_if_needed():
    """Build the pages of the states that have none in the current format; returns those states."""
    import viewer
    viewer._short.clear()
    built = storage.view_formats()
    needed = [st for st in storage.analysis_states() if built.get(st) != viewer.VIEW_FORMAT]
    for state in needed:
        with viewer.in_state(state):
            build_pages(viewer, state)
    return needed


if __name__ == "__main__":
    started = time.time()
    if sys.argv[1:] == ["--pages-if-needed"]:
        print(f"pages built for: {', '.join(pages_if_needed()) or 'no state (all up to date)'}, in {time.time() - started:.0f}s")
    else:
        print(f"{rebuild(sys.argv[1:] or None)} rows -> dashboard_stats_by_state, analytics_pages in {time.time() - started:.0f}s")
