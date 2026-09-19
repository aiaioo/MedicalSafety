"""The single persistence boundary for case_manager.

Every other module (app.py above all) talks to *this* module only -- never
to psycopg2, SQL, or a filesystem path directly. In return, this module
promises to speak in the same plain dicts/lists/strings the rest of the app
already works with (the exact shapes storage/*.json used to hold), never in
rows, cursors, or paths. That's the whole point: swapping what's behind this
module (a different database, a different blob store) later means editing
this one file, not any of app.py's routes.

Two things live here, both "persistence":
  - structured data, in PostgreSQL (see db/schema.sql for the tables) --
    documents' metadata, annotations, snippet metadata, reports, cases,
    causes, allegations.
  - the binary bytes for uploaded documents and cropped snippets, via
    db/storage_backend.py's swappable backend (local disk today) -- this
    module is the only thing that ever touches that backend, keyed by a
    storage key it derives from a row's own id columns (never a stored
    path -- see storage_backend.py's module docstring).

The docx->pdf render cache (rendering a Word doc as a PDF for viewing) is a
derived, disposable artifact, not data -- it lives in a plain local
directory here and is never modeled in PostgreSQL.

Every function below either returns plain JSON-shaped Python values (or
None / a bool for a not-found / existence check) or raises StorageError for
a genuine persistence-layer problem (no database configured, no PDF
conversion tool installed). Callers translate StorageError into whatever
HTTP error shape they want; this module doesn't know about Flask.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from converters import ConversionError, convert_to_pdf
from db.storage_backend import document_storage_key, get_storage_backend, snippet_storage_key


class StorageError(Exception):
    """A persistence-layer problem, as opposed to a caller passing a bad id
    (those are reported via a None/False return, not an exception)."""


# ---------------------------------------------------------------------------
# Connection handling. One small pool for the whole process; every public
# function below opens a cursor, does its work as a single transaction, and
# gives the connection back -- callers never see a connection or cursor.
# ---------------------------------------------------------------------------

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ.get("DATABASE_URL", "postgresql:///case_manager")
        try:
            _pool = ThreadedConnectionPool(1, 10, dsn)
        except psycopg2.OperationalError as exc:
            raise StorageError(f"Could not connect to the database ({dsn}): {exc}") from exc
    return _pool


class _Cursor:
    """`with _cursor() as cur:` runs one transaction: commits on a clean
    exit, rolls back on any exception, always returns the connection to the
    pool."""

    def __enter__(self):
        self._pool = _get_pool()
        self._conn = self._pool.getconn()
        self._conn.autocommit = False
        self._cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return self._cur

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._cur.close()
            self._pool.putconn(self._conn)
        return False


def _cursor() -> _Cursor:
    return _Cursor()


def _iso(value) -> str:
    """A stored TIMESTAMPTZ comes back from psycopg2 as a datetime -- render
    it the same way app.py's own datetime.now(timezone.utc).isoformat()
    calls always have, so callers never see a shape difference from the old
    JSON files."""
    if value is None:
        return ""
    return value.isoformat()


def _doc_family(doc_type: str | None) -> str:
    """documents.doc_type distinguishes .doc/.docx on disk; every place that
    used to read this back out of JSON only ever saw the collapsed "pdf" /
    "docx" family (see app.py's own normalize_type), so that's what's
    reflected outward here too."""
    return "docx" if doc_type in ("doc", "docx") else "pdf"


def _exists(table: str, row_id: str) -> bool:
    with _cursor() as cur:
        cur.execute(f"SELECT 1 FROM {table} WHERE id = %s", (row_id,))  # noqa: S608 (table is always a literal below)
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Documents (metadata) + their bytes + the docx->pdf render cache
# ---------------------------------------------------------------------------

_CONVERSION_CACHE_DIR = Path(__file__).resolve().parent / "storage" / "cache"


def _conversion_cache_path(document_id: str) -> Path:
    return _CONVERSION_CACHE_DIR / f"{document_id}.pdf"


def list_documents() -> list[dict]:
    """Every uploaded source document, id-sorted -- {"id", "type", "title"}."""
    with _cursor() as cur:
        cur.execute("SELECT id, doc_type, title FROM documents ORDER BY id")
        rows = cur.fetchall()
    return [{"id": r["id"], "type": r["doc_type"], "title": r["title"]} for r in rows]


def document_exists(document_id: str) -> bool:
    return _exists("documents", document_id)


def get_document_type(document_id: str) -> str | None:
    with _cursor() as cur:
        cur.execute("SELECT doc_type FROM documents WHERE id = %s", (document_id,))
        row = cur.fetchone()
        return row["doc_type"] if row else None


def get_document_title(document_id: str) -> str:
    with _cursor() as cur:
        cur.execute("SELECT title FROM documents WHERE id = %s", (document_id,))
        row = cur.fetchone()
    return row["title"] if row and row["title"] else document_id


def set_document_title(document_id: str, title: str) -> None:
    with _cursor() as cur:
        cur.execute("UPDATE documents SET title = %s WHERE id = %s", (title, document_id))


def create_document(document_id: str, doc_type: str, data: bytes) -> None:
    """Registers a freshly uploaded document: writes its bytes via the
    storage backend, then its row (title defaults to its own id, matching
    the old doc_meta fallback)."""
    get_storage_backend().write_bytes(document_storage_key(document_id, doc_type), data)
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO documents (id, doc_type, title) VALUES (%s, %s, %s)",
            (document_id, doc_type, document_id),
        )


def document_has_annotations(document_id: str) -> bool:
    with _cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM document_annotations WHERE document_id = %s AND jsonb_array_length(shapes) > 0)",
            (document_id,),
        )
        return cur.fetchone()["exists"]


def document_has_snippets(document_id: str) -> bool:
    with _cursor() as cur:
        cur.execute("SELECT EXISTS(SELECT 1 FROM snippets WHERE document_id = %s)", (document_id,))
        return cur.fetchone()["exists"]


def delete_document(document_id: str) -> None:
    """Removes a document's row (cascading to its annotations, snippet
    rows, and hearing links -- see schema.sql) plus every blob it owns: its
    own bytes, every snippet's PNG, and any docx->pdf render cache entry."""
    doc_type = get_document_type(document_id)
    if doc_type is None:
        return
    backend = get_storage_backend()
    for snippet in list_snippets(document_id):
        backend.delete(snippet_storage_key(document_id, doc_type, snippet["filename"]))
    backend.delete(document_storage_key(document_id, doc_type))
    _conversion_cache_path(document_id).unlink(missing_ok=True)
    with _cursor() as cur:
        cur.execute("DELETE FROM documents WHERE id = %s", (document_id,))


