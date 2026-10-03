"""The one place that knows where the encryption key lives and reads it.

The key is a 256-bit AES key, stored base64-encoded in ``.encryption.key``
next to this module (git-ignored; override the location with the
``ENCRYPTION_KEY_FILE`` environment variable). It is generated on first use
with owner-only (0600) permissions. Losing it means losing everything
encrypted with it, so back it up.

Only encryptor.py is meant to call ``load_key``; everything else in the app
goes through encryptor's functions and never handles the key.
"""

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEFAULT_KEY_FILE = Path(__file__).resolve().parent / ".encryption.key"


def key_path():
    return Path(os.environ.get("ENCRYPTION_KEY_FILE") or DEFAULT_KEY_FILE)


def load_key(create):
    """Return the raw 32-byte key. With ``create``, generate the key file if
    it doesn't exist yet; otherwise a missing file raises FileNotFoundError."""
    path = key_path()
    if path.exists():
        return base64.urlsafe_b64decode(path.read_bytes().strip())
    if not create:
        raise FileNotFoundError(f"Encryption key file not found: {path}")
    key = AESGCM.generate_key(256)
    # O_EXCL + 0600 so the key is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(base64.urlsafe_b64encode(key) + b"\n")
    return key
