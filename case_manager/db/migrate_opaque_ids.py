#!/usr/bin/env python3
"""One-off: give every existing document, report, case, cause and cause-title template an opaque
random id (32 hex chars), so URLs no longer carry names ("jane-doe-discharge-summary-a1b2c3").
New items already get such ids (app.py new_object_id); this converts the old ones.

What it changes, all in one database transaction:
  * the primary key of documents / reports / causes / cases / cause_title_templates;
  * every foreign key that points at them (the constraints are dropped, the values rewritten, and the
    constraints re-created -- which re-validates every row);
  * the ids kept as plain text with no foreign key: key_redemptions.object_id, key_child_access.object_id,
    cause_activity.resource_id, document_splits.document_id and its plan, cases.cause_title_template_id;
  * URLs inside each report's encrypted JSON (/media/snippets/<doc>/..., /media/report-images/<report>/...,
    ?doc=<doc>, ?report=<report>, ?case=<case> ...) and reports.thumbnail_url.
and, before the transaction, on disk: each document's file, snippet folder and docx->pdf cache entry
are renamed (and renamed back if the transaction fails).

Old bookmarks stop working. The old->new mapping is written to a JSON file (--map-file) so nothing is lost.

STOP THE APP FIRST (and restart it afterwards: it caches opened PDFs by id), and take a database dump
and a copy of storage/ before running. Honours DATABASE_URL and STORAGE_ROOT like the app. The encryption
key is read from stdin (a line, or typed hidden at a terminal).

    python db/migrate_opaque_ids.py --dry-run          # report what would change, change nothing
    python db/migrate_opaque_ids.py                    # do it

Safe to re-run: ids that are already opaque are left alone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import encryptor  # noqa: E402
import key_manager  # noqa: E402
import storage  # noqa: E402
from storage_backend import cache_storage_dir, document_storage_key, snippet_storage_key  # noqa: E402

ROOT = Path(os.environ.get("STORAGE_ROOT", HERE.parent)).resolve()
OPAQUE = re.compile(r"^[0-9a-f]{32}$")

# kind -> table whose primary key is renamed
TABLES = {
    "document": "documents",
    "report": "reports",
    "cause": "causes",
    "case": "cases",
    "template": "cause_title_templates",
}
# The cause_activity.resource_kind / key_redemptions.kind names for each kind
KIND_NAMES = {"document": "source", "report": "report", "cause": "cause", "case": "case"}

# URL shapes holding an id in report JSON / plain text. Group 1 = prefix, group 2 = the id.
_ID = r"([A-Za-z0-9_-]+)"
URL_PATTERNS = {
    "document": [r"(/media/snippets/)" + _ID, r"([?&]doc=)" + _ID, r"(/api/doc/)" + _ID],
    "report": [r"(/media/report-images/)" + _ID, r"([?&]report=)" + _ID, r"([?&]annexure=)" + _ID,
               r"(/api/report/)" + _ID],
    "case": [r"([?&]case=)" + _ID],
    "cause": [r"([?&]cause=)" + _ID],
}


def new_id(taken: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex
        if candidate not in taken:
            taken.add(candidate)
            return candidate


def rewrite_urls(text: str, mapping: dict[str, dict[str, str]]) -> str:
    """Swap every old id for its new one, but only where it sits in a URL position, so ordinary words in
    a report that happen to equal an old id (a document once called "summary") are never touched."""
    for kind, patterns in URL_PATTERNS.items():
        ids = mapping[kind]
        for pattern in patterns:
            text = re.sub(pattern, lambda m, ids=ids: m.group(1) + ids.get(m.group(2), m.group(2)), text)
    return text


def rewrite_json(value, mapping):
    """rewrite_urls applied to every string inside a JSON value."""
    if isinstance(value, str):
        return rewrite_urls(value, mapping)
    if isinstance(value, list):
        return [rewrite_json(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: rewrite_json(v, mapping) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def file_moves(cur, doc_map: dict[str, str]) -> list[tuple[Path, Path]]:
    """(src, dest) for every file or folder on disk that is named after a document id."""
    cur.execute("SELECT id, doc_type, storage_owner FROM documents")
    moves = []
    for doc_id, doc_type, owner in cur.fetchall():
        new = doc_map.get(doc_id)
        if new is None:
            continue
        norm = "docx" if doc_type in ("doc", "docx") else "pdf"
        pairs = [
            (document_storage_key(doc_id, doc_type, owner) + encryptor.SUFFIX,
             document_storage_key(new, doc_type, owner) + encryptor.SUFFIX),
            (snippet_storage_key(doc_id, doc_type, "x", owner).rsplit("/", 1)[0],
             snippet_storage_key(new, doc_type, "x", owner).rsplit("/", 1)[0]),
            (f"{cache_storage_dir(owner)}/{doc_id}.pdf{encryptor.SUFFIX}",
             f"{cache_storage_dir(owner)}/{new}.pdf{encryptor.SUFFIX}"),
        ]
        for src, dest in pairs:
            if (ROOT / src).exists():
                moves.append((ROOT / src, ROOT / dest))
    return moves


def do_moves(moves):
    done = []
    try:
        for src, dest in moves:
            if dest.exists():
                raise RuntimeError(f"{dest} already exists; refusing to overwrite")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            done.append((src, dest))
    except BaseException:
        undo_moves(done)
        raise
    return done


def undo_moves(done):
    for src, dest in reversed(done):
        if dest.exists() and not src.exists():
            shutil.move(str(dest), str(src))


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def foreign_keys(cur, tables):
    """Every FK constraint pointing at one of `tables`: (constraint, referencing table, [columns], definition,
    referenced table)."""
    cur.execute(
        """
        SELECT c.conname, c.conrelid::regclass::text, c.confrelid::regclass::text,
               pg_get_constraintdef(c.oid),
               ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(n, ord)
                     JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n ORDER BY k.ord)
        FROM pg_constraint c
        WHERE c.contype = 'f' AND c.confrelid::regclass::text = ANY(%s)
        """,
        (list(tables),),
    )
    return cur.fetchall()


def remap_column(cur, table, column, mapping_table, extra_where=""):
    cur.execute(
        f'UPDATE "{table}" t SET "{column}" = m.new FROM {mapping_table} m WHERE t."{column}" = m.old {extra_where}'
    )
    return cur.rowcount


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show what would change; change nothing")
    ap.add_argument("--map-file", help="where to write the old->new id mapping (default: id_map_<time>.json here)")
    args = ap.parse_args()

    if not args.dry_run:  # the key is only needed to rewrite the encrypted report content
        key_manager.prompt_for_key()
    conn = psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql:///case_manager"))
    conn.autocommit = False
    cur = conn.cursor()

    # --- plan ---------------------------------------------------------------------------------
    mapping: dict[str, dict[str, str]] = {}
    taken: set[str] = set()
    for kind, table in TABLES.items():
        cur.execute(f"SELECT id FROM {table}")
        ids = [r[0] for r in cur.fetchall()]
        taken.update(ids)
        mapping[kind] = {}
    for kind, table in TABLES.items():
        cur.execute(f"SELECT id FROM {table} ORDER BY id")
        for (old,) in cur.fetchall():
            if not OPAQUE.match(old):
                mapping[kind][old] = new_id(taken)
    for kind, m in mapping.items():
        print(f"{kind:9s}: {len(m)} to rename")
    if not any(mapping.values()):
        print("nothing to do")
        return

    map_file = Path(args.map_file) if args.map_file else HERE / f"id_map_{datetime.now():%Y%m%d_%H%M%S}.json"
    moves = file_moves(cur, mapping["document"])
    print(f"{len(moves)} file/folder rename(s) on disk")
    fks = foreign_keys(cur, TABLES.values())
    print(f"{len(fks)} foreign key(s) to rewrite")
    if args.dry_run:
        for src, dest in moves[:10]:
            print(f"  {src.relative_to(ROOT)} -> {dest.relative_to(ROOT)}")
        print("dry run: nothing changed")
        return

    map_file.write_text(json.dumps(mapping, indent=1))
    map_file.chmod(0o600)
    print(f"mapping written to {map_file} (it links old ids to new ones: keep it private)")

    done_moves = []
    try:
        done_moves = do_moves(moves)
        migrate(cur, mapping, fks)
        verify(cur, mapping)
        conn.commit()
    except BaseException:
        conn.rollback()
        undo_moves(done_moves)
        print("FAILED: database rolled back and files moved back; nothing changed", file=sys.stderr)
        raise
    print("done. Restart the app.")


def migrate(cur, mapping, fks):
    # Temp mapping tables, one per kind, plus one for each family of ids that share a column.
    for kind in TABLES:
        cur.execute(f"CREATE TEMP TABLE map_{kind} (old TEXT PRIMARY KEY, new TEXT NOT NULL UNIQUE) ON COMMIT DROP")
        psycopg2.extras.execute_values(
            cur, f"INSERT INTO map_{kind} (old, new) VALUES %s", list(mapping[kind].items()))

    # 1. Drop every FK onto the renamed tables, remembering how to put them back.
    for name, table, _, definition, _cols in fks:
        cur.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')

    # 2. Primary keys, then every referencing column.
    table_kind = {t: k for k, t in TABLES.items()}
    for kind, table in TABLES.items():
        remap_column(cur, table, "id", f"map_{kind}")
    for name, table, ref_table, definition, cols in fks:
        kind = table_kind[ref_table.split(".")[-1]]
        for col in cols:
            remap_column(cur, table.split(".")[-1], col, f"map_{kind}")

    # 3. Ids stored as plain text with no foreign key.
    for kind, label in KIND_NAMES.items():
        remap_column(cur, "key_redemptions", "object_id", f"map_{kind}", f"AND t.kind = '{label}'")
        remap_column(cur, "cause_activity", "resource_id", f"map_{kind}", f"AND t.resource_kind = '{label}'")
    remap_column(cur, "key_child_access", "object_id", "map_report", "AND t.kind = 'report'")
    remap_column(cur, "document_splits", "document_id", "map_document")
    remap_column(cur, "cases", "cause_title_template_id", "map_template")
    cur.execute("SELECT id, plan FROM document_splits")
    for split_id, plan in cur.fetchall():
        new_plan = rewrite_plan(plan, mapping["document"])
        if new_plan != plan:
            cur.execute("UPDATE document_splits SET plan = %s WHERE id = %s", (psycopg2.extras.Json(new_plan), split_id))

    # 4. URLs inside report content.
    cur.execute("SELECT id, doc_enc, thumbnail_url FROM reports")
    for report_id, blob, thumb in cur.fetchall():
        sets, args = [], []
        if blob is not None:
            doc = storage._decrypt_report_doc(blob)
            new_doc = rewrite_json(doc, mapping)
            if new_doc != doc:
                sets.append("doc_enc = %s")
                args.append(storage.encrypt_report_doc(new_doc))
        if thumb:
            new_thumb = rewrite_urls(thumb, mapping)
            if new_thumb != thumb:
                sets.append("thumbnail_url = %s")
                args.append(new_thumb)
        if sets:
            cur.execute(f"UPDATE reports SET {', '.join(sets)} WHERE id = %s", (*args, report_id))

    # 5. Put the foreign keys back (this re-checks every row against the new ids).
    for name, table, _, definition, _cols in fks:
        cur.execute(f'ALTER TABLE {table} ADD CONSTRAINT "{name}" {definition}')


def rewrite_plan(plan, doc_map):
    """A split plan's segment ids are document ids; the rest of it (page numbers, snippet file names) is not."""
    plan = json.loads(json.dumps(plan))
    for seg in plan.get("segments", []):
        if seg.get("id") in doc_map:
            seg["id"] = doc_map[seg["id"]]
    return plan


