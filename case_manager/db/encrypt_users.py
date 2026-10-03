#!/usr/bin/env python3
"""One-off: encrypt every user's email and full name in place, fill in
users.email_hash (the lookup key, see storage._email_hash), and drop the old
lower(email) unique index. Run after db/migrations/042_encrypt_user_identity.sql.
Safe to re-run. Honours DATABASE_URL and ENCRYPTION_KEY_FILE like the app.

    python db/encrypt_users.py

Back up .encryption.key first -- the email lookup hashes depend on it too.
Run VACUUM FULL users afterwards so Postgres drops the old plaintext rows
(older database dumps still hold it). Note users.storage_dir (the per-user
storage folder name) is an unsalted hash of the email and is left as is.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import storage  # noqa: E402


def _plain(value: str) -> str:
    """The plaintext of a value that may or may not be encrypted yet."""
    if value and value.startswith(storage._TEXT_PREFIX):
        try:
            return storage._dec_text(value)
        except Exception:  # noqa: BLE001 -- a plaintext value that merely starts with the prefix
            return value
    return value


def main() -> None:
    conn = psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql:///case_manager"))
    done = 0
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, email, full_name, email_hash FROM users ORDER BY id FOR UPDATE")
        for user_id, email, full_name, email_hash in cur.fetchall():
            plain_email, plain_name = _plain(email), _plain(full_name)
            new_hash = storage._email_hash(plain_email)
            new_email = email if _plain(email) != email else storage._enc_text(plain_email)
            new_name = full_name if _plain(full_name) != full_name else storage._enc_text(plain_name)
            if (new_email, new_name, new_hash) == (email, full_name, email_hash):
                continue
            if storage._dec_text(new_email) != plain_email or storage._dec_text(new_name) != plain_name:
                sys.exit(f"verification failed for user {user_id}; nothing changed")
            cur.execute("UPDATE users SET email = %s, full_name = %s, email_hash = %s WHERE id = %s",
                        (new_email, new_name, new_hash, user_id))
            done += 1
        cur.execute("DROP INDEX IF EXISTS users_email_lower_idx")
    print(f"{done} user(s) encrypted")


if __name__ == "__main__":
    main()
