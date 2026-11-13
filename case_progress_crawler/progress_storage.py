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
  analysis_*        analysis*.py:        status, parties, advocates, timeline, enquiries replace_analysis / load_analysis

On disk (files, not data): the order PDFs and HTML pages in case_details/consumer/<case key>/orders/ (order_path,
order_files, save_order_file ...), and the KMC's PDFs in pdfs/, which the court crawlers and the viewer handle as
paths. Every read here is of the whole current state; every write is one transaction.
"""

import json
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

BASE_DIR = Path(__file__).parent
SCHEMA_FILE = BASE_DIR / "db" / "schema.sql"
CASE_DETAILS_DIR = BASE_DIR / "case_details" / "consumer"  # <case key>/orders/<date>_<kind>[_vN].pdf|html
DEFAULT_DSN = "postgresql:///case_progress"

_pool = None
_pool_lock = threading.Lock()  # the viewer warms its cache in a thread while requests arrive: make the pool once


# ---------------------------------------------------------------------------------------------
# Connection

@contextmanager
def _db():
    """A cursor in a transaction: committed when the block ends, rolled back if it raises."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(1, 8, os.environ.get("CASE_PROGRESS_DATABASE_URL", DEFAULT_DSN))
    pool = _pool
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


def load_case_documents(name):
    """{case key: document} of one kind of document, for every case that has it."""
    with _db() as cur:
        cur.execute("SELECT case_key, doc FROM case_documents WHERE name = %s", (name,))
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


def order_dir(case_key):
    return CASE_DETAILS_DIR / case_key / "orders"


def order_path(case_key, name):
    """Where an order file (<date>_<kind>[_vN].pdf|html) is, or would be."""
    return order_dir(case_key) / name


def order_files(case_key):
    """The saved order files of a case, sorted by name."""
    folder = order_dir(case_key)
    return sorted(folder.glob("*")) if folder.is_dir() else []


def order_exists(case_key, name):
    return order_path(case_key, name).exists()


def read_order_file(case_key, name):
    """The bytes of a saved order, or None."""
    try:
        return order_path(case_key, name).read_bytes()
    except FileNotFoundError:
        return None


def save_order_file(case_key, name, content):
    """Save the bytes of an order (written atomically); returns its path."""
    dest = order_path(case_key, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(content)
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------------------------------------
# The analysis: one court's cases, parties, advocates and hearings, rebuilt as a whole

ANALYSIS_TABLES = {  # table -> columns after `court`, in the order of the rows given to replace_analysis
    "status": ["case_key", "enquiry_id", "status", "status_since", "label", "label_reason"],
    "parties": ["enquiry_id", "party", "party_type", "role", "party_as_of", "case_key"],
    "advocates": ["enquiry_id", "case_key", "role", "advocate", "source"],
    "timeline": ["enquiry_id", "hearing_date", "hearing_time", "time_source", "list", "held", "purpose_tags", "purpose_text",
                 "outcome_text", "next_date", "postponed_from", "next_stage_tags", "cross_examination_of", "raw_text", "case_key"],
}


def replace_analysis(court, enquiries, **rows):
    """Replace everything stored for `court`'s analysis. `enquiries` is {enquiry id: dict}; `rows` has a list of
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
        cur.execute("INSERT INTO analysis_runs (court) VALUES (%s) ON CONFLICT (court) DO UPDATE SET run_at = now()", (court,))


CASE_METRIC_COLUMNS = ["court", "case_key", "enquiry_id", "year", "kind", "outcome", "provider_side", "label", "hospitals", "n_hearings",
                       "n_held", "first_hearing", "last_hearing", "n_gaps", "median_gap_days", "max_gap_days",
                       "time_asked_patient", "time_asked_hospital", "absent_patient", "absent_hospital"]
HEARING_METRIC_COLUMNS = ["court", "case_key", "hearing_date", "held", "gap_days", "time_asked_by", "absent_side",
                          "provider_side", "from_history"]


def replace_metrics(case_rows, hearing_rows):
    """Replace case_metrics and hearing_metrics (metrics.py) with these rows, in the column orders above."""
    with _db() as cur:
        cur.execute("DELETE FROM case_metrics")
        cur.execute("DELETE FROM hearing_metrics")
        for table, columns, rows in (("case_metrics", CASE_METRIC_COLUMNS, case_rows),
                                     ("hearing_metrics", HEARING_METRIC_COLUMNS, hearing_rows)):
            if rows:
                psycopg2.extras.execute_values(cur, f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s", rows, page_size=2000)


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


def load_analysis(court):
    """{"status": [row dict], "parties": [...], "advocates": [...], "timeline": [...]} for a court, rows in the
    order they were written, keyed by the column names of ANALYSIS_TABLES (all values strings)."""
    out = {}
    with _db() as cur:
        for table, columns in ANALYSIS_TABLES.items():
            cur.execute(f"SELECT {', '.join(columns)} FROM analysis_{table} WHERE court = %s ORDER BY id", (court,))
            out[table] = list(cur.fetchall())
    return out


def load_enquiries(court):
    """{enquiry id: dict}: everything the analysis found about each enquiry/case."""
    with _db() as cur:
        cur.execute("SELECT enquiry_id, doc FROM analysis_enquiries WHERE court = %s ORDER BY enquiry_id", (court,))
        return {r["enquiry_id"]: r["doc"] for r in cur.fetchall()}


# ---------------------------------------------------------------------------------------------

TABLES = ["courts", "crawl_state", "case_links", "cause_lists", "case_documents", "case_document_versions",
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
