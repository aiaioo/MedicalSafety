#!/usr/bin/env python3
"""One-off: encrypt every user's profile photo in place (users.photo).

Photos already holding encryptor.py output (they start with its magic bytes; a
JPEG never does) are skipped, so it is safe to re-run. Each photo is decrypted
again and compared before the row is committed. No schema change. Honours
DATABASE_URL like the app. The encryption key is read from stdin (a line, or
typed hidden at a terminal).

    python db/encrypt_user_photos.py

Run VACUUM FULL users afterwards so Postgres drops the old plaintext rows
(older database dumps still hold it).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import encryptor  # noqa: E402
import key_manager  # noqa: E402


def main() -> None:
    key_manager.prompt_for_key()
    conn = psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql:///case_manager"))
    done = skipped = 0
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, photo FROM users WHERE photo IS NOT NULL ORDER BY id FOR UPDATE")
        for user_id, photo in cur.fetchall():
            photo = bytes(photo)
            if photo.startswith(encryptor.MAGIC):
                skipped += 1
                continue
            blob = encryptor.encrypt_bytes(photo)
            if encryptor.decrypt_bytes(blob) != photo:
                sys.exit(f"verification failed for user {user_id}'s photo; nothing changed")
            cur.execute("UPDATE users SET photo = %s WHERE id = %s", (blob, user_id))
            done += 1
    print(f"{done} photo(s) encrypted, {skipped} already encrypted")


if __name__ == "__main__":
    main()
