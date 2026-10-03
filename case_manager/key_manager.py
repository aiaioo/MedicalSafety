"""Holds the encryption key in memory -- it is never read from or written to disk.

``KeyManager`` is a process-wide singleton. At startup it holds no key; an
administrator supplies one (the app's /encryption-key page, or stdin for the
command-line scripts via ``prompt_for_key``) and ``remember_key`` keeps it in
memory for the life of the process. encryptor.py is the only module meant to
fetch it (``get_key``); everything else goes through encryptor's functions.

A key is only accepted after it has decrypted ``key_check.enc`` -- a small,
fixed, committed file holding a known plaintext, encrypted under the real key
-- to exactly that plaintext. A wrong key, or a right key against an altered
file, is rejected and nothing is stored.

Because the key lives only in the running process: restarting loses it, and
with several worker processes only the one that was given it would have it,
so the app runs as a single process (threads for concurrency).

Command line:
    python3 key_manager.py generate   # new random key, printed once; also
                                      # writes key_check.enc if it is absent
"""

import base64
import getpass
import sys
import threading
from pathlib import Path

CHECK_FILE = Path(__file__).resolve().parent / "key_check.enc"
CHECK_PLAINTEXT = b"case_manager encryption key check v1"


class KeyNotAvailable(RuntimeError):
    """No key has been given to the KeyManager yet."""


class InvalidKey(ValueError):
    """The key is malformed, or failed the check against key_check.enc."""


class KeyManager:
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._key = None
                instance._lock = threading.Lock()
                cls._instance = instance
            return cls._instance

    def is_key_in_memory(self):
        return self._key is not None

    def get_key(self):
        """The raw 32-byte key; raises KeyNotAvailable if none is stored."""
        key = self._key
        if key is None:
            raise KeyNotAvailable("The encryption key has not been entered yet")
        return key

    def remember_key(self, key_text, check_file=None, check_plaintext=None):
        """Test ``key_text`` (base64, as printed by ``generate``) against the
        check file and, only if it decrypts to the expected plaintext, store it
        in memory. Raises InvalidKey otherwise, storing nothing. The check
        file/plaintext can be overridden for tests."""
        import encryptor  # here, not at the top: encryptor imports this module

        try:
            key = base64.urlsafe_b64decode((key_text or "").strip())
        except (ValueError, TypeError):
            raise InvalidKey("That is not a valid key") from None
        if len(key) != 32:
            raise InvalidKey("That is not a valid key")
        try:
            blob = Path(check_file or CHECK_FILE).read_bytes()
            plaintext = encryptor.decrypt_bytes(blob, key=key)
        except FileNotFoundError:
            raise InvalidKey("The key check file is missing, so no key can be verified") from None
        except encryptor.InvalidToken:
            raise InvalidKey("That key is wrong") from None
        if plaintext != (CHECK_PLAINTEXT if check_plaintext is None else check_plaintext):
            raise InvalidKey("That key is wrong")
        with self._lock:
            self._key = key


def get_key():
    return KeyManager().get_key()


def prompt_for_key(source="stdin"):
    """For command-line scripts: ask for the key on stdout and read it, then
    remember it (exits with a message if it is rejected). ``source="stdin"``
    reads a line from stdin (piped, or hidden when stdin is a terminal);
    ``"tty"`` always uses the terminal, for scripts whose stdin carries data."""
    if KeyManager().is_key_in_memory():
        return
    print("Enter the encryption key: ", end="", flush=True)
    if source == "tty" or sys.stdin.isatty():
        text = getpass.getpass("")
    else:
        text = sys.stdin.readline()
        print()
    try:
        KeyManager().remember_key(text)
    except InvalidKey as exc:
        sys.exit(f"error: {exc}")


def _generate():
    import encryptor
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = AESGCM.generate_key(256)
    if CHECK_FILE.exists():
        print(f"{CHECK_FILE.name} already exists and is left unchanged; this key will NOT match it.", file=sys.stderr)
    else:
        CHECK_FILE.write_bytes(encryptor.encrypt_bytes(CHECK_PLAINTEXT, key=key))
        print(f"wrote {CHECK_FILE}", file=sys.stderr)
    print(base64.urlsafe_b64encode(key).decode("ascii"))


if __name__ == "__main__":
    commands = {"generate": _generate}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]]()
