#!/usr/bin/env python3
"""One-off: move existing files to the per-user layout (storage/<owner>/...).

Run after db/migrations/013_document_storage_owner.sql. For every document
with a storage_owner it moves the document's file, its snippet folder and its
docx->pdf cache entry from wherever the older layouts put them
(documents/, storage/documents/, storage/snippets/, storage/cache/) to
storage/<owner>/{documents,snippets,cache}/. Safe to re-run: anything already
in place is skipped. Honours DATABASE_URL and STORAGE_ROOT like the app.

    python db/relocate_files.py [--dry-run]
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage_backend import cache_storage_dir, document_storage_key, snippet_storage_key  # noqa: E402

ROOT = Path(os.environ.get("STORAGE_ROOT", Path(__file__).resolve().parent.parent)).resolve()


def move(src: Path, dest: Path, dry_run: bool) -> None:
    if not src.exists() or dest.exists():
        return
    print(f"  {src.relative_to(ROOT)} -> {dest.relative_to(ROOT)}")
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    conn = psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql:///case_manager"))
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, doc_type, storage_owner FROM documents WHERE storage_owner IS NOT NULL ORDER BY id")
        docs = cur.fetchall()
    for doc_id, doc_type, owner in docs:
        print(doc_id)
        new_doc = ROOT / document_storage_key(doc_id, doc_type, owner)
        for old in (ROOT / "documents" / f"{doc_id}.{doc_type}",
                    ROOT / document_storage_key(doc_id, doc_type)):
            move(old, new_doc, dry_run)
        norm = "docx" if doc_type in ("doc", "docx") else "pdf"
        old_snips = ROOT / "storage" / "snippets" / f"{doc_id}__{norm}"
        new_snips = ROOT / snippet_storage_key(doc_id, doc_type, "x", owner).rsplit("/", 1)[0]
        if old_snips.is_dir() and not new_snips.exists():
            print(f"  {old_snips.relative_to(ROOT)}/ -> {new_snips.relative_to(ROOT)}/")
            if not dry_run:
                new_snips.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_snips), str(new_snips))
        move(ROOT / "storage" / "cache" / f"{doc_id}.pdf", ROOT / cache_storage_dir(owner) / f"{doc_id}.pdf", dry_run)
    print("done" + (" (dry run)" if dry_run else ""))


if __name__ == "__main__":
    main()
