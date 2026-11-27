"""The single access point for everything the crawler stores.

Structured data lives in PostgreSQL (database `case_progress`, schema in db/schema.sql); nothing else in the
project talks to the database or reads/writes data files -- they call the functions here, which take and return
plain dicts, lists and strings. Set CASE_PROGRESS_DATABASE_URL to use another database (default: postgresql:///case_progress; not DATABASE_URL, which
the case manager -- that mounts the viewer in its own process -- uses for its own database);
`python progress_storage.py init` creates the tables, `python progress_storage.py status` counts the rows.

In the database (by court id, e.g. "kmc", "consumer", where a function takes one):
  courts            court_list.py:       every known court, by city                      load_courts / save_courts
  crawl_state       the downloaders:     which PDFs / cause lists have been seen         load_state / save_state
  case_links        viewer.py:           hand-entered links per case                     load_links / save_case_links
  cause_lists       courts/consumer.py:  the e-Jagriti cause lists, every revision       save_cause_list / latest_cause_lists
  case_documents    courts/ejagriti_case: a case's status, history, meta, classification  load_case_document / save_case_document ...
  case_orders       courts/ejagriti_case: which stored file each order of a case is       order_files / save_order_file ...
  analysis_*        analysis*.py:        status, parties, advocates, timeline, enquiries replace_analysis / load_analysis
  view_*            dashboard_stats.py:  the cases and hearings as the pages show them     replace_views / view_*

On disk (files, not data): the order PDFs and HTML pages in case_details/store/, one file for each distinct content
however many orders are that file (case_orders says which), and the KMC's PDFs in pdfs/, which the court crawlers and
the viewer handle as paths. Every read here is of the whole current state; every write is one transaction.
"""

import hashlib
import json
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

BASE_DIR = Path(__file__).parent
SCHEMA_FILE = BASE_DIR / "db" / "schema.sql"
ORDER_STORE_DIR = BASE_DIR / "case_details" / "store"  # <first 2 of the SHA-256>/<SHA-256>.pdf|html
CASE_DETAILS_DIR = BASE_DIR / "case_details" / "consumer"  # where orders were before the store (migrate_orders_to_store.py)
DEFAULT_DSN = "postgresql:///case_progress"

_pool = None
_pool_lock = threading.Lock()  # the viewer warms its cache in a thread while requests arrive: make the pool once


# ---------------------------------------------------------------------------------------------
# Connection

def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(1, 8, os.environ.get("CASE_PROGRESS_DATABASE_URL", DEFAULT_DSN))
    return _pool


@contextmanager
def _db():
    """A cursor in a transaction: committed when the block ends, rolled back if it raises."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _json(value):
    return psycopg2.extras.Json(value, dumps=lambda v: json.dumps(v, ensure_ascii=False))


def init_db():
    """Create the tables if they are not there (db/schema.sql is re-runnable)."""
    with _db() as cur:
        cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------
# Courts, crawl state, links

def load_courts():
    """{court id: court} for every known court (see court_list.py)."""
    with _db() as cur:
        cur.execute("SELECT court_id, data FROM courts ORDER BY court_id")
        return {r["court_id"]: r["data"] for r in cur.fetchall()}


def save_courts(courts):
    with _db() as cur:
        cur.execute("DELETE FROM courts")
        psycopg2.extras.execute_values(cur, "INSERT INTO courts (court_id, data) VALUES %s",
                                       [(cid, _json(c)) for cid, c in courts.items()])


def load_state(court):
    """{"seen": {key: info}}: what a court's crawler has already downloaded."""
    with _db() as cur:
        cur.execute("SELECT key, value FROM crawl_state WHERE court = %s", (court,))
        return {"seen": {r["key"]: r["value"] for r in cur.fetchall()}}


def save_state(court, state):
    """Store the state of load_state(): new and changed entries are written, entries no longer in it removed."""
    seen = state["seen"]
    with _db() as cur:
        cur.execute("SELECT key FROM crawl_state WHERE court = %s", (court,))
        gone = [r["key"] for r in cur.fetchall() if r["key"] not in seen]
        if gone:
            cur.execute("DELETE FROM crawl_state WHERE court = %s AND key = ANY(%s)", (court, gone))
        if seen:
            psycopg2.extras.execute_values(
                cur, "INSERT INTO crawl_state (court, key, value) VALUES %s "
                     "ON CONFLICT (court, key) DO UPDATE SET value = EXCLUDED.value WHERE crawl_state.value IS DISTINCT FROM EXCLUDED.value",
                [(court, k, _json(v)) for k, v in seen.items()])


def load_links(court):
    """{case key: [{"url", "text", "note"}, ...]} of the links entered by hand for a court's cases."""
    with _db() as cur:
        cur.execute("SELECT case_key, links FROM case_links WHERE court = %s ORDER BY case_key", (court,))
        return {r["case_key"]: r["links"] for r in cur.fetchall()}