def verify(cur, mapping):
    """Nothing may still carry an old id."""
    problems = []
    for kind, table in TABLES.items():
        cur.execute(f"SELECT id FROM {table}")
        left = [r[0] for r in cur.fetchall() if not OPAQUE.match(r[0])]
        if left:
            problems.append(f"{table}: ids not converted: {left[:3]}")
    for kind, patterns in URL_PATTERNS.items():
        olds = set(mapping[kind])
        cur.execute("SELECT id, doc_enc FROM reports WHERE doc_enc IS NOT NULL")
        for report_id, blob in cur.fetchall():
            text = json.dumps(storage._decrypt_report_doc(blob), ensure_ascii=False)
            for pattern in patterns:
                for m in re.finditer(pattern, text):
                    if m.group(2) in olds:
                        problems.append(f"report {report_id}: still references old {kind} id {m.group(2)}")
    cur.execute("SELECT id FROM documents")
    docs = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT document_id FROM document_splits")
    missing = [r[0] for r in cur.fetchall() if r[0] not in docs]
    if missing:
        problems.append(f"document_splits point at missing documents: {missing[:3]}")
    if problems:
        raise RuntimeError("verification failed:\n  " + "\n  ".join(problems))
    print("verified: no old ids remain")


if __name__ == "__main__":
    main()
