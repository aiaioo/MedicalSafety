#!/usr/bin/env python3
"""One-off: move every report's text from reports.doc (plaintext JSONB) into
reports.doc_enc (encrypted, encryptor.py), fill in reports.thumbnail_url, and
NULL reports.doc. Run after db/migrations/040_encrypt_report_docs.sql. Each
row is decrypted again and compared before it is committed. Safe to re-run:
rows whose doc is already NULL are skipped. Honours DATABASE_URL like the app. The encryption key is read from stdin
(a line, or typed hidden at a terminal).

    python db/encrypt_report_docs.py

Keep a copy of the key. Note that Postgres may keep old plaintext in
dead tuples/WAL until vacuumed and rewritten; run VACUUM FULL reports for
full assurance (and older database dumps still hold plaintext).
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
    done = 0
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, doc FROM reports WHERE doc IS NOT NULL ORDER BY id FOR UPDATE")
        for report_id, doc in cur.fetchall():
            if doc is None or not isinstance(doc, dict):
                continue
            blob = storage.encrypt_report_doc(doc)
            if storage._decrypt_report_doc(blob) != doc:
                sys.exit(f"verification failed for report {report_id}; nothing changed")
            cur.execute(
                "UPDATE reports SET doc_enc = %s, thumbnail_url = %s, doc = NULL WHERE id = %s",
                (blob, storage._first_image_src(doc), report_id),
            )
            done += 1
    print(f"{done} report(s) encrypted")


if __name__ == "__main__":
    main()