def save_case_links(court, case_key, links):
    """Replace one case's links (an empty list removes them)."""
    with _db() as cur:
        if links:
            cur.execute("INSERT INTO case_links (court, case_key, links) VALUES (%s, %s, %s) "
                        "ON CONFLICT (court, case_key) DO UPDATE SET links = EXCLUDED.links", (court, case_key, _json(links)))
        else:
            cur.execute("DELETE FROM case_links WHERE court = %s AND case_key = %s", (court, case_key))


# ---------------------------------------------------------------------------------------------
# Cause lists

def save_cause_list(commission_id, day, version, doc):
    """Store one version of a commission's cause list for a day (a revised list is a new version)."""
    with _db() as cur:
        cur.execute("INSERT INTO cause_lists (commission_id, list_date, version, doc) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (commission_id, list_date, version) DO UPDATE SET doc = EXCLUDED.doc, saved_at = now()",
                    (str(commission_id), day, version, _json(doc)))


def latest_cause_lists():
    """[{"commission_id", "date" (YYYY-MM-DD), "version", "doc"}] -- the highest version of each commission's
    list for each date, ordered by commission then date."""
    with _db() as cur:
        cur.execute("SELECT DISTINCT ON (commission_id, list_date) commission_id, list_date::text AS date, version, doc "
                    "FROM cause_lists ORDER BY commission_id, list_date, version DESC")
        return list(cur.fetchall())


