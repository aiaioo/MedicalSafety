#!/usr/bin/env python3
"""One-off: encrypt every existing plaintext file under the storage root.

For each file under storage/ that isn't already '*.enc' it writes
'<name>.enc' (encryptor.py format), reads that back
and verifies it, and only then deletes the plaintext original. The docx->pdf
render cache (storage/**/cache/) is deleted instead of converted: it is
regenerated on demand, encrypted. Safe to re-run. Honours STORAGE_ROOT like
the app. The encryption key is read from stdin (a line, or typed hidden at a terminal).

    python db/encrypt_existing_files.py [--dry-run]

Keep a copy of the key -- without it the encrypted files are lost.
Note that deleting a file does not scrub its blocks from the disk; for full
assurance also wipe/replace the underlying volume.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import encryptor  # noqa: E402
import key_manager  # noqa: E402

ROOT = Path(os.environ.get("STORAGE_ROOT", Path(__file__).resolve().parent.parent)).resolve()


def main() -> None:
    key_manager.prompt_for_key()
    dry_run = "--dry-run" in sys.argv
    storage = ROOT / "storage"
    done = cache = skipped = 0
    for path in sorted(p for p in storage.rglob("*") if p.is_file()):
        rel = path.relative_to(ROOT)
        if path.suffix == encryptor.SUFFIX:
            skipped += 1
        elif path.parent.name == "cache":
            print(f"delete cache {rel}")
            cache += 1
            if not dry_run:
                path.unlink()
        else:
            print(f"encrypt {rel}")
            done += 1
            if not dry_run:
                plain = path.read_bytes()
                dest = encryptor.write_encrypted(path, plain)
                if encryptor.read_encrypted(dest) != plain:
                    dest.unlink()
                    sys.exit(f"verification failed for {rel}; original left in place")
                path.unlink()
    print(f"{done} encrypted, {cache} cache files deleted, {skipped} already encrypted"
          + (" (dry run)" if dry_run else ""))


if __name__ == "__main__":
    main()
