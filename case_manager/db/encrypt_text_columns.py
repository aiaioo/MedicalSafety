#!/usr/bin/env python3
"""One-off: encrypt, in place, the free-text columns of cases, hearings,
case parties, causes, goals, allegations, evidence and "to prove" items.

Each non-empty value becomes 'enc1:<base64 ciphertext>' (storage._enc_text);
empty values and NULLs are left alone. Values that already decrypt are
skipped, so it is safe to re-run. Every value is decrypted again and compared
before the transaction commits. No schema change. Honours DATABASE_URL and
ENCRYPTION_KEY_FILE like the app.

    python db/encrypt_text_columns.py

Back up .encryption.key first. Run VACUUM FULL on the tables afterwards so
Postgres drops the old plaintext rows (older database dumps still hold it).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
from cryptography.fernet import InvalidToken

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import storage  # noqa: E402

TABLES = {
    "cases": ("name", "court", "case_number", "summary", "court_location", "cause_title_doc"),
    "hearings": ("hearing_date", "title", "summary"),
    "case_parties": ("name",),
    "causes": ("title", "description"),
    "goals": ("title", "description"),
    "allegations": ("title", "description"),
    "allegation_evidence": ("text",),
    "allegation_to_prove": ("title", "summary"),
}


def _already_encrypted(value: str) -> bool:
    try:
        storage._dec_text(value)
        return value.startswith(storage._TEXT_PREFIX)
    except (InvalidToken, ValueError, storage.StorageError):
        return False


def main() -> None:
    conn = psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql:///case_manager"))
    counts = {}
    with conn, conn.cursor() as cur:
        for table, columns in TABLES.items():
            cols = ", ".join(columns)
            cur.execute(f"SELECT id, {cols} FROM {table} ORDER BY id FOR UPDATE")  # noqa: S608 (fixed names)
            n = 0
            for row in cur.fetchall():
                updates = {}
                for column, value in zip(columns, row[1:]):
                    if not value or _already_encrypted(value):
                        continue
                    blob = storage._enc_text(value)
                    if storage._dec_text(blob) != value:
                        sys.exit(f"verification failed for {table}.{column} of {row[0]}; nothing changed")
                    updates[column] = blob
                if updates:
                    sets = ", ".join(f"{c} = %s" for c in updates)
                    cur.execute(f"UPDATE {table} SET {sets} WHERE id = %s", (*updates.values(), row[0]))  # noqa: S608
                    n += 1
            counts[table] = n
    print(", ".join(f"{t}: {n}" for t, n in counts.items()) + " row(s) encrypted")


if __name__ == "__main__":
    main()