def iter_latest_cause_lists(batch=20):
    """Like latest_cause_lists(), but yields one list at a time (a server-side cursor fetching `batch` rows
    per round trip), so a caller that only digests each list never holds them all in memory."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(name="latest_cause_lists", cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.itersize = batch
            cur.execute("SELECT DISTINCT ON (commission_id, list_date) commission_id, list_date::text AS date, version, doc "
                        "FROM cause_lists ORDER BY commission_id, list_date, version DESC")
            yield from cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ---------------------------------------------------------------------------------------------
# Case details: the documents the portal gave for each case, and the order files

def load_case_document(case_key, name):
    """A case's document ("status.json", "history.json", "meta.json", "classification.json", "no_orders.json"),
    or None if it has not been saved."""
    with _db() as cur:
        cur.execute("SELECT doc FROM case_documents WHERE case_key = %s AND name = %s", (case_key, name))
        row = cur.fetchone()
        return row["doc"] if row else None


def save_case_document(case_key, name, doc):
    with _db() as cur:
        cur.execute("INSERT INTO case_documents (case_key, name, doc) VALUES (%s, %s, %s) "
                    "ON CONFLICT (case_key, name) DO UPDATE SET doc = EXCLUDED.doc, saved_at = now()",
                    (case_key, name, _json(doc)))


def load_case_documents(name, courts=None, case_keys=None):
    """{case key: document} of one kind of document, for every case that has it -- or, with `courts` (analysed court ids),
    for the cases of those courts only, or of those `case_keys`."""
    with _db() as cur:
        if case_keys is not None:
            cur.execute("SELECT case_key, doc FROM case_documents WHERE name = %s AND case_key = ANY(%s)", (name, list(case_keys)))
        elif courts is None:
            cur.execute("SELECT case_key, doc FROM case_documents WHERE name = %s", (name,))
        else:
            cur.execute("SELECT case_key, doc FROM case_documents WHERE name = %s "
                        "AND case_key IN (SELECT case_key FROM analysis_status WHERE court = ANY(%s))", (name, list(courts)))
        return {r["case_key"]: r["doc"] for r in cur.fetchall()}


def load_status_summaries(courts=None, case_keys=None):
    """{case key: status document} cut down to what the analyses and the viewer read of it -- caseStage, caseFilingDate,
    filedInComissionName and, of each entry of caseHearingDetails, dateOfHearing, proceedingText and caseStage -- for
    the cases of the analysed `courts`, or of `case_keys`. The whole documents are several times the size."""
    where, value = (("case_key = ANY(%s)", list(case_keys)) if case_keys is not None else
                    ("case_key IN (SELECT case_key FROM analysis_status WHERE court = ANY(%s))", list(courts)))
    with _db() as cur:
        cur.execute(f"""
            SELECT case_key, jsonb_build_object(
                'caseStage', doc->'caseStage', 'caseFilingDate', doc->'caseFilingDate',
                'filedInComissionName', doc->'filedInComissionName',
                'caseHearingDetails', (
                    SELECT jsonb_agg(jsonb_build_object('dateOfHearing', h->'dateOfHearing',
                                     'proceedingText', h->'proceedingText', 'caseStage', h->'caseStage') ORDER BY n)
                    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(doc->'caseHearingDetails') = 'array'
                                                   THEN doc->'caseHearingDetails' ELSE '[]'::jsonb END)
                         WITH ORDINALITY AS e(h, n))) AS doc
            FROM case_documents WHERE name = 'status.json' AND {where}""", (value,))
        return {r["case_key"]: r["doc"] for r in cur.fetchall()}


def load_status_advocates(case_keys):
    """{case key: status document} cut down to complainantAdvocate and respondentAdvocate, all the consumer analysis
    reads of it, for those `case_keys`. The whole documents, with the record of every hearing, are far larger."""
    with _db() as cur:
        cur.execute("""
            SELECT case_key, jsonb_build_object('complainantAdvocate', doc->'complainantAdvocate',
                                                'respondentAdvocate', doc->'respondentAdvocate') AS doc
            FROM case_documents WHERE name = 'status.json' AND case_key = ANY(%s)""", (list(case_keys),))
        return {r["case_key"]: r["doc"] for r in cur.fetchall()}


def save_case_document_version(case_key, name, stamp, doc):
    """Keep an earlier copy of a document that has changed (stamp: when it was replaced)."""
    with _db() as cur:
        cur.execute("INSERT INTO case_document_versions (case_key, name, stamp, doc) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (case_key, name, stamp) DO UPDATE SET doc = EXCLUDED.doc", (case_key, name, stamp, _json(doc)))


def case_documents_version(*names):
    """Something that changes whenever one of these documents (default: status and history) is added or
    changed, so a caller can tell when to rebuild what it derived from them."""
    with _db() as cur:
        cur.execute("SELECT count(*) AS n, max(saved_at)::text AS latest FROM case_documents WHERE name = ANY(%s)",
                    (list(names) or ["status.json", "history.json"],))
        row = cur.fetchone()
        return (row["n"], row["latest"])


# ---------------------------------------------------------------------------------------------
# Order files: each distinct file is kept once in ORDER_STORE_DIR, named by its SHA-256, and case_orders
# says which file each order of a case is. Courts give connected cases one order, so many orders share a file.

class OrderFile(NamedTuple):
    name: str   # the order's name within its case: <date>_<kind>[_vN].pdf|html
    path: Path  # the stored file, which other orders may share: never change it in place
    size: int


def stored_order_path(sha256, name):
    """Where the file with this content is, or would be (`name` gives the extension)."""
    return ORDER_STORE_DIR / sha256[:2] / f"{sha256}{Path(name).suffix}"


def _order_file(row):
    return OrderFile(row["name"], stored_order_path(row["sha256"], row["name"]), row["size"])


def order_files(case_key):
    """The saved orders of a case (OrderFile), sorted by name."""
    with _db() as cur:
        cur.execute("SELECT name, sha256, size FROM case_orders WHERE case_key = %s", (case_key,))
        return sorted(_order_file(r) for r in cur.fetchall())


def find_order_file(case_key, name):
    """One saved order of a case (OrderFile), or None."""
    with _db() as cur:
        cur.execute("SELECT name, sha256, size FROM case_orders WHERE case_key = %s AND name = %s", (case_key, name))
        row = cur.fetchone()
        return _order_file(row) if row else None


def load_judgement_orders():
    """{case key: [OrderFile]} of every case's judgement PDFs (all dates and versions)."""
    out = {}
    with _db() as cur:
        cur.execute(r"SELECT case_key, name, sha256, size FROM case_orders WHERE name ~ '_judgement(_v[0-9]+)?[.]pdf$'")
        for r in cur.fetchall():
            out.setdefault(r["case_key"], []).append(_order_file(r))
    return out


def order_exists(case_key, name):
    """Is this order saved, with its file there?"""
    order = find_order_file(case_key, name)
    return order is not None and order.path.is_file()


def read_order_file(case_key, name):
    """The bytes of a saved order, or None."""
    order = find_order_file(case_key, name)
    if order is None:
        return None
    try:
        return order.path.read_bytes()
    except FileNotFoundError:
        return None


def save_order_file(case_key, name, content):
    """Save the bytes of an order; returns the path of the stored file. The file is written (atomically) only if no
    order saved before has the same content, and before the reference to it, so a reference never lacks its file."""
    sha256 = hashlib.sha256(content).hexdigest()
    dest = stored_order_path(sha256, name)
    if not (dest.is_file() and dest.stat().st_size == len(content)):
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
        tmp.write_bytes(content)
        os.replace(tmp, dest)
    with _db() as cur:
        cur.execute("INSERT INTO case_orders (case_key, name, sha256, size) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (case_key, name) DO UPDATE SET sha256 = EXCLUDED.sha256, size = EXCLUDED.size, saved_at = now()",
                    (case_key, name, sha256, len(content)))
    return dest


