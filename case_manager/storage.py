"""The single persistence boundary for case_manager.

Every other module (app.py above all) talks to *this* module only -- never
to psycopg2, SQL, or a filesystem path directly. In return, this module
promises to speak in the same plain dicts/lists/strings the rest of the app
already works with (the exact shapes storage/*.json used to hold), never in
rows, cursors, or paths. That's the whole point: swapping what's behind this
module (a different database, a different blob store) later means editing
this one file, not any of app.py's routes.

Two things live here, both "persistence":
  - structured data, in PostgreSQL (see db/schema.sql and db/migrations/
    for the tables) -- documents' metadata, annotations, snippet metadata,
    reports, cases, causes, allegations, and users with their sign-in
    sessions and per-object access roles.
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
from collections.abc import Collection
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import psycopg2
import psycopg2.errors
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from converters import ConversionError, convert_to_pdf
from db.storage_backend import (
    cache_storage_dir, document_storage_key, get_storage_backend, owner_dir_name, snippet_storage_key,
)


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


class DeleteBlocked(Exception):
    """A delete refused because something still depends on the object; the
    message says what. Every delete_* below checks this inside the same
    transaction as the delete, after locking the object's row (FOR UPDATE):
    a concurrent insert that references the row takes a conflicting lock via
    its foreign key, so the check and the delete can't be interleaved with
    a new dependent."""


def _lock(cur, table: str, object_id: str) -> bool:
    cur.execute(f"SELECT 1 FROM {table} WHERE id = %s FOR UPDATE", (object_id,))  # noqa: S608 (fixed table names)
    return cur.fetchone() is not None


def _refuse_if_any(cur, sql: str, params: tuple, message: str) -> None:
    cur.execute(sql, params)
    if cur.fetchone() is not None:
        raise DeleteBlocked(message)


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


# The causes/cases a report or source is associated with (the report_causes /
# report_cases / source_causes / source_cases tables), as two id arrays --
# spliced into a SELECT over reports (alias r) or documents (alias d).
_LINKED_IDS_SQL = """
    ARRAY(SELECT l.cause_id FROM {table_prefix}_causes l WHERE l.{key} = {alias}.id ORDER BY l.created_at) AS cause_ids,
    ARRAY(SELECT l.case_id FROM {table_prefix}_cases l WHERE l.{key} = {alias}.id ORDER BY l.created_at) AS case_ids
