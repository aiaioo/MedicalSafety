#!/usr/bin/env python3
"""One-off: encrypt every report image in place (report_images.data).

Rows already holding encryptor.py output (they start with its magic bytes; a
JPEG never does) are skipped, so it is safe to re-run. Each image is
decrypted again and compared before the row is committed. No schema change.
Honours DATABASE_URL like the app. The encryption key is read from stdin
(a line, or typed hidden at a terminal).

    python db/encrypt_report_images.py

Keep a copy of the key. Run VACUUM FULL report_images afterwards so
Postgres drops the old plaintext rows (older database dumps still hold it).
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
        cur.execute("SELECT id, data FROM report_images ORDER BY id FOR UPDATE")
        for image_id, data in cur.fetchall():
            data = bytes(data)
            if data.startswith(encryptor.MAGIC):
                skipped += 1
                continue
            blob = encryptor.encrypt_bytes(data)
            if encryptor.decrypt_bytes(blob) != data:
                sys.exit(f"verification failed for image {image_id}; nothing changed")
            cur.execute("UPDATE report_images SET data = %s WHERE id = %s", (blob, image_id))
            done += 1
    print(f"{done} image(s) encrypted, {skipped} already encrypted")


if __name__ == "__main__":
    main()