def load_order_refs():
    """{(case key, order name): (SHA-256, size)} of every saved order."""
    with _db() as cur:
        cur.execute("SELECT case_key, name, sha256, size FROM case_orders")
        return {(r["case_key"], r["name"]): (r["sha256"], r["size"]) for r in cur.fetchall()}


def add_order_refs(rows):
    """Record references (case key, order name, SHA-256, size) to files already in the store; an order that has a
    reference keeps it."""
    with _db() as cur:
        psycopg2.extras.execute_values(
            cur, "INSERT INTO case_orders (case_key, name, sha256, size) VALUES %s ON CONFLICT (case_key, name) DO NOTHING",
            rows, page_size=1000)


# ---------------------------------------------------------------------------------------------
# The analysis: one court's cases, parties, advocates and hearings, rebuilt as a whole

ANALYSIS_TABLES = {  # table -> columns after `court`, in the order of the rows given to replace_analysis
    "status": ["case_key", "enquiry_id", "status", "status_since", "label", "label_reason"],
    "parties": ["enquiry_id", "party", "party_type", "role", "party_as_of", "case_key"],
    "advocates": ["enquiry_id", "case_key", "role", "advocate", "source"],
    "timeline": ["enquiry_id", "hearing_date", "hearing_time", "time_source", "list", "held", "purpose_tags", "purpose_text",
                 "outcome_text", "next_date", "postponed_from", "next_stage_tags", "cross_examination_of", "raw_text", "case_key"],
}


def replace_analysis(court, enquiries, state="Karnataka", **rows):
    """Replace everything stored for `court`'s analysis (a court of `state`). `enquiries` is {enquiry id: dict}; `rows` has a list of
    row lists per table of ANALYSIS_TABLES (status, parties, advocates, timeline; a table may be left out, which
    empties it), each row in the column order given there. Values are stored as text; None becomes ''."""
    with _db() as cur:
        for table in ANALYSIS_TABLES:
            cur.execute(f"DELETE FROM analysis_{table} WHERE court = %s", (court,))
        cur.execute("DELETE FROM analysis_enquiries WHERE court = %s", (court,))
        for table, columns in ANALYSIS_TABLES.items():
            data = [(court, *("" if v is None else str(v) for v in row)) for row in rows.get(table, [])]
            if data:
                psycopg2.extras.execute_values(
                    cur, f"INSERT INTO analysis_{table} (court, {', '.join(columns)}) VALUES %s", data, page_size=1000)
        if enquiries:
            psycopg2.extras.execute_values(
                cur, "INSERT INTO analysis_enquiries (court, enquiry_id, doc) VALUES %s",
                [(court, eid, _json(doc)) for eid, doc in enquiries.items()], page_size=200)
        cur.execute("INSERT INTO analysis_runs (court, state) VALUES (%s, %s) "
                    "ON CONFLICT (court) DO UPDATE SET run_at = now(), state = EXCLUDED.state", (court, state))


CASE_METRIC_COLUMNS = ["court", "case_key", "enquiry_id", "year", "kind", "outcome", "provider_side", "label", "hospitals", "n_hearings",
                       "n_held", "first_hearing", "last_hearing", "n_gaps", "median_gap_days", "max_gap_days",
                       "time_asked_patient", "time_asked_hospital", "absent_patient", "absent_hospital", "filing_date", "days_to_close", "gaps"]
HEARING_METRIC_COLUMNS = ["court", "case_key", "hearing_date", "held", "gap_days", "time_asked_by", "absent_side",
                          "provider_side", "from_history"]