"""


# ---------------------------------------------------------------------------
# Documents (metadata) + their bytes + the docx->pdf render cache
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent


def _conversion_cache_dir(owner: str | None) -> Path:
    return _BASE_DIR / cache_storage_dir(owner)


def _conversion_cache_path(document_id: str, owner: str | None) -> Path:
    return _conversion_cache_dir(owner) / f"{document_id}.pdf"


def _document_location(document_id: str) -> tuple[str, str | None] | None:
    """(doc_type, storage owner folder) -- everything needed to build a
    document's or its snippets' storage keys -- or None if there's no such
    document."""
    with _cursor() as cur:
        cur.execute("SELECT doc_type, storage_owner FROM documents WHERE id = %s", (document_id,))
        row = cur.fetchone()
    return (row["doc_type"], row["storage_owner"]) if row else None


def _cause_owner_dir(cur, link: tuple[str, str], fallback_user_id: int) -> str:
    """The storage folder for a document uploaded for `link` = ("cause" |
    "case", id): the owner of that cause's users.storage_dir (of the case's
    cause). The uploader stands in only if the cause has no owner row."""
    kind, target_id = link
    cause_id = target_id
    if kind == "case":
        cur.execute("SELECT cause_id FROM cases WHERE id = %s", (target_id,))
        row = cur.fetchone()
        cause_id = row["cause_id"] if row else None
    cur.execute(
        """
        SELECT u.storage_dir FROM user_causes uc JOIN users u ON u.id = uc.user_id
        WHERE uc.cause_id = %s AND uc.role = 'owner' ORDER BY uc.created_at, u.id LIMIT 1
        """,
        (cause_id,),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT storage_dir FROM users WHERE id = %s", (fallback_user_id,))
        row = cur.fetchone()
    return row["storage_dir"]


def list_documents(user_id: int) -> list[dict]:
    """Every uploaded source document this user has any role on, id-sorted
    -- {"id", "type", "title", "role", "cause_ids", "case_ids", "snippet_count"}."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.doc_type, d.title, us.role, {links},
                   (SELECT count(*) FROM snippets s WHERE s.document_id = d.id) AS snippet_count
            FROM documents d JOIN user_sources us ON us.document_id = d.id AND us.user_id = %s
            ORDER BY d.id
            """.format(links=_LINKED_IDS_SQL.format(table_prefix="source", key="document_id", alias="d")),
            (user_id,),
        )
        rows = cur.fetchall()
    return [
        {"id": r["id"], "type": r["doc_type"], "title": r["title"], "role": r["role"],
         "cause_ids": list(r["cause_ids"]), "case_ids": list(r["case_ids"]),
         "snippet_count": r["snippet_count"]}
        for r in rows
    ]


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


def create_document(document_id: str, doc_type: str, data: bytes, owner_id: int, link: tuple[str, str]) -> None:
    """Registers a freshly uploaded document: writes its bytes via the
    storage backend, then -- in one transaction -- its row (title defaults
    to its own id, matching the old doc_meta fallback), its uploader's
    ownership, and its first association, `link` = ("cause" | "case", id).
    The bytes go under the owner of that cause (see _cause_owner_dir), a
    folder fixed on the row from then on."""
    with _cursor() as cur:
        owner = _cause_owner_dir(cur, link, owner_id)
    get_storage_backend().write_bytes(document_storage_key(document_id, doc_type, owner), data)
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO documents (id, doc_type, title, storage_owner) VALUES (%s, %s, %s, %s)",
            (document_id, doc_type, document_id, owner),
        )
        _grant(cur, owner_id, "source", document_id, "owner")
        _link(cur, "source", document_id, *link)


def delete_document(document_id: str) -> None:
    """Removes a document's row plus its file (and any docx->pdf render
    cache entry). Refused while it has annotations or snippets, is linked
    to a hearing, is the source of a report, or is in an annexure -- deleting it would
    silently cut those references."""
    location = _document_location(document_id)
    if location is None:
        return
    doc_type, owner = location
    with _cursor() as cur:
        if not _lock(cur, "documents", document_id):
            return
        _refuse_if_any(cur, "SELECT 1 FROM document_annotations WHERE document_id = %s AND jsonb_array_length(shapes) > 0 LIMIT 1",
                       (document_id,), "Cannot delete a document that has annotations. Remove them first.")
        _refuse_if_any(cur, "SELECT 1 FROM snippets WHERE document_id = %s LIMIT 1", (document_id,),
                       "Cannot delete a document that has snippets. Remove them first.")
        _refuse_if_any(cur, "SELECT 1 FROM hearing_documents WHERE document_id = %s LIMIT 1", (document_id,),
                       "Cannot delete a document that is linked to a hearing")
        _refuse_if_any(cur, "SELECT 1 FROM reports WHERE source_document_id = %s LIMIT 1", (document_id,),
                       "Cannot delete a document that is the source of a report")
        _refuse_if_any(cur, "SELECT 1 FROM report_annexure_documents WHERE document_id = %s LIMIT 1", (document_id,),
                       "Cannot delete a document that is included in an annexure. Remove it from the annexure first.")
        cur.execute("DELETE FROM documents WHERE id = %s", (document_id,))
    # The row goes first: if that fails nothing is lost, and if the file
    # delete fails afterwards the worst case is a stray file, never a row
    # whose file is missing.
    get_storage_backend().delete(document_storage_key(document_id, doc_type, owner))
    _conversion_cache_path(document_id, owner).unlink(missing_ok=True)


def get_document_pdf_bytes(document_id: str) -> bytes:
    """The document's content as PDF bytes -- straight from storage for a
    native PDF, or converted-and-cached (LibreOffice, run at most once per
    document since uploads are never replaced in place) for a Word doc."""
    location = _document_location(document_id)
    if location is None:
        raise StorageError(f"No document with id {document_id!r}")
    doc_type, owner = location

    backend = get_storage_backend()
    if doc_type == "pdf":
        return backend.read_bytes(document_storage_key(document_id, "pdf", owner))

    cache_dir = _conversion_cache_dir(owner)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = _conversion_cache_path(document_id, owner)
    if not cache_path.exists():
        source_bytes = backend.read_bytes(document_storage_key(document_id, doc_type, owner))
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / f"{document_id}.{doc_type}"
            source_path.write_bytes(source_bytes)
            try:
                converted = convert_to_pdf(source_path, cache_dir)
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
    location = _document_location(document_id)
    if location is None:
        raise StorageError(f"No document with id {document_id!r}")
    doc_type, owner = location

    snippet_id = uuid.uuid4().hex[:12]
    filename = f"p{page}_{snippet_id}.png"
    get_storage_backend().write_bytes(snippet_storage_key(document_id, doc_type, filename, owner), png_bytes)

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


def replace_snippet_image(document_id: str, snippet: dict, annotated: bool, png_bytes: bytes) -> None:
    """Overwrites an existing snippet's PNG in place (same filename, so the
    URLs reports embed keep working) and refreshes its annotated flag."""
    location = _document_location(document_id)
    if location is None:
        raise StorageError(f"No document with id {document_id!r}")
    doc_type, owner = location
    get_storage_backend().write_bytes(snippet_storage_key(document_id, doc_type, snippet["filename"], owner), png_bytes)
    with _cursor() as cur:
        cur.execute("UPDATE snippets SET annotated = %s WHERE id = %s", (annotated, snippet["id"]))


def delete_snippet(document_id: str, snippet_id: str) -> bool:
    """Refused while a report's content embeds the snippet's image (reports
    reference it by URL; save_report mirrors those URLs into report_snippets)."""
    with _cursor() as cur:
        cur.execute("SELECT filename FROM snippets WHERE id = %s AND document_id = %s", (snippet_id, document_id))
        row = cur.fetchone()
        if row is None:
            return False
        _refuse_if_any(cur, "SELECT 1 FROM report_snippets WHERE snippet_id = %s LIMIT 1", (snippet_id,),
                       "Cannot delete a snippet that is used in a report")
        cur.execute("DELETE FROM snippets WHERE id = %s", (snippet_id,))

    location = _document_location(document_id)
    if location is not None:
        doc_type, owner = location
        get_storage_backend().delete(snippet_storage_key(document_id, doc_type, row["filename"], owner))
    return True


def read_snippet_bytes(document_id: str, filename: str) -> bytes | None:
    location = _document_location(document_id)
    if location is None:
        return None
    doc_type, owner = location
    backend = get_storage_backend()
    key = snippet_storage_key(document_id, doc_type, filename, owner)
    if not backend.exists(key):
        return None
    return backend.read_bytes(key)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

_REPORT_COLUMNS = """
    r.id, r.name, r.doc, r.source_document_id, d.doc_type AS source_doc_type,
    r.margins, r.page_numbers, r.created_at, r.updated_at,