def get_document_pdf_bytes(document_id: str) -> bytes:
    """The document's content as PDF bytes -- straight from storage for a
    native PDF, or converted-and-cached (LibreOffice, run at most once per
    document since uploads are never replaced in place) for a Word doc."""
    doc_type = get_document_type(document_id)
    if doc_type is None:
        raise StorageError(f"No document with id {document_id!r}")

    backend = get_storage_backend()
    if doc_type == "pdf":
        return backend.read_bytes(document_storage_key(document_id, "pdf"))

    cache_path = _conversion_cache_path(document_id)
    if not cache_path.exists():
        source_bytes = backend.read_bytes(document_storage_key(document_id, doc_type))
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / f"{document_id}.{doc_type}"
            source_path.write_bytes(source_bytes)
            try:
                converted = convert_to_pdf(source_path, _CONVERSION_CACHE_DIR)
            except ConversionError as exc:
                raise StorageError(str(exc)) from exc
            if converted != cache_path:
                converted.replace(cache_path)
    return cache_path.read_bytes()


# ---------------------------------------------------------------------------
# Per-page annotations
# ---------------------------------------------------------------------------

def get_all_annotations(document_id: str) -> dict:
    """{"1": [...shapes...], "2": [...]}, matching the old per-document JSON
    file's shape exactly (only pages that have any annotations appear)."""
    with _cursor() as cur:
        cur.execute("SELECT page_number, shapes FROM document_annotations WHERE document_id = %s", (document_id,))
        rows = cur.fetchall()
    return {str(r["page_number"]): r["shapes"] for r in rows}