def replace_metrics(case_rows, hearing_rows, courts=()):
    """Replace case_metrics and hearing_metrics (metrics.py) with these rows, in the column orders above, and record
    today as the day these courts' rows were computed (metrics_meta)."""
    with _db() as cur:
        cur.execute("DELETE FROM case_metrics")
        cur.execute("DELETE FROM hearing_metrics")
        cur.execute("DELETE FROM metrics_meta")
        for table, columns, rows in (("case_metrics", CASE_METRIC_COLUMNS, case_rows),
                                     ("hearing_metrics", HEARING_METRIC_COLUMNS, hearing_rows)):
            if rows:
                psycopg2.extras.execute_values(cur, f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s", rows, page_size=2000)
        if courts:
            psycopg2.extras.execute_values(cur, "INSERT INTO metrics_meta (court, computed_on) VALUES %s", [(c,) for c in courts], template="(%s, CURRENT_DATE)")


MAX_METRICS_AGE_DAYS = 3  # stored metrics older than this are not used (a crawl has failed for days); up to then they are at most as stale as the last crawl


def stored_case_metrics(court):
    """{case_key: row} of what metrics.py stored for this court (outcome, the gaps between hearings, the counts of hearings
    where a side asked for time or was absent), if it was computed within MAX_METRICS_AGE_DAYS; else None, and the viewer
    works the figures out itself (slower, the same answer). A case changed since is up to a crawl out of date; a case the
    rows lack is simply worked out."""
    with _db() as cur:
        cur.execute("SELECT (CURRENT_DATE - computed_on) AS age FROM metrics_meta WHERE court = %s", (court,))
        meta = cur.fetchone()
        if not meta or meta["age"] > MAX_METRICS_AGE_DAYS:
            return None
        cur.execute("SELECT case_key, outcome, gaps, time_asked_patient, time_asked_hospital, absent_patient, absent_hospital "
                    "FROM case_metrics WHERE court = %s AND gaps IS NOT NULL", (court,))
        return {r["case_key"]: r for r in cur.fetchall()}


def replace_dashboard_stats(state, rows):
    """Replace a state's rows of dashboard_stats_by_state (dashboard_stats.py) with rows of (sel, non_medical, year, doc), in one transaction."""
    with _db() as cur:
        cur.execute("DELETE FROM dashboard_stats_by_state WHERE state = %s", (state,))
        if rows:
            psycopg2.extras.execute_values(cur, "INSERT INTO dashboard_stats_by_state (state, sel, non_medical, year, doc) VALUES %s",
                                           [(state, sel, nm, year, _json(doc)) for sel, nm, year, doc in rows])


def load_dashboard_stats(state, sel, non_medical, year):
    """The stored panel numbers for this state and selection (year 0 = all years) as {"doc", "computed_at"}, or None if there are none."""
    with _db() as cur:
        cur.execute("SELECT doc, computed_at FROM dashboard_stats_by_state WHERE state = %s AND sel = %s AND non_medical = %s AND year = %s",
                    (state, sel, non_medical, year))
        r = cur.fetchone()
        return {"doc": r["doc"], "computed_at": r["computed_at"]} if r else None


def replace_analytics_pages(state, rows):
    """Replace a state's rows of analytics_pages with rows of (page, period, non_medical, doc), in one transaction."""
    with _db() as cur:
        cur.execute("DELETE FROM analytics_pages WHERE state = %s", (state,))
        if rows:
            psycopg2.extras.execute_values(cur, "INSERT INTO analytics_pages (state, page, period, non_medical, doc) VALUES %s",
                                           [(state, page, period, nm, _json(doc)) for page, period, nm, doc in rows], page_size=20)


def load_analytics_page(state, page, period, non_medical):
    """The stored table of an analytics page, or None if there is none."""
    with _db() as cur:
        cur.execute("SELECT doc FROM analytics_pages WHERE state = %s AND page = %s AND period = %s AND non_medical = %s",
                    (state, page, period, non_medical))
        r = cur.fetchone()
        return r["doc"] if r else None


# ---------------------------------------------------------------------------------------------
# The tracker's pages: a state's cases and hearings ready to show (see viewer.view_rows, which makes the rows)

def replace_views(state, format, cases, hearings, links, names, terms):
    """Replace everything stored for a state's pages, in one transaction. `cases`: (case_key, ord, court, year, outcome,
    label, enquiry_id, number_key, haystack, id_tokens, doc); `hearings`: (case_key, n, hearing_date, doc); `links`:
    (kind, slug, case_key); `names`: (sel, kind, slug, name); `terms`: (court, label, term)."""
    tables = [("view_cases", "case_key, ord, court, year, outcome, label, enquiry_id, number_key, haystack, id_tokens, doc",
               ((*r[:-1], _json(r[-1])) for r in cases)),
              ("view_hearings", "case_key, n, hearing_date, doc", ((*r[:-1], _json(r[-1])) for r in hearings)),
              ("view_links", "kind, slug, case_key", links),
              ("view_names", "sel, kind, slug, name", names),
              ("view_terms", "court, label, term", terms)]
    with _db() as cur:
        for table, columns, rows in tables:
            cur.execute(f"DELETE FROM {table} WHERE state = %s", (state,))
            psycopg2.extras.execute_values(cur, f"INSERT INTO {table} (state, {columns}) VALUES %s",
                                           ((state, *row) for row in rows), page_size=500)
        cur.execute("INSERT INTO view_runs (state, format) VALUES (%s, %s) "
                    "ON CONFLICT (state) DO UPDATE SET format = EXCLUDED.format, built_at = now()", (state, format))


def view_formats():
    """{state: format} of the states whose pages are built."""
    with _db() as cur:
        cur.execute("SELECT state, format FROM view_runs")
        return {r["state"]: r["format"] for r in cur.fetchall()}


def _view_filter(state, courts, medical_only=False, year=None, outcome=None, words=None, tokens=None):
    """(WHERE clause on view_cases c, its parameters). A search is (every word of `words` somewhere in the names) or
    (every one of `tokens` starting a word of the case number)."""
    where, params = ["c.state = %s", "c.court = ANY(%s)"], [state, list(courts)]
    if medical_only:
        where.append("c.label <> 'not_medical'")
    if year is not None:
        where.append("c.year = %s")
        params.append(year)
    if outcome:
        where.append("c.outcome = %s")
        params.append(outcome)
    if words:
        by_name = " AND ".join(["c.haystack LIKE %s"] * len(words))
        by_number = " AND ".join(["c.id_tokens LIKE %s"] * len(tokens)) or "TRUE"
        where.append(f"(({by_name}) OR ({by_number}))")
        params += [f"%{_like(w)}%" for w in words] + [f"% {_like(t)}%" for t in tokens]
    return " AND ".join(where), params


def _like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def view_case_keys(state, courts, **filters):
    """The keys of the cases that pass the filters (see _view_filter), in list order."""
    where, params = _view_filter(state, courts, **filters)
    with _db() as cur:
        cur.execute(f"SELECT c.case_key FROM view_cases c WHERE {where} ORDER BY c.ord", params)
        return [r["case_key"] for r in cur.fetchall()]


def view_linked_keys(state, courts, kind, slug, medical_only=False):
    """The keys of the cases that name this hospital or advocate, in list order."""
    where, params = _view_filter(state, courts, medical_only=medical_only)
    with _db() as cur:
        cur.execute(f"SELECT c.case_key FROM view_links l JOIN view_cases c ON c.case_key = l.case_key "
                    f"WHERE l.state = %s AND l.kind = %s AND l.slug = %s AND {where} ORDER BY c.ord", [state, kind, slug, *params])
        return [r["case_key"] for r in cur.fetchall()]


def view_case_docs(case_keys=None, state=None):
    """The stored cases with these keys, in the order given, or all of a state's, in list order."""
    with _db() as cur:
        if case_keys is None:
            cur.execute("SELECT doc FROM view_cases WHERE state = %s ORDER BY ord", (state,))
            return [r["doc"] for r in cur.fetchall()]
        cur.execute("SELECT case_key, doc FROM view_cases WHERE case_key = ANY(%s)", (list(case_keys),))
        found = {r["case_key"]: r["doc"] for r in cur.fetchall()}
        return [found[k] for k in case_keys if k in found]


def view_years(state, courts):
    """The years cases of these courts were filed in."""
    with _db() as cur:
        cur.execute("SELECT DISTINCT year FROM view_cases WHERE state = %s AND court = ANY(%s)", (state, list(courts)))
        return [r["year"] for r in cur.fetchall()]


def view_case_hearings(case_key):
    with _db() as cur:
        cur.execute("SELECT doc FROM view_hearings WHERE case_key = %s ORDER BY n", (case_key,))
        return [r["doc"] for r in cur.fetchall()]


def view_hearing_years(state, courts):
    with _db() as cur:
        cur.execute("SELECT DISTINCT left(h.hearing_date, 4) AS year FROM view_hearings h JOIN view_cases c ON c.case_key = h.case_key "
                    "WHERE h.state = %s AND c.court = ANY(%s)", (state, list(courts)))
        return [r["year"] for r in cur.fetchall()]


def view_hearing_dates(state, courts, year=None, **filters):
    """[(date, entries on it)] for the hearings of the cases that pass the filters, newest date first, of one year if given."""
    where, params = _view_filter(state, courts, **filters)
    if year:
        where += " AND h.hearing_date LIKE %s"
        params.append(f"{_like(year)}%")
    with _db() as cur:
        cur.execute(f'SELECT h.hearing_date AS date, count(*) AS n FROM view_hearings h JOIN view_cases c ON c.case_key = h.case_key '
                    f'WHERE h.state = %s AND {where} GROUP BY h.hearing_date ORDER BY h.hearing_date COLLATE "C" DESC', [state, *params])
        return [(r["date"], r["n"]) for r in cur.fetchall()]


def view_hearings_on(state, courts, dates, **filters):
    """[(date, case doc, hearing doc)] on these dates, for the cases that pass the filters: cases in list order, and a
    case's hearings in its own order."""
    where, params = _view_filter(state, courts, **filters)
    with _db() as cur:
        cur.execute(f"SELECT h.hearing_date AS date, c.doc AS case_doc, h.doc AS hearing_doc FROM view_hearings h "
                    f"JOIN view_cases c ON c.case_key = h.case_key WHERE h.state = %s AND h.hearing_date = ANY(%s) AND {where} "
                    f"ORDER BY c.ord, h.n", [state, list(dates), *params])
        return [(r["date"], r["case_doc"], r["hearing_doc"]) for r in cur.fetchall()]


def view_names(state, sel, kind):
    """{slug: name shown} for the hospitals or advocates under a court selection."""
    with _db() as cur:
        cur.execute("SELECT slug, name FROM view_names WHERE state = %s AND sel = %s AND kind = %s", (state, sel, kind))
        return {r["slug"]: r["name"] for r in cur.fetchall()}


def view_name(state, sel, kind, slug):
    with _db() as cur:
        cur.execute("SELECT name FROM view_names WHERE state = %s AND sel = %s AND kind = %s AND slug = %s", (state, sel, kind, slug))
        row = cur.fetchone()
        return row["name"] if row else None


def view_link_counts(state, kind, medical_only=False):
    """{slug: number of cases} over every court of a state; medical_only leaves out those labelled not_medical."""
    join = " JOIN view_cases c ON c.case_key = l.case_key AND c.label <> 'not_medical'" if medical_only else ""
    with _db() as cur:
        cur.execute(f"SELECT l.slug, count(*) AS n FROM view_links l{join} WHERE l.state = %s AND l.kind = %s GROUP BY l.slug", (state, kind))
        return {r["slug"]: r["n"] for r in cur.fetchall()}


def view_suggestions(state, sel, courts, q, medical_only, limit):
    """Case numbers and party, hospital and advocate names of the listed cases containing `q` (lower case): those that
    start with it first, then the shortest."""
    shown = " AND c.label <> 'not_medical'" if medical_only else ""
    with _db() as cur:
        cur.execute(f"""
            SELECT term FROM (
                SELECT c.term FROM view_terms c WHERE c.state = %(state)s AND c.court = ANY(%(courts)s){shown}
                UNION
                SELECT n.name FROM view_links l JOIN view_cases c ON c.case_key = l.case_key
                  JOIN view_names n ON n.state = l.state AND n.sel = %(sel)s AND n.kind = l.kind AND n.slug = l.slug
                  WHERE l.state = %(state)s AND c.court = ANY(%(courts)s){shown}
            ) t WHERE lower(term) LIKE %(any)s
            ORDER BY (lower(term) NOT LIKE %(start)s), char_length(term), term COLLATE "C" LIMIT %(limit)s""",
                    {"state": state, "sel": sel, "courts": list(courts), "any": f"%{_like(q)}%", "start": f"{_like(q)}%", "limit": limit})
        return [r["term"] for r in cur.fetchall()]


def view_cases_by_number(key, states, limit=None, exact=False):
    """The listed (not not_medical) cases of these states whose number is `key` (`exact`), or contains it. Exact matches
    come in the order of `states`, then of each state's list; others with those that start with `key` first, then the
    shortest numbers."""
    match, value = ("=", key) if exact else ("LIKE", f"%{_like(key)}%")
    order, ordering = "array_position(%s, state), ord", [list(states)]
    if not exact:
        order, ordering = f"(number_key NOT LIKE %s), char_length(enquiry_id), {order}", [f"{_like(key)}%", *ordering]
    with _db() as cur:
        cur.execute(f"SELECT doc FROM view_cases WHERE number_key {match} %s AND label <> 'not_medical' AND state = ANY(%s) "
                    f"ORDER BY {order}" + (" LIMIT %s" if limit else ""),
                    [value, list(states), *ordering, *([limit] if limit else [])])
        return [r["doc"] for r in cur.fetchall()]


def load_judgements():
    """{case_key: {"file", "size", "class", "reason", "version"}}: the stored classification of each case's judgement PDF (judgements.py)."""
    with _db() as cur:
        cur.execute('SELECT case_key, file, size, class, reason, version FROM case_judgements')
        return {r["case_key"]: dict(r) for r in cur.fetchall()}


def save_judgement(case_key, file, size, reason, version, **kw):
    """Store (or replace) a case's judgement classification; the class is passed as class=... ."""
    with _db() as cur:
        cur.execute("INSERT INTO case_judgements (case_key, file, size, class, reason, version) VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (case_key) DO UPDATE SET file = EXCLUDED.file, size = EXCLUDED.size, class = EXCLUDED.class, "
                    "reason = EXCLUDED.reason, version = EXCLUDED.version, classified_at = now()",
                    (case_key, file, size, kw["class"], reason or "", version))


def delete_judgements_except(case_keys):
    """Drop the classifications of cases not in `case_keys` (their judgement PDF is gone); returns how many."""
    with _db() as cur:
        cur.execute("DELETE FROM case_judgements WHERE NOT (case_key = ANY(%s))", (list(case_keys),))
        return cur.rowcount


def analysis_courts(state=None):
    """The analysed courts' ids, of one state or of all of them."""
    with _db() as cur:
        if state:
            cur.execute("SELECT court FROM analysis_runs WHERE state = %s ORDER BY court", (state,))
        else:
            cur.execute("SELECT court FROM analysis_runs ORDER BY court")
        return [r["court"] for r in cur.fetchall()]


def analysis_states():
    """The states that have analysed courts, Karnataka first and the rest alphabetically."""
    with _db() as cur:
        cur.execute("SELECT state FROM analysis_runs GROUP BY state ORDER BY (state <> 'Karnataka'), state")
        return [r["state"] for r in cur.fetchall()]


def state_of_court(court):
    with _db() as cur:
        cur.execute("SELECT state FROM analysis_runs WHERE court = %s", (court,))
        row = cur.fetchone()
        return row["state"] if row else None


def state_of_case(case_key):
    """The state of the analysed case with this key, or None."""
    with _db() as cur:
        cur.execute("SELECT r.state FROM analysis_status s JOIN analysis_runs r ON r.court = s.court WHERE s.case_key = %s LIMIT 1", (case_key,))
        row = cur.fetchone()
        return row["state"] if row else None


def analysis_stamp(courts):
    """Something that changes whenever any of these courts' analysis is replaced, for caches: it looks at those courts
    only, so another state's analysis changes nothing."""
    with _db() as cur:
        cur.execute("SELECT string_agg(court || '=' || run_at::text, ',' ORDER BY court) AS runs FROM analysis_runs WHERE court = ANY(%s)", (list(courts),))
        return cur.fetchone()["runs"]


def count_label(courts, label):
    """How many cases of these analysed courts carry this label (status rows)."""
    with _db() as cur:
        cur.execute("SELECT count(*) AS n FROM analysis_status WHERE court = ANY(%s) AND label = %s", (list(courts), label))
        return cur.fetchone()["n"]


def has_analysis(court):
    """Has this court been analysed (so that load_analysis has something to give)?"""
    with _db() as cur:
        cur.execute("SELECT 1 FROM analysis_runs WHERE court = %s", (court,))
        return cur.fetchone() is not None


def analysis_version(court):
    """Something that changes whenever the court's analysis is replaced, for caches."""
    with _db() as cur:
        cur.execute("SELECT run_at::text AS run_at FROM analysis_runs WHERE court = %s", (court,))
        row = cur.fetchone()
        return row["run_at"] if row else None


def load_analysis(court, texts=True):
    """{"status": [row dict], "parties": [...], "advocates": [...], "timeline": [...]} for a court, rows in the
    order they were written, keyed by the column names of ANALYSIS_TABLES (all values strings). With texts=False
    the timeline rows come without raw_text, the longest column (see timeline_texts)."""
    out = {}
    with _db() as cur:
        for table, columns in ANALYSIS_TABLES.items():
            columns = [c for c in columns if texts or c != "raw_text"]
            cur.execute(f"SELECT {', '.join(columns)} FROM analysis_{table} WHERE court = %s ORDER BY id", (court,))
            out[table] = list(cur.fetchall())
    return out


def timeline_texts(case_keys):
    """The timeline rows of these cases with their raw_text, in the order written: [{"court", "case_key", "hearing_date",
    "hearing_time", "list", "purpose_text", "outcome_text", "raw_text"}]."""
    with _db() as cur:
        cur.execute("SELECT court, case_key, hearing_date, hearing_time, list, purpose_text, outcome_text, raw_text "
                    "FROM analysis_timeline WHERE case_key = ANY(%s) ORDER BY id", (list(case_keys),))
        return list(cur.fetchall())


def load_listed_commissions(court):
    """{enquiry id: the commission whose cause list named the case} for a consumer court's analysis."""
    with _db() as cur:
        cur.execute("SELECT enquiry_id, doc->>'commission' AS commission FROM analysis_enquiries WHERE court = %s", (court,))
        return {r["enquiry_id"]: r["commission"] or "" for r in cur.fetchall()}


def load_enquiries(court):
    """{enquiry id: dict}: everything the analysis found about each enquiry/case."""
    with _db() as cur:
        cur.execute("SELECT enquiry_id, doc FROM analysis_enquiries WHERE court = %s ORDER BY enquiry_id", (court,))
        return {r["enquiry_id"]: r["doc"] for r in cur.fetchall()}


# ---------------------------------------------------------------------------------------------

TABLES = ["courts", "crawl_state", "case_links", "cause_lists", "case_documents", "case_document_versions", "case_orders",
          "analysis_status", "analysis_parties", "analysis_advocates", "analysis_timeline", "analysis_enquiries",
          "case_metrics", "hearing_metrics", "case_judgements"]


def status():
    with _db() as cur:
        for t in TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {t}")
            print(f"{t:26} {cur.fetchone()['n']:>9,} rows")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "init":
        init_db()
        print("tables ready")
    elif command == "status":
        status()
    else:
        sys.exit("usage: python progress_storage.py [init|status]")