""" + _LINKED_IDS_SQL.format(table_prefix="report", key="report_id", alias="r")


def _report_row_to_dict(row: dict) -> dict:
    source_doc = row["source_document_id"] or ""
    return {
        "id": row["id"],
        "name": row["name"],
        "doc": row["doc"],
        "source_doc": source_doc,
        "source_type": _doc_family(row["source_doc_type"]) if source_doc else "pdf",
        "cause_ids": list(row["cause_ids"]),
        "case_ids": list(row["case_ids"]),
        "margins": row["margins"],
        "pageNumbers": row["page_numbers"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def list_reports(user_id: int) -> list[dict]:
    """The lightweight projection the reports list view needs, for the
    reports this user has any role on -- callers that need a report's full
    document tree use get_report instead, so listing every report never has
    to ship every report's (potentially large) content over the wire."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.name, r.source_document_id, d.doc_type AS source_doc_type,
                   r.created_at, r.updated_at, ur.role, {links},
                   (SELECT count(*) FROM report_snippets rs WHERE rs.report_id = r.id) AS snippet_count
            FROM reports r
            JOIN user_reports ur ON ur.report_id = r.id AND ur.user_id = %s
            LEFT JOIN documents d ON d.id = r.source_document_id
            """.format(links=_LINKED_IDS_SQL.format(table_prefix="report", key="report_id", alias="r")),
            (user_id,),
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
            "cause_ids": list(r["cause_ids"]),
            "case_ids": list(r["case_ids"]),
            "role": r["role"],
            "snippet_count": r["snippet_count"],
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


def _snippet_refs(node, found: set | None = None) -> set[tuple[str, str]]:
    """(document_id, filename) of every snippet image a Tiptap doc embeds."""
    found = set() if found is None else found
    if isinstance(node, dict):
        if node.get("type") == "image":
            parts = [p for p in urlsplit((node.get("attrs") or {}).get("src", "")).path.split("/") if p]
            if len(parts) >= 4 and parts[-3:-2] == ["snippets"] and parts[-4] == "media":
                found.add((parts[-2], parts[-1]))
        for child in node.get("content") or []:
            _snippet_refs(child, found)
    return found


def save_report(report_id: str, data: dict, owner_id: int | None = None,
                link: tuple[str, str] | None = None) -> None:
    """Creates or overwrites a report. When creating, pass `owner_id` and
    `link` (its first association, ("cause" | "case", id)) to record both in
    the same transaction. Associations are otherwise managed with
    link_to/unlink_from, never by a save."""
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
        # Refs to snippets that no longer exist (a broken image) are dropped by the join.
        refs = sorted(_snippet_refs(data["doc"]))
        cur.execute("DELETE FROM report_snippets WHERE report_id = %s", (report_id,))
        if refs:
            cur.execute(
                """
                INSERT INTO report_snippets (report_id, snippet_id)
                SELECT %s, s.id FROM snippets s
                JOIN unnest(%s::text[], %s::text[]) AS ref(document_id, filename)
                  ON s.document_id = ref.document_id AND s.filename = ref.filename
                """,
                (report_id, [r[0] for r in refs], [r[1] for r in refs]),
            )
        if owner_id is not None:
            _grant(cur, owner_id, "report", report_id, "owner")
        if link is not None:
            _link(cur, "report", report_id, *link)


def report_snippet_pages(report_id: str) -> dict[str, list[int]]:
    """{document_id: sorted page numbers} of the snippets a report embeds."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT s.document_id, s.page_number FROM report_snippets rs
            JOIN snippets s ON s.id = rs.snippet_id
            WHERE rs.report_id = %s ORDER BY s.document_id, s.page_number
            """,
            (report_id,),
        )
        rows = cur.fetchall()
    pages: dict[str, list[int]] = {}
    for r in rows:
        if r["page_number"] not in pages.setdefault(r["document_id"], []):
            pages[r["document_id"]].append(r["page_number"])
    return pages


def get_annexure(report_id: str) -> list[dict]:
    """[{"id": document id, "page_mode": "all" | "snippets" | "custom",
    "page_range": str}] in annexure order."""
    with _cursor() as cur:
        cur.execute(
            "SELECT document_id, page_mode, page_range FROM report_annexure_documents WHERE report_id = %s ORDER BY position",
            (report_id,),
        )
        return [{"id": r["document_id"], "page_mode": r["page_mode"], "page_range": r["page_range"]}
                for r in cur.fetchall()]


def save_annexure(report_id: str, documents: list[dict]) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM report_annexure_documents WHERE report_id = %s", (report_id,))
        for position, d in enumerate(documents):
            cur.execute(
                "INSERT INTO report_annexure_documents (report_id, document_id, position, page_mode, page_range) VALUES (%s, %s, %s, %s, %s)",
                (report_id, d["id"], position, d["page_mode"], d["page_range"]),
            )


def get_annexure_page_numbers(report_id: str) -> dict | None:
    with _cursor() as cur:
        cur.execute("SELECT annexure_page_numbers FROM reports WHERE id = %s", (report_id,))
        row = cur.fetchone()
    return row["annexure_page_numbers"] if row else None


def save_annexure_page_numbers(report_id: str, page_numbers: dict) -> None:
    with _cursor() as cur:
        cur.execute("UPDATE reports SET annexure_page_numbers = %s WHERE id = %s",
                    (psycopg2.extras.Json(page_numbers), report_id))


def get_annexure_include_annotations(report_id: str) -> bool:
    with _cursor() as cur:
        cur.execute("SELECT annexure_include_annotations FROM reports WHERE id = %s", (report_id,))
        row = cur.fetchone()
    return bool(row and row["annexure_include_annotations"])


def save_annexure_include_annotations(report_id: str, include: bool) -> None:
    with _cursor() as cur:
        cur.execute("UPDATE reports SET annexure_include_annotations = %s WHERE id = %s", (include, report_id))


def delete_report(report_id: str) -> None:
    """Refused while an allegation's evidence cites the report (the foreign
    key would otherwise just null the citation)."""
    with _cursor() as cur:
        if not _lock(cur, "reports", report_id):
            return
        _refuse_if_any(cur, "SELECT 1 FROM allegation_evidence WHERE report_id = %s LIMIT 1", (report_id,),
                       "Cannot delete a report that is cited in an allegation's evidence")
        cur.execute("DELETE FROM reports WHERE id = %s", (report_id,))


# ---------------------------------------------------------------------------
# Cases (+ hearings, + each hearing's linked documents)
# ---------------------------------------------------------------------------

def _hearing_doc_dict(row: dict) -> dict:
    return {"id": row["id"], "doc_id": row["document_id"], "doc_type": _doc_family(row["doc_type"])}


def _assemble_cases(case_rows, hearing_rows, doc_link_rows, roles=None) -> list[dict]:
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
            **({"role": roles[c["id"]]} if roles else {}),
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


def list_cases(user_id: int) -> list[dict]:
    """The cases this user has any role on, each carrying that "role"."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.name, c.cause_id, c.court, c.case_number, c.summary, c.created_at, c.updated_at, uc.role
            FROM cases c JOIN user_cases uc ON uc.case_id = c.id AND uc.user_id = %s
            """,
            (user_id,),
        )
        case_rows = cur.fetchall()
        case_ids = [c["id"] for c in case_rows]
        cur.execute(
            "SELECT id, case_id, hearing_date, title, summary FROM hearings WHERE case_id = ANY(%s) ORDER BY position",
            (case_ids,),
        )
        hearing_rows = cur.fetchall()
        cur.execute(
            _HEARING_DOC_JOIN + " JOIN hearings h ON h.id = hd.hearing_id WHERE h.case_id = ANY(%s) ORDER BY hd.position",
            (case_ids,),
        )
        doc_link_rows = cur.fetchall()
    return _assemble_cases(case_rows, hearing_rows, doc_link_rows, roles={c["id"]: c["role"] for c in case_rows})


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


def save_case(case_id: str, data: dict, owner_id: int | None = None) -> None:
    """Creates or overwrites a case. Pass `owner_id` when creating, to
    record the creator's ownership in the same transaction."""
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
        if owner_id is not None:
            _grant(cur, owner_id, "case", case_id, "owner")