def get_page_annotations(document_id: str, page: int) -> list:
    with _cursor() as cur:
        cur.execute(
            "SELECT shapes FROM document_annotations WHERE document_id = %s AND page_number = %s",
            (document_id, page),
        )
        row = cur.fetchone()
    return row["shapes"] if row else []


def set_page_annotations(document_id: str, page: int, annotations: list) -> None:
    with _cursor() as cur:
        if annotations:
            cur.execute(
                """
                INSERT INTO document_annotations (document_id, page_number, shapes)
                VALUES (%s, %s, %s)
                ON CONFLICT (document_id, page_number) DO UPDATE SET shapes = EXCLUDED.shapes
                """,
                (document_id, page, psycopg2.extras.Json(annotations)),
            )
        else:
            cur.execute(
                "DELETE FROM document_annotations WHERE document_id = %s AND page_number = %s",
                (document_id, page),
            )


# ---------------------------------------------------------------------------
# Snippets (cropped page images)
# ---------------------------------------------------------------------------

def list_snippets(document_id: str, page: int | None = None) -> list[dict]:
    with _cursor() as cur:
        if page is None:
            cur.execute(
                """
                SELECT id, page_number, filename, rect_x, rect_y, rect_w, rect_h, annotated, created_at
                FROM snippets WHERE document_id = %s ORDER BY created_at
                """,
                (document_id,),
            )
        else:
            cur.execute(
                """
                SELECT id, page_number, filename, rect_x, rect_y, rect_w, rect_h, annotated, created_at
                FROM snippets WHERE document_id = %s AND page_number = %s ORDER BY created_at
                """,
                (document_id, page),
            )
        rows = cur.fetchall()
    return [
        {
            "id": r["id"],
            "page": r["page_number"],
            "filename": r["filename"],
            "rect": {"x": r["rect_x"], "y": r["rect_y"], "w": r["rect_w"], "h": r["rect_h"]},
            "annotated": r["annotated"],
            "created_at": _iso(r["created_at"]),
        }
        for r in rows
    ]


def create_snippet(document_id: str, page: int, rect: dict, annotated: bool, png_bytes: bytes) -> dict:
    doc_type = get_document_type(document_id)
    if doc_type is None:
        raise StorageError(f"No document with id {document_id!r}")

    snippet_id = uuid.uuid4().hex[:12]
    filename = f"p{page}_{snippet_id}.png"
    get_storage_backend().write_bytes(snippet_storage_key(document_id, doc_type, filename), png_bytes)

    created_at = datetime.now(timezone.utc).isoformat()
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO snippets (id, document_id, page_number, filename, rect_x, rect_y, rect_w, rect_h,
                                   annotated, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (snippet_id, document_id, page, filename, rect["x"], rect["y"], rect["w"], rect["h"],
             annotated, created_at),
        )
    return {"id": snippet_id, "page": page, "filename": filename, "rect": rect, "annotated": annotated,
            "created_at": created_at}


def delete_snippet(document_id: str, snippet_id: str) -> bool:
    with _cursor() as cur:
        cur.execute("SELECT filename FROM snippets WHERE id = %s AND document_id = %s", (snippet_id, document_id))
        row = cur.fetchone()
        if row is None:
            return False
        cur.execute("DELETE FROM snippets WHERE id = %s", (snippet_id,))

    doc_type = get_document_type(document_id)
    if doc_type is not None:
        get_storage_backend().delete(snippet_storage_key(document_id, doc_type, row["filename"]))
    return True


