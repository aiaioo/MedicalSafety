#!/usr/bin/env python3
"""One-off migration: load every storage/*.json file (plus the documents/
directory listing) into the PostgreSQL schema defined in db/schema.sql.

This does NOT move, copy, or touch any of the actual PDF/DOCX/PNG bytes --
those stay exactly where they are on disk. It only copies the *metadata*
that currently lives in JSON files into rows, using db/storage_backend.py's
key functions to confirm the paired binary file still exists where the
metadata expects it.

Usage:
    createdb case_manager
    psql case_manager -f db/schema.sql
    DATABASE_URL=postgresql:///case_manager python3 db/migrate_json_to_postgres.py

Safe to re-run: every insert is an upsert (ON CONFLICT DO UPDATE), so running
this again after storage/*.json has changed re-syncs the database rather than
duplicating rows. It never deletes a row that no longer has a matching JSON
file -- run with --prune if you want that (see bottom of file).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage_backend import LocalFilesystemStorage, document_storage_key, snippet_storage_key  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
DOCUMENTS_DIR = BASE_DIR / "documents"
STORAGE_DIR = BASE_DIR / "storage"


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def iter_json_files(dirpath: Path):
    if not dirpath.exists():
        return
    for path in sorted(dirpath.glob("*.json")):
        yield path


# ---------------------------------------------------------------------------
# documents (scanned from documents/, titled from storage/doc_meta/*.json)
# ---------------------------------------------------------------------------

def migrate_documents(cur, local_storage: LocalFilesystemStorage) -> set[str]:
    doc_meta_dir = STORAGE_DIR / "doc_meta"
    docs: dict[str, str] = {}  # id -> doc_type
    for ext, doc_type in ((".pdf", "pdf"), (".docx", "docx"), (".doc", "doc")):
        for path in DOCUMENTS_DIR.glob(f"*{ext}"):
            docs.setdefault(path.stem, doc_type)

    ids: set[str] = set()
    for doc_id, doc_type in sorted(docs.items()):
        key = document_storage_key(doc_id, doc_type)
        if not local_storage.exists(key):
            print(f"  ! skipping document {doc_id!r}: expected file at {key!r} not found", file=sys.stderr)
            continue
        meta = load_json(doc_meta_dir / f"{doc_id}.json", default={}) or {}
        title = meta.get("title") if isinstance(meta, dict) else None
        title = title.strip() if isinstance(title, str) and title.strip() else doc_id
        cur.execute(
            """
            INSERT INTO documents (id, doc_type, title)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET doc_type = EXCLUDED.doc_type, title = EXCLUDED.title
            """,
            (doc_id, doc_type, title),
        )
        ids.add(doc_id)
    print(f"  documents: {len(ids)}")
    return ids


# ---------------------------------------------------------------------------
# causes + goals + goal_cases (goal_cases deferred until cases exist)
# ---------------------------------------------------------------------------

def migrate_causes(cur) -> set[str]:
    ids: set[str] = set()
    for path in iter_json_files(STORAGE_DIR / "causes"):
        if path.stem == "_last_used":
            continue
        data = load_json(path, default=None)
        if not isinstance(data, dict):
            continue
        cause_id = path.stem
        cur.execute(
            """
            INSERT INTO causes (id, title, description, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title, description = EXCLUDED.description,
                created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at
            """,
            (cause_id, data.get("title", ""), data.get("description", ""),
             data.get("created_at"), data.get("updated_at")),
        )
        ids.add(cause_id)
    print(f"  causes: {len(ids)}")
    return ids


def migrate_goals(cur, cause_ids: set[str]):
    goal_case_links = []  # (goal_id, case_id, position) deferred until cases exist
    count = 0
    for path in iter_json_files(STORAGE_DIR / "causes"):
        if path.stem == "_last_used":
            continue
        data = load_json(path, default=None)
        if not isinstance(data, dict) or path.stem not in cause_ids:
            continue
        cause_id = path.stem
        for position, goal in enumerate(data.get("goals") or []):
            if not isinstance(goal, dict):
                continue
            goal_id = goal.get("id")
            if not goal_id:
                continue
            cur.execute(
                """
                INSERT INTO goals (id, cause_id, title, description, position)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    cause_id = EXCLUDED.cause_id, title = EXCLUDED.title,
                    description = EXCLUDED.description, position = EXCLUDED.position
                """,
                (goal_id, cause_id, goal.get("title", ""), goal.get("description", ""), position),
            )
            count += 1
            for case_position, case_id in enumerate(goal.get("case_ids") or []):
                goal_case_links.append((goal_id, case_id, case_position))
    print(f"  goals: {count}")
    return goal_case_links


# ---------------------------------------------------------------------------
# cases + hearings + hearing_documents
# ---------------------------------------------------------------------------

def migrate_cases(cur, cause_ids: set[str], fallback_cause_id: str | None) -> set[str]:
    ids: set[str] = set()
    for path in iter_json_files(STORAGE_DIR / "cases"):
        data = load_json(path, default=None)
        if not isinstance(data, dict):
            continue
        case_id = path.stem
        cause_id = data.get("cause_id")
        if cause_id not in cause_ids:
            cause_id = fallback_cause_id
        if cause_id is None:
            print(f"  ! skipping case {case_id!r}: no valid cause and no fallback cause available", file=sys.stderr)
            continue
        cur.execute(
            """
            INSERT INTO cases (id, cause_id, name, court, case_number, summary, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                cause_id = EXCLUDED.cause_id, name = EXCLUDED.name, court = EXCLUDED.court,
                case_number = EXCLUDED.case_number, summary = EXCLUDED.summary,
                created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at
            """,
            (case_id, cause_id, data.get("name", ""), data.get("court", ""),
             data.get("case_number", ""), data.get("summary", ""),
             data.get("created_at"), data.get("updated_at")),
        )
        ids.add(case_id)
    print(f"  cases: {len(ids)}")
    return ids


def migrate_hearings(cur, case_ids: set[str], document_ids: set[str]):
    hearing_count = 0
    doc_link_count = 0
    for path in iter_json_files(STORAGE_DIR / "cases"):
        data = load_json(path, default=None)
        if not isinstance(data, dict) or path.stem not in case_ids:
            continue
        case_id = path.stem
        for position, hearing in enumerate(data.get("hearings") or []):
            if not isinstance(hearing, dict):
                continue
            hearing_id = hearing.get("id")
            if not hearing_id:
                continue
            cur.execute(
                """
                INSERT INTO hearings (id, case_id, position, hearing_date, title, summary)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    case_id = EXCLUDED.case_id, position = EXCLUDED.position,
                    hearing_date = EXCLUDED.hearing_date, title = EXCLUDED.title, summary = EXCLUDED.summary
                """,
                (hearing_id, case_id, position, hearing.get("date", ""),
                 hearing.get("title", ""), hearing.get("summary", "")),
            )
            hearing_count += 1
            for direction, key in (("submitted", "submitted_docs"), ("received", "received_docs")):
                for doc_position, doc_item in enumerate(hearing.get(key) or []):
                    if not isinstance(doc_item, dict):
                        continue
                    doc_id = doc_item.get("doc_id")
                    if doc_id not in document_ids:
                        continue
                    cur.execute(
                        """
                        INSERT INTO hearing_documents (id, hearing_id, direction, document_id, position)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            hearing_id = EXCLUDED.hearing_id, direction = EXCLUDED.direction,
                            document_id = EXCLUDED.document_id, position = EXCLUDED.position
                        """,
                        (doc_item.get("id"), hearing_id, direction, doc_id, doc_position),
                    )
                    doc_link_count += 1
    print(f"  hearings: {hearing_count} (hearing_documents: {doc_link_count})")


def migrate_goal_cases(cur, goal_case_links, case_ids: set[str]):
    count = 0
    for goal_id, case_id, position in goal_case_links:
        if case_id not in case_ids:
            continue
        cur.execute(
            """
            INSERT INTO goal_cases (goal_id, case_id, position)
            VALUES (%s, %s, %s)
            ON CONFLICT (goal_id, case_id) DO UPDATE SET position = EXCLUDED.position
            """,
            (goal_id, case_id, position),
        )
        count += 1
    print(f"  goal_cases: {count}")


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

def migrate_reports(cur, document_ids: set[str]) -> set[str]:
    ids: set[str] = set()
    for path in iter_json_files(STORAGE_DIR / "reports"):
        data = load_json(path, default=None)
        if not isinstance(data, dict):
            continue
        report_id = path.stem
        source_doc = data.get("source_doc") or None
        if source_doc not in document_ids:
            source_doc = None
        cur.execute(
            """
            INSERT INTO reports (id, name, doc, source_document_id, margins, page_numbers, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name, doc = EXCLUDED.doc, source_document_id = EXCLUDED.source_document_id,
                margins = EXCLUDED.margins, page_numbers = EXCLUDED.page_numbers,
                created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at
            """,
            (report_id, data.get("name", ""),
             psycopg2.extras.Json(data["doc"]) if data.get("doc") is not None else None, source_doc,
             psycopg2.extras.Json(data.get("margins") or {}), psycopg2.extras.Json(data.get("pageNumbers") or {}),
             data.get("created_at"), data.get("updated_at")),
        )
        ids.add(report_id)
    print(f"  reports: {len(ids)}")
    return ids


# ---------------------------------------------------------------------------
# allegations + evidence + to_prove (+ links) + case links
# ---------------------------------------------------------------------------

def migrate_allegations(cur, case_ids: set[str], report_ids: set[str]):
    order_path = STORAGE_DIR / "allegations" / "allegation_order.json"
    order = load_json(order_path, default=[]) or []
    order_index = {aid: i for i, aid in enumerate(order) if isinstance(aid, str)}

    allegation_paths = [
        p for p in iter_json_files(STORAGE_DIR / "allegations") if p.stem != "allegation_order"
    ]
    # Anything not in allegation_order.json still needs a stable position,
    # placed after every explicitly-ordered allegation.
    next_index = len(order_index)
    for path in allegation_paths:
        if path.stem not in order_index:
            order_index[path.stem] = next_index
            next_index += 1

    allegation_count = 0
    evidence_count = 0
    to_prove_count = 0
    to_prove_link_count = 0
    case_link_count = 0

    for path in allegation_paths:
        data = load_json(path, default=None)
        if not isinstance(data, dict):
            continue
        allegation_id = path.stem
        cur.execute(
            """
            INSERT INTO allegations (id, title, description, order_index, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title, description = EXCLUDED.description, order_index = EXCLUDED.order_index,
                created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at
            """,
            (allegation_id, data.get("title", ""), data.get("description", ""),
             order_index[allegation_id], data.get("created_at"), data.get("updated_at")),
        )
        allegation_count += 1

        valid_evidence_ids = set()
        evidence_rows = []  # (id, kind, text, report_id, position)
        for kind in ("inculpatory", "exculpatory"):
            for position, item in enumerate(data.get(kind) or []):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                evidence_id = item["id"]
                valid_evidence_ids.add(evidence_id)
                report_id = item.get("report_id") or None
                if report_id not in report_ids:
                    report_id = None
                evidence_rows.append((evidence_id, kind, item.get("text", ""), report_id, position))

        for evidence_id, kind, text, report_id, position in evidence_rows:
            cur.execute(
                """
                INSERT INTO allegation_evidence (id, allegation_id, kind, text, report_id, position)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    allegation_id = EXCLUDED.allegation_id, kind = EXCLUDED.kind, text = EXCLUDED.text,
                    report_id = EXCLUDED.report_id, position = EXCLUDED.position
                """,
                (evidence_id, allegation_id, kind, text, report_id, position),
            )
            evidence_count += 1

        for position, item in enumerate(data.get("to_prove") or []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            to_prove_id = item["id"]
            cur.execute(
                """
                INSERT INTO allegation_to_prove (id, allegation_id, title, summary, position)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    allegation_id = EXCLUDED.allegation_id, title = EXCLUDED.title,
                    summary = EXCLUDED.summary, position = EXCLUDED.position
                """,
                (to_prove_id, allegation_id, item.get("title", ""), item.get("summary", ""), position),
            )
            to_prove_count += 1
            for link_position, evidence_id in enumerate(item.get("evidence_ids") or []):
                if evidence_id not in valid_evidence_ids:
                    continue
                cur.execute(
                    """
                    INSERT INTO allegation_to_prove_evidence (to_prove_id, evidence_id, position)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (to_prove_id, evidence_id) DO UPDATE SET position = EXCLUDED.position
                    """,
                    (to_prove_id, evidence_id, link_position),
                )
                to_prove_link_count += 1

        for position, case_id in enumerate(data.get("case_ids") or []):
            if case_id not in case_ids:
                continue
            cur.execute(
                """
                INSERT INTO allegation_cases (allegation_id, case_id, position)
                VALUES (%s, %s, %s)
                ON CONFLICT (allegation_id, case_id) DO UPDATE SET position = EXCLUDED.position
                """,
                (allegation_id, case_id, position),
            )
            case_link_count += 1

    print(f"  allegations: {allegation_count} (evidence: {evidence_count}, "
          f"to_prove: {to_prove_count}, to_prove_evidence: {to_prove_link_count}, case links: {case_link_count})")


# ---------------------------------------------------------------------------
# document_annotations + snippets
# ---------------------------------------------------------------------------

def migrate_annotations(cur, document_ids: set[str]):
    count = 0
    for path in iter_json_files(STORAGE_DIR / "annotations"):
        doc_id = path.stem.rsplit("__", 1)[0]
        if doc_id not in document_ids:
            continue
        data = load_json(path, default=None)
        if not isinstance(data, dict):
            continue
        for page_str, shapes in data.items():
            try:
                page_number = int(page_str)
            except (TypeError, ValueError):
                continue
            if not isinstance(shapes, list) or not shapes:
                continue
            cur.execute(
                """
                INSERT INTO document_annotations (document_id, page_number, shapes)
                VALUES (%s, %s, %s)
                ON CONFLICT (document_id, page_number) DO UPDATE SET shapes = EXCLUDED.shapes
                """,
                (doc_id, page_number, psycopg2.extras.Json(shapes)),
            )
            count += 1
    print(f"  document_annotations: {count}")


def migrate_snippets(cur, document_ids: set[str], local_storage: LocalFilesystemStorage):
    count = 0
    for path in iter_json_files(STORAGE_DIR / "snippets"):
        doc_id, _, norm_type = path.stem.rpartition("__")
        if doc_id not in document_ids:
            continue
        items = load_json(path, default=None)
        if not isinstance(items, list):
            continue
        # doc_type for the storage key comes from the documents table, not
        # the filename suffix (which is only ever "pdf" or "docx"/"doc").
        cur.execute("SELECT doc_type FROM documents WHERE id = %s", (doc_id,))
        row = cur.fetchone()
        doc_type = row[0] if row else norm_type
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            filename = item.get("filename")
            rect = item.get("rect") or {}
            key = snippet_storage_key(doc_id, doc_type, filename or "")
            if not filename or not local_storage.exists(key):
                print(f"  ! skipping snippet {item.get('id')!r}: expected file at {key!r} not found", file=sys.stderr)
                continue
            try:
                rect_x, rect_y, rect_w, rect_h = float(rect["x"]), float(rect["y"]), float(rect["w"]), float(rect["h"])
            except (KeyError, TypeError, ValueError):
                continue
            cur.execute(
                """
                INSERT INTO snippets (id, document_id, page_number, filename, rect_x, rect_y, rect_w, rect_h,
                                       annotated, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    document_id = EXCLUDED.document_id, page_number = EXCLUDED.page_number,
                    filename = EXCLUDED.filename, rect_x = EXCLUDED.rect_x, rect_y = EXCLUDED.rect_y,
                    rect_w = EXCLUDED.rect_w, rect_h = EXCLUDED.rect_h,
                    annotated = EXCLUDED.annotated, created_at = EXCLUDED.created_at
                """,
                (item["id"], doc_id, item.get("page", 1), filename, rect_x, rect_y, rect_w, rect_h,
                 bool(item.get("annotated", False)), item.get("created_at")),
            )
            count += 1
    print(f"  snippets: {count}")


# ---------------------------------------------------------------------------
# app_settings
# ---------------------------------------------------------------------------

def migrate_app_settings(cur, cause_ids: set[str]) -> str | None:
    last_used = load_json(STORAGE_DIR / "causes" / "_last_used.json", default=None)
    if not isinstance(last_used, str) or last_used not in cause_ids:
        last_used = None
    cur.execute(
        """
        UPDATE app_settings SET last_used_cause_id = %s WHERE id = TRUE
        """,
        (last_used,),
    )
    print(f"  app_settings.last_used_cause_id = {last_used!r}")
    return last_used


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--database-url", default=os.environ.get("DATABASE_URL"),
        help="postgresql:// connection string (defaults to $DATABASE_URL)",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("provide --database-url or set $DATABASE_URL")

    local_storage = LocalFilesystemStorage(BASE_DIR)

    conn = psycopg2.connect(args.database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                print("documents ...")
                document_ids = migrate_documents(cur, local_storage)

                print("causes ...")
                cause_ids = migrate_causes(cur)

                print("cases ...")
                fallback_cause_id = sorted(cause_ids)[0] if cause_ids else None
                case_ids = migrate_cases(cur, cause_ids, fallback_cause_id)

                print("hearings ...")
                migrate_hearings(cur, case_ids, document_ids)

                print("goals ...")
                goal_case_links = migrate_goals(cur, cause_ids)
                migrate_goal_cases(cur, goal_case_links, case_ids)

                print("reports ...")
                report_ids = migrate_reports(cur, document_ids)

                print("allegations ...")
                migrate_allegations(cur, case_ids, report_ids)

                print("document_annotations ...")
                migrate_annotations(cur, document_ids)

                print("snippets ...")
                migrate_snippets(cur, document_ids, local_storage)

                print("app_settings ...")
                migrate_app_settings(cur, cause_ids)
        print("done.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