def delete_case(case_id: str) -> None:
    """Refused while the case has hearings or allegations. Reports/sources
    associated only with this case move to its cause."""
    with _cursor() as cur:
        cur.execute("SELECT cause_id FROM cases WHERE id = %s FOR UPDATE", (case_id,))
        row = cur.fetchone()
        if row is None:
            return
        _refuse_if_any(cur, "SELECT 1 FROM hearings WHERE case_id = %s LIMIT 1", (case_id,),
                       "Cannot delete a case that still has hearings")
        _refuse_if_any(cur, "SELECT 1 FROM allegation_cases WHERE case_id = %s LIMIT 1", (case_id,),
                       "Cannot delete a case that still has allegations")
        _relink_orphans(cur, "case", case_id, row["cause_id"])
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


def list_causes(user_id: int) -> list[dict]:
    """The causes this user has any role on, each carrying that "role"."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.title, c.description, c.created_at, c.updated_at, uc.role
            FROM causes c JOIN user_causes uc ON uc.cause_id = c.id AND uc.user_id = %s
            """,
            (user_id,),
        )
        cause_rows = cur.fetchall()
        cur.execute(_GOAL_QUERY.format(where="WHERE g.cause_id = ANY(%s)"), ([c["id"] for c in cause_rows],))
        goal_rows = cur.fetchall()
    goals_by_cause: dict[str, list] = {}
    for g in goal_rows:
        goals_by_cause.setdefault(g["cause_id"], []).append(_goal_dict(g))
    return [
        {
            "id": c["id"], "title": c["title"], "description": c["description"],
            "goals": goals_by_cause.get(c["id"], []),
            "role": c["role"],
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


def save_cause(cause_id: str, data: dict, owner_id: int | None = None) -> None:
    """Creates or overwrites a cause. Pass `owner_id` when creating, to
    record the creator's ownership in the same transaction."""
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
        if owner_id is not None:
            _grant(cur, owner_id, "cause", cause_id, "owner")



def _relink_orphans(cur, gone_kind: str, gone_id: str, new_cause_id: str) -> None:
    """Called just before a cause or case (`gone_kind`) is deleted: every
    report and source whose *only* association is that cause/case gets
    `new_cause_id` instead, so none is left associated with nothing (the
    ON DELETE CASCADE on the link tables would otherwise drop the link
    silently)."""
    for table_prefix, key in (("report", "report_id"), ("source", "document_id")):
        own = f"{table_prefix}_{gone_kind}s"
        other = f"{table_prefix}_{'case' if gone_kind == 'cause' else 'cause'}s"
        cur.execute(
            f"""
            INSERT INTO {table_prefix}_causes ({key}, cause_id)
            SELECT o.{key}, %s FROM {own} o
            WHERE o.{gone_kind}_id = %s
              AND NOT EXISTS (SELECT 1 FROM {own} x WHERE x.{key} = o.{key} AND x.{gone_kind}_id <> %s)
              AND NOT EXISTS (SELECT 1 FROM {other} y WHERE y.{key} = o.{key})
            ON CONFLICT DO NOTHING
            """,  # noqa: S608 (table/column names are fixed above)
            (new_cause_id, gone_id, gone_id),
        )


def delete_cause(cause_id: str, fallback_cause_id: str, user_id: int) -> None:
    """Refused while the cause has goals, cases or allegations, or is
    `user_id`'s last cause they can edit. Otherwise, in one transaction:
    reports/sources associated only with it move to `fallback_cause_id`,
    and so does `user_id`'s default if it was this cause."""
    with _cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user_id,))  # serializes this user's cause deletes
        if not _lock(cur, "causes", cause_id):
            return
        _refuse_if_any(cur, "SELECT 1 FROM goals WHERE cause_id = %s LIMIT 1", (cause_id,),
                       "Cannot delete a cause that still has goals")
        _refuse_if_any(cur, "SELECT 1 FROM cases WHERE cause_id = %s LIMIT 1", (cause_id,),
                       "Cannot delete a cause that still has cases")
        _refuse_if_any(cur, "SELECT 1 FROM allegations WHERE cause_id = %s LIMIT 1", (cause_id,),
                       "Cannot delete a cause that still has allegations")
        cur.execute("SELECT COUNT(*) AS n FROM user_causes WHERE user_id = %s AND role IN ('owner', 'editor')", (user_id,))
        if cur.fetchone()["n"] <= 1:
            raise DeleteBlocked("Cannot delete your last remaining cause")
        _relink_orphans(cur, "cause", cause_id, fallback_cause_id)
        cur.execute(
            "UPDATE users SET default_cause_id = %s WHERE id = %s AND default_cause_id = %s",
            (fallback_cause_id, user_id, cause_id),
        )
        cur.execute("DELETE FROM causes WHERE id = %s", (cause_id,))