def read_snippet_bytes(document_id: str, filename: str) -> bytes | None:
    doc_type = get_document_type(document_id)
    if doc_type is None:
        return None
    backend = get_storage_backend()
    key = snippet_storage_key(document_id, doc_type, filename)
    if not backend.exists(key):
        return None
    return backend.read_bytes(key)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

_REPORT_COLUMNS = """
    r.id, r.name, r.doc, r.source_document_id, d.doc_type AS source_doc_type,
    r.margins, r.page_numbers, r.created_at, r.updated_at
"""


def _report_row_to_dict(row: dict) -> dict:
    source_doc = row["source_document_id"] or ""
    return {
        "id": row["id"],
        "name": row["name"],
        "doc": row["doc"],
        "source_doc": source_doc,
        "source_type": _doc_family(row["source_doc_type"]) if source_doc else "pdf",
        "margins": row["margins"],
        "pageNumbers": row["page_numbers"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def list_reports() -> list[dict]:
    """The lightweight projection the reports list view needs -- callers
    that need a report's full document tree use get_report instead, so
    listing every report never has to ship every report's (potentially
    large) content over the wire."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.name, r.source_document_id, d.doc_type AS source_doc_type, r.created_at, r.updated_at
            FROM reports r LEFT JOIN documents d ON d.id = r.source_document_id
            """
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        source_doc = r["source_document_id"] or ""
        items.append({
            "id": r["id"],
            "name": r["name"],
            "source_doc": source_doc,
            "source_type": _doc_family(r["source_doc_type"]) if source_doc else "pdf",
            "created_at": _iso(r["created_at"]),
            "updated_at": _iso(r["updated_at"]),
        })
    return items


def report_exists(report_id: str) -> bool:
    return _exists("reports", report_id)


def get_report(report_id: str) -> dict | None:
    with _cursor() as cur:
        cur.execute(
            f"SELECT {_REPORT_COLUMNS} FROM reports r LEFT JOIN documents d ON d.id = r.source_document_id WHERE r.id = %s",
            (report_id,),
        )
        row = cur.fetchone()
    return _report_row_to_dict(row) if row else None


def save_report(report_id: str, data: dict) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO reports (id, name, doc, source_document_id, margins, page_numbers, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name, doc = EXCLUDED.doc, source_document_id = EXCLUDED.source_document_id,
                margins = EXCLUDED.margins, page_numbers = EXCLUDED.page_numbers, updated_at = EXCLUDED.updated_at
            """,
            (report_id, data["name"], psycopg2.extras.Json(data["doc"]), data["source_doc"] or None,
             psycopg2.extras.Json(data["margins"]), psycopg2.extras.Json(data["pageNumbers"]),
             data["created_at"], data["updated_at"]),
        )


def delete_report(report_id: str) -> None:
    # allegation_evidence.report_id is ON DELETE SET NULL, so every evidence
    # card that linked to this report is automatically unlinked -- no manual
    # sweep over allegations needed.
    with _cursor() as cur:
        cur.execute("DELETE FROM reports WHERE id = %s", (report_id,))


# ---------------------------------------------------------------------------
# Cases (+ hearings, + each hearing's linked documents)
# ---------------------------------------------------------------------------

def _hearing_doc_dict(row: dict) -> dict:
    return {"id": row["id"], "doc_id": row["document_id"], "doc_type": _doc_family(row["doc_type"])}


def _assemble_cases(case_rows, hearing_rows, doc_link_rows) -> list[dict]:
    docs_by_hearing: dict[str, list] = {}
    for link in doc_link_rows:
        docs_by_hearing.setdefault(link["hearing_id"], []).append(link)

    hearings_by_case: dict[str, list] = {}
    for h in hearing_rows:
        links = docs_by_hearing.get(h["id"], [])
        hearings_by_case.setdefault(h["case_id"], []).append({
            "id": h["id"],
            "date": h["hearing_date"],
            "title": h["title"],
            "summary": h["summary"],
            "submitted_docs": [_hearing_doc_dict(l) for l in links if l["direction"] == "submitted"],
            "received_docs": [_hearing_doc_dict(l) for l in links if l["direction"] == "received"],
        })

    return [
        {
            "id": c["id"],
            "name": c["name"],
            "cause_id": c["cause_id"],
            "court": c["court"],
            "case_number": c["case_number"],
            "summary": c["summary"],
            "hearings": hearings_by_case.get(c["id"], []),
            "created_at": _iso(c["created_at"]),
            "updated_at": _iso(c["updated_at"]),
        }
        for c in case_rows
    ]


_HEARING_DOC_JOIN = """
    SELECT hd.hearing_id, hd.id, hd.direction, hd.document_id, d.doc_type
    FROM hearing_documents hd
    JOIN documents d ON d.id = hd.document_id
"""


def list_cases() -> list[dict]:
    with _cursor() as cur:
        cur.execute("SELECT id, name, cause_id, court, case_number, summary, created_at, updated_at FROM cases")
        case_rows = cur.fetchall()
        cur.execute("SELECT id, case_id, hearing_date, title, summary FROM hearings ORDER BY position")
        hearing_rows = cur.fetchall()
        cur.execute(_HEARING_DOC_JOIN + " ORDER BY hd.position")
        doc_link_rows = cur.fetchall()
    return _assemble_cases(case_rows, hearing_rows, doc_link_rows)


def case_exists(case_id: str) -> bool:
    return _exists("cases", case_id)


def get_case(case_id: str) -> dict | None:
    with _cursor() as cur:
        cur.execute(
            "SELECT id, name, cause_id, court, case_number, summary, created_at, updated_at FROM cases WHERE id = %s",
            (case_id,),
        )
        case_row = cur.fetchone()
        if case_row is None:
            return None
        cur.execute("SELECT id, case_id, hearing_date, title, summary FROM hearings WHERE case_id = %s ORDER BY position", (case_id,))
        hearing_rows = cur.fetchall()
        cur.execute(_HEARING_DOC_JOIN + " JOIN hearings h ON h.id = hd.hearing_id WHERE h.case_id = %s ORDER BY hd.position", (case_id,))
        doc_link_rows = cur.fetchall()
    return _assemble_cases([case_row], hearing_rows, doc_link_rows)[0]


def save_case(case_id: str, data: dict) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases (id, cause_id, name, court, case_number, summary, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                cause_id = EXCLUDED.cause_id, name = EXCLUDED.name, court = EXCLUDED.court,
                case_number = EXCLUDED.case_number, summary = EXCLUDED.summary, updated_at = EXCLUDED.updated_at
            """,
            (case_id, data["cause_id"], data["name"], data["court"], data["case_number"], data["summary"],
             data["created_at"], data["updated_at"]),
        )
        # A fresh DELETE + reinsert of every child row: exactly the "whole
        # record overwritten on every save" semantics the old JSON file had,
        # just spread across parent + child tables in one transaction.
        cur.execute("DELETE FROM hearings WHERE case_id = %s", (case_id,))  # cascades to hearing_documents
        for position, hearing in enumerate(data.get("hearings") or []):
            cur.execute(
                "INSERT INTO hearings (id, case_id, position, hearing_date, title, summary) VALUES (%s, %s, %s, %s, %s, %s)",
                (hearing["id"], case_id, position, hearing.get("date", ""), hearing.get("title", ""), hearing.get("summary", "")),
            )
            for direction, key in (("submitted", "submitted_docs"), ("received", "received_docs")):
                for doc_position, doc_item in enumerate(hearing.get(key) or []):
                    cur.execute(
                        "INSERT INTO hearing_documents (id, hearing_id, direction, document_id, position) VALUES (%s, %s, %s, %s, %s)",
                        (doc_item["id"], hearing["id"], direction, doc_item["doc_id"], doc_position),
                    )


def delete_case(case_id: str) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM cases WHERE id = %s", (case_id,))


def count_allegations_by_case() -> dict:
    with _cursor() as cur:
        cur.execute("SELECT case_id, COUNT(*) AS n FROM allegation_cases GROUP BY case_id")
        return {r["case_id"]: r["n"] for r in cur.fetchall()}


def list_case_ids_by_cause(cause_id: str) -> list[str]:
    with _cursor() as cur:
        cur.execute("SELECT id FROM cases WHERE cause_id = %s", (cause_id,))
        return [r["id"] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Causes (+ goals, each optionally linked to cases)
# ---------------------------------------------------------------------------

_GOAL_QUERY = """
    SELECT g.cause_id, g.id, g.title, g.description, g.position,
           COALESCE(array_agg(gc.case_id ORDER BY gc.position) FILTER (WHERE gc.case_id IS NOT NULL), '{{}}') AS case_ids
    FROM goals g
    LEFT JOIN goal_cases gc ON gc.goal_id = g.id
    {where}
    GROUP BY g.cause_id, g.id, g.title, g.description, g.position
    ORDER BY g.position
"""


def _goal_dict(row: dict) -> dict:
    return {"id": row["id"], "title": row["title"], "description": row["description"], "case_ids": list(row["case_ids"])}


def list_causes() -> list[dict]:
    with _cursor() as cur:
        cur.execute("SELECT id, title, description, created_at, updated_at FROM causes")
        cause_rows = cur.fetchall()
        cur.execute(_GOAL_QUERY.format(where=""))
        goal_rows = cur.fetchall()
    goals_by_cause: dict[str, list] = {}
    for g in goal_rows:
        goals_by_cause.setdefault(g["cause_id"], []).append(_goal_dict(g))
    return [
        {
            "id": c["id"], "title": c["title"], "description": c["description"],
            "goals": goals_by_cause.get(c["id"], []),
            "created_at": _iso(c["created_at"]), "updated_at": _iso(c["updated_at"]),
        }
        for c in cause_rows
    ]


def cause_exists(cause_id: str) -> bool:
    return _exists("causes", cause_id)


def get_cause(cause_id: str) -> dict | None:
    with _cursor() as cur:
        cur.execute("SELECT title, description, created_at, updated_at FROM causes WHERE id = %s", (cause_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(_GOAL_QUERY.format(where="WHERE g.cause_id = %s"), (cause_id,))
        goal_rows = cur.fetchall()
    return {
        "title": row["title"], "description": row["description"],
        "goals": [_goal_dict(g) for g in goal_rows],
        "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
    }


def save_cause(cause_id: str, data: dict) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO causes (id, title, description, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description,
                                            updated_at = EXCLUDED.updated_at
            """,
            (cause_id, data["title"], data["description"], data["created_at"], data["updated_at"]),
        )
        cur.execute("DELETE FROM goals WHERE cause_id = %s", (cause_id,))  # cascades to goal_cases
        for position, goal in enumerate(data.get("goals") or []):
            cur.execute(
                "INSERT INTO goals (id, cause_id, title, description, position) VALUES (%s, %s, %s, %s, %s)",
                (goal["id"], cause_id, goal.get("title", ""), goal.get("description", ""), position),
            )
            for case_position, case_id in enumerate(goal.get("case_ids") or []):
                cur.execute(
                    "INSERT INTO goal_cases (goal_id, case_id, position) VALUES (%s, %s, %s)",
                    (goal["id"], case_id, case_position),
                )


def delete_cause(cause_id: str) -> None:
    """Fails (a database FK error) if any case still points at this cause --
    a case's cause is mandatory, so callers must reassign those cases first
    (see list_case_ids_by_cause) rather than this module inventing a
    fallback cause on their behalf."""
    with _cursor() as cur:
        cur.execute("DELETE FROM causes WHERE id = %s", (cause_id,))


def most_recently_updated_cause_id(exclude: str | None = None) -> str | None:
    with _cursor() as cur:
        if exclude is None:
            cur.execute("SELECT id FROM causes ORDER BY updated_at DESC LIMIT 1")
        else:
            cur.execute("SELECT id FROM causes WHERE id != %s ORDER BY updated_at DESC LIMIT 1", (exclude,))
        row = cur.fetchone()
        return row["id"] if row else None


def get_last_used_cause() -> str | None:
    with _cursor() as cur:
        cur.execute("SELECT last_used_cause_id FROM app_settings WHERE id = TRUE")
        row = cur.fetchone()
        return row["last_used_cause_id"] if row else None


def set_last_used_cause(cause_id: str) -> None:
    with _cursor() as cur:
        cur.execute("UPDATE app_settings SET last_used_cause_id = %s WHERE id = TRUE", (cause_id,))


# ---------------------------------------------------------------------------
# Allegations (+ inculpatory/exculpatory evidence, + "to prove" items each
# optionally linking some of that evidence, + linked cases)
# ---------------------------------------------------------------------------

_TO_PROVE_QUERY = """
    SELECT tp.allegation_id, tp.id, tp.title, tp.summary, tp.position,
           COALESCE(array_agg(tpe.evidence_id ORDER BY tpe.position) FILTER (WHERE tpe.evidence_id IS NOT NULL), '{{}}') AS evidence_ids
    FROM allegation_to_prove tp
    LEFT JOIN allegation_to_prove_evidence tpe ON tpe.to_prove_id = tp.id
    {where}
    GROUP BY tp.allegation_id, tp.id, tp.title, tp.summary, tp.position
    ORDER BY tp.position
"""


def _evidence_dict(row: dict) -> dict:
    return {"id": row["id"], "text": row["text"], "report_id": row["report_id"] or ""}


def _to_prove_dict(row: dict) -> dict:
    return {"id": row["id"], "title": row["title"], "summary": row["summary"], "evidence_ids": list(row["evidence_ids"])}


def _assemble_allegations(allegation_rows, evidence_rows, to_prove_rows, case_rows) -> list[dict]:
    evidence_by_allegation: dict[str, list] = {}
    for r in evidence_rows:
        evidence_by_allegation.setdefault(r["allegation_id"], []).append(r)
    to_prove_by_allegation: dict[str, list] = {}
    for t in to_prove_rows:
        to_prove_by_allegation.setdefault(t["allegation_id"], []).append(t)
    cases_by_allegation: dict[str, list] = {}
    for r in case_rows:
        cases_by_allegation.setdefault(r["allegation_id"], []).append(r["case_id"])

    items = []
    for a in allegation_rows:
        evidence = evidence_by_allegation.get(a["id"], [])
        items.append({
            "id": a["id"],
            "title": a["title"],
            "description": a["description"],
            "to_prove": [_to_prove_dict(t) for t in to_prove_by_allegation.get(a["id"], [])],
            "inculpatory": [_evidence_dict(r) for r in evidence if r["kind"] == "inculpatory"],
            "exculpatory": [_evidence_dict(r) for r in evidence if r["kind"] == "exculpatory"],
            "case_ids": cases_by_allegation.get(a["id"], []),
            "created_at": _iso(a["created_at"]),
            "updated_at": _iso(a["updated_at"]),
        })
    return items


def list_allegations() -> list[dict]:
    """In display order (allegations/allegation_order.json's old job is now
    just ORDER BY order_index -- see set_allegation_order)."""
    with _cursor() as cur:
        cur.execute("SELECT id, title, description, created_at, updated_at FROM allegations ORDER BY order_index")
        allegation_rows = cur.fetchall()
        cur.execute("SELECT allegation_id, id, kind, text, report_id FROM allegation_evidence ORDER BY position")
        evidence_rows = cur.fetchall()
        cur.execute(_TO_PROVE_QUERY.format(where=""))
        to_prove_rows = cur.fetchall()
        cur.execute("SELECT allegation_id, case_id FROM allegation_cases ORDER BY position")
        case_rows = cur.fetchall()
    return _assemble_allegations(allegation_rows, evidence_rows, to_prove_rows, case_rows)


def get_allegation(allegation_id: str) -> dict | None:
    with _cursor() as cur:
        cur.execute("SELECT id, title, description, created_at, updated_at FROM allegations WHERE id = %s", (allegation_id,))
        allegation_row = cur.fetchone()
        if allegation_row is None:
            return None
        cur.execute("SELECT allegation_id, id, kind, text, report_id FROM allegation_evidence WHERE allegation_id = %s ORDER BY position", (allegation_id,))
        evidence_rows = cur.fetchall()
        cur.execute(_TO_PROVE_QUERY.format(where="WHERE tp.allegation_id = %s"), (allegation_id,))
        to_prove_rows = cur.fetchall()
        cur.execute("SELECT allegation_id, case_id FROM allegation_cases WHERE allegation_id = %s ORDER BY position", (allegation_id,))
        case_rows = cur.fetchall()
    return _assemble_allegations([allegation_row], evidence_rows, to_prove_rows, case_rows)[0]


def save_allegation(allegation_id: str, data: dict) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO allegations (id, title, description, order_index, created_at, updated_at)
            VALUES (%s, %s, %s, (SELECT COALESCE(MAX(order_index), -1) + 1 FROM allegations), %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title, description = EXCLUDED.description, updated_at = EXCLUDED.updated_at
            """,
            (allegation_id, data["title"], data["description"], data["created_at"], data["updated_at"]),
        )
        cur.execute("DELETE FROM allegation_evidence WHERE allegation_id = %s", (allegation_id,))  # cascades to allegation_to_prove_evidence
        for kind in ("inculpatory", "exculpatory"):
            for position, item in enumerate(data.get(kind) or []):
                cur.execute(
                    "INSERT INTO allegation_evidence (id, allegation_id, kind, text, report_id, position) VALUES (%s, %s, %s, %s, %s, %s)",
                    (item["id"], allegation_id, kind, item.get("text", ""), item.get("report_id") or None, position),
                )
        cur.execute("DELETE FROM allegation_to_prove WHERE allegation_id = %s", (allegation_id,))  # cascades to allegation_to_prove_evidence
        for position, item in enumerate(data.get("to_prove") or []):
            cur.execute(
                "INSERT INTO allegation_to_prove (id, allegation_id, title, summary, position) VALUES (%s, %s, %s, %s, %s)",
                (item["id"], allegation_id, item.get("title", ""), item.get("summary", ""), position),
            )
            for link_position, evidence_id in enumerate(item.get("evidence_ids") or []):
                cur.execute(
                    "INSERT INTO allegation_to_prove_evidence (to_prove_id, evidence_id, position) VALUES (%s, %s, %s)",
                    (item["id"], evidence_id, link_position),
                )
        cur.execute("DELETE FROM allegation_cases WHERE allegation_id = %s", (allegation_id,))
        for position, case_id in enumerate(data.get("case_ids") or []):
            cur.execute(
                "INSERT INTO allegation_cases (allegation_id, case_id, position) VALUES (%s, %s, %s)",
                (allegation_id, case_id, position),
            )


def delete_allegation(allegation_id: str) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM allegations WHERE id = %s", (allegation_id,))


def set_allegation_order(order: list[str]) -> list[str]:
    """Persists a new display order. `order` need not mention every
    allegation -- anything left unmentioned keeps its place, appended after
    the ones just reordered, in its previous relative order."""
    with _cursor() as cur:
        cur.execute("SELECT id FROM allegations ORDER BY order_index")
        current_order = [r["id"] for r in cur.fetchall()]
        existing_ids = set(current_order)

        seen: list[str] = []
        for allegation_id in order:
            if allegation_id in existing_ids and allegation_id not in seen:
                seen.append(allegation_id)
        seen_set = set(seen)
        seen += [allegation_id for allegation_id in current_order if allegation_id not in seen_set]

        for index, allegation_id in enumerate(seen):
            cur.execute("UPDATE allegations SET order_index = %s WHERE id = %s", (index, allegation_id))
    return seen
