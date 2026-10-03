#!/usr/bin/env python3
"""One-off: move every page's annotation shapes from document_annotations.shapes
(plaintext JSONB) into shapes_enc (encrypted, encryptor.py) and empty shapes.
Rows holding no shapes at all are deleted (a page with no annotations has no
row). Run after db/migrations/041_encrypt_annotations.sql. Each row is
decrypted again and compared before it is committed. Safe to re-run: rows
that already have shapes_enc are skipped. Honours DATABASE_URL like the app. The encryption key is read from stdin
(a line, or typed hidden at a terminal).

    python db/encrypt_annotations.py

Keep a copy of the key. Run VACUUM FULL document_annotations
afterwards so Postgres drops the old plaintext rows (older database dumps
still hold it).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import key_manager  # noqa: E402
import storage  # noqa: E402


def main() -> None:
    key_manager.prompt_for_key()
    conn = psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql:///case_manager"))
    done = emptied = 0
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, shapes FROM document_annotations WHERE shapes_enc IS NULL ORDER BY id FOR UPDATE")
        for row_id, shapes in cur.fetchall():
            if not shapes:
                cur.execute("DELETE FROM document_annotations WHERE id = %s", (row_id,))
                emptied += 1
                continue
            blob = storage._encrypt_shapes(shapes)
            if storage._decrypt_shapes(blob) != shapes:
                sys.exit(f"verification failed for annotation row {row_id}; nothing changed")
            cur.execute("UPDATE document_annotations SET shapes_enc = %s, shapes = '[]' WHERE id = %s", (blob, row_id))
            done += 1
    print(f"{done} annotation row(s) encrypted, {emptied} empty row(s) removed")


if __name__ == "__main__":
    main()