def create_general_cause(user_id: int) -> str:
    """Creates a "General" cause owned by this user and makes it their
    default -- every user starts with one, so they always have a default
    cause. Returns its id."""
    now = datetime.now(timezone.utc).isoformat()
    cause_id = f"general-{uuid.uuid4().hex[:6]}"
    save_cause(cause_id, {"title": "General", "description": "", "goals": [],
                          "created_at": now, "updated_at": now}, owner_id=user_id)
    set_default_cause(user_id, cause_id)
    return cause_id


def most_recently_updated_editable_cause_id(user_id: int, exclude: str | None = None) -> str | None:
    """The most recently updated cause this user can add cases to (editor
    or owner), optionally skipping `exclude`."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT c.id FROM causes c
            JOIN user_causes uc ON uc.cause_id = c.id AND uc.user_id = %s AND uc.role IN ('owner', 'editor')
            WHERE c.id IS DISTINCT FROM %s
            ORDER BY c.updated_at DESC LIMIT 1
            """,
            (user_id, exclude),
        )
        row = cur.fetchone()
        return row["id"] if row else None


def get_default_cause(user_id: int) -> str | None:
    with _cursor() as cur:
        cur.execute("SELECT default_cause_id FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return row["default_cause_id"] if row else None


def set_default_cause(user_id: int, cause_id: str) -> None:
    with _cursor() as cur:
        cur.execute("UPDATE users SET default_cause_id = %s WHERE id = %s", (cause_id, user_id))


def set_show_advanced(user_id: int, show: bool) -> None:
    with _cursor() as cur:
        cur.execute("UPDATE users SET show_advanced = %s WHERE id = %s", (show, user_id))


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
            "cause_id": a["cause_id"],
            "to_prove": [_to_prove_dict(t) for t in to_prove_by_allegation.get(a["id"], [])],
            "inculpatory": [_evidence_dict(r) for r in evidence if r["kind"] == "inculpatory"],
            "exculpatory": [_evidence_dict(r) for r in evidence if r["kind"] == "exculpatory"],
            "case_ids": cases_by_allegation.get(a["id"], []),
            "created_at": _iso(a["created_at"]),
            "updated_at": _iso(a["updated_at"]),
        })
    return items


def list_allegations(cause_ids: Collection[str], allegation_ids: Collection[str] = ()) -> list[dict]:
    """In display order (allegations/allegation_order.json's old job is now
    just ORDER BY order_index -- see set_allegation_order). Only those under
    `cause_ids` or named in `allegation_ids` -- callers pass the causes and
    allegations the user may see."""
    with _cursor() as cur:
        cur.execute(
            "SELECT id, title, description, cause_id, created_at, updated_at FROM allegations "
            "WHERE cause_id = ANY(%s) OR id = ANY(%s) ORDER BY order_index",
            (list(cause_ids), list(allegation_ids)),
        )
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
        cur.execute("SELECT id, title, description, cause_id, created_at, updated_at FROM allegations WHERE id = %s", (allegation_id,))
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
            INSERT INTO allegations (id, cause_id, title, description, order_index, created_at, updated_at)
            VALUES (%s, %s, %s, %s, (SELECT COALESCE(MAX(order_index), -1) + 1 FROM allegations), %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                cause_id = EXCLUDED.cause_id, title = EXCLUDED.title, description = EXCLUDED.description,
                updated_at = EXCLUDED.updated_at
            """,
            (allegation_id, data["cause_id"], data["title"], data["description"], data["created_at"], data["updated_at"]),
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
    """Refused while any of its evidence has a report linked."""
    with _cursor() as cur:
        if not _lock(cur, "allegations", allegation_id):
            return
        _refuse_if_any(cur, "SELECT 1 FROM allegation_evidence WHERE allegation_id = %s AND report_id IS NOT NULL LIMIT 1",
                       (allegation_id,), "Cannot delete an allegation that still has reports linked to its evidence")
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


# ---------------------------------------------------------------------------
# Users and their sign-in sessions. Only a password *hash* ever reaches this
# module (see auth.py's User class) -- never the password itself -- and only
# a session token's SHA-256, never the token the browser holds.
# ---------------------------------------------------------------------------

def create_user(email: str, password_hash: str) -> dict | None:
    """{"id", "email", "password_hash"} for the new user, or None if that
    email (case-insensitively) is already registered."""
    try:
        with _cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, password_hash, storage_dir) VALUES (%s, %s, %s) RETURNING id, email, password_hash",
                (email, password_hash, owner_dir_name(email)),
            )
            return dict(cur.fetchone())
    except psycopg2.errors.UniqueViolation:
        return None


def get_user_by_email(email: str) -> dict | None:
    with _cursor() as cur:
        cur.execute("SELECT id, email, password_hash FROM users WHERE lower(email) = lower(%s)", (email,))
        row = cur.fetchone()
    return dict(row) if row else None


def create_session(user_id: int, token_hash: str, expires_at: datetime) -> None:
    """Records a new sign-in, clearing out any of this user's sessions that
    have already expired while it's at it."""
    with _cursor() as cur:
        cur.execute("DELETE FROM user_sessions WHERE user_id = %s AND expires_at <= now()", (user_id,))
        cur.execute(
            "INSERT INTO user_sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
            (token_hash, user_id, expires_at),
        )


def get_session_user(token_hash: str) -> dict | None:
    """{"id", "email", "password_hash"} of the user a still-unexpired
    session belongs to, else None."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.email, u.password_hash, u.show_advanced FROM user_sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.expires_at > now()
            """,
            (token_hash,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def delete_session(token_hash: str) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM user_sessions WHERE token_hash = %s", (token_hash,))


def record_signup_attempt(ip: str, limit: int, window_seconds: int) -> bool:
    """Counts a sign-up attempt from `ip` unless it already made `limit`
    within the last `window_seconds`; returns whether it was allowed.
    Refused attempts aren't recorded, so the window slides rather than
    extending itself."""
    with _cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (ip,))  # serialize this IP's checks across workers
        cur.execute("DELETE FROM signup_attempts WHERE attempted_at <= now() - make_interval(secs => %s)", (window_seconds,))
        cur.execute("SELECT count(*) AS n FROM signup_attempts WHERE ip = %s", (ip,))
        if cur.fetchone()["n"] >= limit:
            return False
        cur.execute("INSERT INTO signup_attempts (ip) VALUES (%s)", (ip,))
    return True


def create_signup_captcha(captcha_id: str, answer: str, expires_at: datetime) -> None:
    """Records a captcha challenge, sweeping out expired ones while it's at it."""
    with _cursor() as cur:
        cur.execute("DELETE FROM signup_captchas WHERE expires_at <= now()")
        cur.execute(
            "INSERT INTO signup_captchas (id, answer, expires_at) VALUES (%s, %s, %s)",
            (captcha_id, answer, expires_at),
        )


def take_signup_captcha_answer(captcha_id: str) -> str | None:
    """Removes a still-unexpired captcha challenge and returns its answer
    (None if it doesn't exist, was already used, or has expired), so each
    challenge can be answered only once."""
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM signup_captchas WHERE id = %s AND expires_at > now() RETURNING answer",
            (captcha_id,),
        )
        row = cur.fetchone()
    return row["answer"] if row else None


# ---------------------------------------------------------------------------
# Per-object access roles (owner / editor / viewer) -- one association table
# per kind of object, see db/migrations/001_users_and_access.sql. A user with
# no row for an object has no access to it at all.
# ---------------------------------------------------------------------------

_ACCESS_TABLES = {
    "cause": ("user_causes", "cause_id"),
    "case": ("user_cases", "case_id"),
    "report": ("user_reports", "report_id"),
    "source": ("user_sources", "document_id"),
    "allegation": ("user_allegations", "allegation_id"),
}


def _grant(cur, user_id: int, kind: str, object_id: str, role: str) -> None:
    table, column = _ACCESS_TABLES[kind]
    cur.execute(
        f"""
        INSERT INTO {table} (user_id, {column}, role) VALUES (%s, %s, %s)
        ON CONFLICT (user_id, {column}) DO UPDATE SET role = EXCLUDED.role
        """,  # noqa: S608 (table/column come from _ACCESS_TABLES, never from input)
        (user_id, object_id, role),
    )


def get_role(user_id: int, kind: str, object_id: str) -> str | None:
    """"owner" / "editor" / "viewer", or None if this user has no access to
    that object (including when it doesn't exist)."""
    table, column = _ACCESS_TABLES[kind]
    with _cursor() as cur:
        cur.execute(f"SELECT role FROM {table} WHERE user_id = %s AND {column} = %s", (user_id, object_id))  # noqa: S608
        row = cur.fetchone()
    return row["role"] if row else None


def accessible_ids(user_id: int, kind: str) -> set[str]:
    """Ids of every object of this kind the user has any role on."""
    table, column = _ACCESS_TABLES[kind]
    with _cursor() as cur:
        cur.execute(f"SELECT {column} AS id FROM {table} WHERE user_id = %s", (user_id,))  # noqa: S608
        return {r["id"] for r in cur.fetchall()}


def can_view_snippet_images(user_id: int, document_id: str) -> bool:
    """Snippet PNGs get embedded in reports, so they're visible to anyone
    who can view either their source document or a report drawing on that
    document -- otherwise sharing a report would show its reader broken
    images."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS(SELECT 1 FROM user_sources WHERE user_id = %s AND document_id = %s)
                OR EXISTS(SELECT 1 FROM reports r JOIN user_reports ur ON ur.report_id = r.id
                          WHERE ur.user_id = %s AND r.source_document_id = %s) AS ok
            """,
            (user_id, document_id, user_id, document_id),
        )
        return cur.fetchone()["ok"]


# ---------------------------------------------------------------------------
# Collaborations (db/migrations/015_collaborations.sql): one row per pair of
# users, created by an invitation and confirmed when the invitee accepts.
# Between confirmed collaborators, an owner shares objects by adding
# viewer/editor rows to the access tables above.
# ---------------------------------------------------------------------------

# kind -> (object table, display-title column)
_OBJECT_TITLES = {
    "cause": ("causes", "title"),
    "case": ("cases", "name"),
    "allegation": ("allegations", "title"),
    "report": ("reports", "name"),
    "source": ("documents", "title"),
}
SHARE_ROLES = ("viewer", "editor")
SHARE_KINDS = tuple(_OBJECT_TITLES)


def find_collaboration(user_a: int, user_b: int) -> dict | None:
    """The collaboration between two users, whoever invited whom."""
    with _cursor() as cur:
        cur.execute(
            "SELECT id, inviter_id, invitee_id, status FROM collaborations "
            "WHERE (inviter_id = %s AND invitee_id = %s) OR (inviter_id = %s AND invitee_id = %s)",
            (user_a, user_b, user_b, user_a),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def create_collaboration(inviter_id: int, invitee_id: int) -> int | None:
    """Id of the new pending invitation, or None if these two already have one."""
    try:
        with _cursor() as cur:
            cur.execute(
                "INSERT INTO collaborations (inviter_id, invitee_id) VALUES (%s, %s) RETURNING id",
                (inviter_id, invitee_id),
            )
            return cur.fetchone()["id"]
    except psycopg2.errors.UniqueViolation:
        return None


def get_collaboration(collaboration_id: int) -> dict | None:
    with _cursor() as cur:
        cur.execute("SELECT id, inviter_id, invitee_id, status FROM collaborations WHERE id = %s", (collaboration_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def list_collaborations(user_id: int) -> list[dict]:
    """Every collaboration this user is part of, most recent activity first:
    {"id", "other_id", "email", "status" ("sent" / "received" / "confirmed"),
    "at", "is_new"}. `is_new` marks an invitation just received, or an
    acceptance the user has yet to see."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.inviter_id, c.status, c.inviter_seen,
                   COALESCE(c.responded_at, c.created_at) AS at,
                   o.id AS other_id, o.email
            FROM collaborations c
            JOIN users o ON o.id = CASE WHEN c.inviter_id = %s THEN c.invitee_id ELSE c.inviter_id END
            WHERE c.inviter_id = %s OR c.invitee_id = %s
            ORDER BY at DESC, c.id DESC
            """,
            (user_id, user_id, user_id),
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        mine = r["inviter_id"] == user_id
        status = "confirmed" if r["status"] == "confirmed" else ("sent" if mine else "received")
        items.append({
            "id": r["id"], "other_id": r["other_id"], "email": r["email"], "status": status, "at": _iso(r["at"]),
            "is_new": status == "received" or (status == "confirmed" and mine and not r["inviter_seen"]),
        })
    return items


def accept_collaboration(collaboration_id: int, user_id: int) -> bool:
    """Confirms a pending invitation addressed to `user_id`."""
    with _cursor() as cur:
        cur.execute(
            "UPDATE collaborations SET status = 'confirmed', responded_at = now() "
            "WHERE id = %s AND invitee_id = %s AND status = 'pending'",
            (collaboration_id, user_id),
        )
        return cur.rowcount == 1


def delete_pending_collaboration(collaboration_id: int, user_id: int) -> bool:
    """Withdraws an invitation the user sent, or declines one they received."""
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM collaborations WHERE id = %s AND status = 'pending' AND (inviter_id = %s OR invitee_id = %s)",
            (collaboration_id, user_id, user_id),
        )
        return cur.rowcount == 1


def count_notifications(user_id: int) -> int:
    """Invitations awaiting this user's answer, plus their invitations that
    were accepted and they haven't looked at yet."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS n FROM collaborations
            WHERE (invitee_id = %s AND status = 'pending')
               OR (inviter_id = %s AND status = 'confirmed' AND NOT inviter_seen)
            """,
            (user_id, user_id),
        )
        return cur.fetchone()["n"]


def mark_acceptances_seen(user_id: int) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE collaborations SET inviter_seen = TRUE WHERE inviter_id = %s AND status = 'confirmed' AND NOT inviter_seen",
            (user_id,),
        )


def list_owned_objects(user_id: int) -> dict[str, list[dict]]:
    """kind -> [{"id", "title"}] of everything this user owns (and so can share)."""
    result = {}
    with _cursor() as cur:
        for kind, (table, column) in _ACCESS_TABLES.items():
            obj_table, title_col = _OBJECT_TITLES[kind]
            cur.execute(
                f"SELECT o.id, o.{title_col} AS title FROM {obj_table} o "  # noqa: S608 (fixed names)
                f"JOIN {table} a ON a.{column} = o.id AND a.user_id = %s AND a.role = 'owner' "
                f"ORDER BY lower(o.{title_col}), o.id",
                (user_id,),
            )
            result[kind] = [{"id": r["id"], "title": r["title"] or r["id"]} for r in cur.fetchall()]
    return result


def list_shared_objects(owner_id: int, collaborator_id: int) -> dict[str, list[dict]]:
    """kind -> [{"id", "title", "role"}]: the objects `owner_id` owns that
    `collaborator_id` holds a (non-owner) role on."""
    result = {}
    with _cursor() as cur:
        for kind, (table, column) in _ACCESS_TABLES.items():
            obj_table, title_col = _OBJECT_TITLES[kind]
            cur.execute(
                f"SELECT o.id, o.{title_col} AS title, c.role FROM {obj_table} o "  # noqa: S608 (fixed names)
                f"JOIN {table} a ON a.{column} = o.id AND a.user_id = %s AND a.role = 'owner' "
                f"JOIN {table} c ON c.{column} = o.id AND c.user_id = %s AND c.role <> 'owner' "
                f"ORDER BY lower(o.{title_col}), o.id",
                (owner_id, collaborator_id),
            )
            result[kind] = [{"id": r["id"], "title": r["title"] or r["id"], "role": r["role"]} for r in cur.fetchall()]
    return result


def set_shared_objects(owner_id: int, collaborator_id: int, grants: dict[str, dict[str, str]]) -> None:
    """Makes the collaborator's roles on the owner's objects of each kind
    named in `grants` ({kind: {object_id: "viewer" | "editor"}}) exactly
    those: objects not listed lose the collaborator's access. Only objects
    the owner owns are touched, and a role of "owner" is never granted,
    changed or removed."""
    with _cursor() as cur:
        for kind, wanted in grants.items():
            table, column = _ACCESS_TABLES[kind]
            cur.execute(f"SELECT {column} AS id FROM {table} WHERE user_id = %s AND role = 'owner'", (owner_id,))  # noqa: S608
            owned = {r["id"] for r in cur.fetchall()}
            keep = [oid for oid in wanted if oid in owned]
            cur.execute(
                f"DELETE FROM {table} WHERE user_id = %s AND role <> 'owner' "  # noqa: S608
                f"AND {column} = ANY(%s) AND NOT ({column} = ANY(%s))",
                (collaborator_id, list(owned), keep),
            )
            for oid in keep:
                cur.execute(
                    f"INSERT INTO {table} (user_id, {column}, role) VALUES (%s, %s, %s) "  # noqa: S608
                    f"ON CONFLICT (user_id, {column}) DO UPDATE SET role = EXCLUDED.role WHERE {table}.role <> 'owner'",
                    (collaborator_id, oid, wanted[oid]),
                )


def grant_owner(kind: str, object_id: str, user_id: int) -> None:
    with _cursor() as cur:
        _grant(cur, user_id, kind, object_id, "owner")


# ---------------------------------------------------------------------------
# Associations between reports/sources and the causes/cases they relate to
# (many-to-many -- see db/migrations/001_users_and_access.sql). Who may add
# or remove one is app.py's decision; these just record it.
# ---------------------------------------------------------------------------

_LINK_TABLES = {
    ("report", "cause"): ("report_causes", "report_id", "cause_id"),
    ("report", "case"): ("report_cases", "report_id", "case_id"),
    ("source", "cause"): ("source_causes", "document_id", "cause_id"),
    ("source", "case"): ("source_cases", "document_id", "case_id"),
}


def _link(cur, kind: str, object_id: str, target_kind: str, target_id: str) -> None:
    table, key, target_key = _LINK_TABLES[(kind, target_kind)]
    cur.execute(
        f"INSERT INTO {table} ({key}, {target_key}) VALUES (%s, %s) ON CONFLICT DO NOTHING",  # noqa: S608 (from _LINK_TABLES)
        (object_id, target_id),
    )


def link_to(kind: str, object_id: str, target_kind: str, target_id: str) -> None:
    """Associates a report or source (`kind`) with a cause or case."""
    with _cursor() as cur:
        _link(cur, kind, object_id, target_kind, target_id)


def unlink_from(kind: str, object_id: str, target_kind: str, target_id: str) -> bool:
    """Removes that association; False if there wasn't one."""
    table, key, target_key = _LINK_TABLES[(kind, target_kind)]
    with _cursor() as cur:
        cur.execute(f"DELETE FROM {table} WHERE {key} = %s AND {target_key} = %s", (object_id, target_id))  # noqa: S608
        return cur.rowcount > 0
