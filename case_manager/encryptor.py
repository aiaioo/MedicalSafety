"""Encrypt data to disk and decrypt it back into memory -- never plaintext on disk.

* Writing: plaintext comes from memory (bytes, str, an iterable of byte
  chunks, or a readable binary stream) and only ciphertext reaches disk, in a
  file whose name ends in ``.enc`` (``report.pdf`` -> ``report.pdf.enc``).
* Reading: ciphertext is decrypted straight into memory, as bytes
  (``read_encrypted``), a seekable in-memory stream (``open_encrypted``, e.g.
  for Flask's ``send_file`` or PyMuPDF), or a generator of chunks
  (``iter_decrypted``) for large files.

The key comes from key_manager.py, which holds it in memory only (see there);
without it every function here raises KeyNotAvailable. Losing the key means
losing every ``.enc`` file made with it.

Format: MAGIC | 7-byte random nonce prefix | chunks. Each chunk is
AES-256-GCM over up to CHUNK_SIZE plaintext bytes, with nonce = prefix ||
chunk counter || last-chunk flag. Every chunk is authenticated, and the flag
means reordering, dropping or truncating chunks is detected. Wrong key or
tampering raises ``InvalidToken``. ``iter_decrypted`` yields each chunk only
after it has been verified, but tampering later in the file only surfaces when
the generator reaches it; use ``read_encrypted`` if you need all-or-nothing.

Library use:
    from encryptor import write_encrypted, read_encrypted, open_encrypted
    write_encrypted("notes.txt", b"secret")     # -> notes.txt.enc
    data = read_encrypted("notes.txt.enc")      # bytes
    with open_encrypted("scan.pdf.enc") as f: ...  # io.BytesIO

Command line (stdin/stdout only, so no plaintext file is ever created):
    python3 encryptor.py encrypt OUT < plaintext   # writes OUT.enc
    python3 encryptor.py cat FILE.enc > somewhere  # plaintext to stdout
"""

import argparse
import hashlib
import hmac
import io
import os
import struct
import sys
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import key_manager

SUFFIX = ".enc"
MAGIC = b"MSENC1"
CHUNK_SIZE = 64 * 1024
_TAG = 16
_PREFIX_LEN = 7


def _nonce(prefix, counter, last):
    return prefix + struct.pack(">IB", counter, 1 if last else 0)


def _enc_path(path):
    path = Path(path)
    return path if path.suffix == SUFFIX else path.with_name(path.name + SUFFIX)


def _plain_chunks(data):
    """Yield non-empty byte chunks from bytes, str, a stream or an iterable."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        data = io.BytesIO(data)
    if hasattr(data, "read"):
        while chunk := data.read(CHUNK_SIZE):
            yield chunk
    else:
        for chunk in data:
            if chunk:
                yield chunk


def _fixed_size(chunks):
    """Re-slice arbitrary chunks into CHUNK_SIZE pieces (last may be short)."""
    buf = bytearray()
    for chunk in chunks:
        buf += chunk
        while len(buf) >= CHUNK_SIZE:
            yield bytes(buf[:CHUNK_SIZE])
            del buf[:CHUNK_SIZE]
    if buf:
        yield bytes(buf)


def _encrypt_chunks(data, key):
    """Yield the ciphertext file as byte pieces."""
    aead = AESGCM(key)
    prefix = os.urandom(_PREFIX_LEN)
    yield MAGIC + prefix
    # One-chunk lookahead so the final chunk can be flagged. An empty
    # plaintext still gets one (empty) final chunk.
    counter, pending = 0, b""
    for chunk in _fixed_size(_plain_chunks(data)):
        if counter or pending:
            yield aead.encrypt(_nonce(prefix, counter, False), pending, None)
            counter += 1
        pending = chunk
    yield aead.encrypt(_nonce(prefix, counter, True), pending, None)


def write_encrypted(path, data):
    """Encrypt ``data`` from memory to ``path`` + '.enc'; returns the path.

    ``data`` may be bytes, str (UTF-8), a readable binary stream, or an
    iterable of byte chunks. The write is atomic (temp file + rename).
    """
    dest = _enc_path(path)
    key = key_manager.get_key()
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            for piece in _encrypt_chunks(data, key):
                f.write(piece)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return dest


def blind_index(text):
    """A deterministic keyed hash (hex) of ``text``, for looking up encrypted
    values by equality (e.g. an email address) without storing them in the
    clear. Derived from the encryption key; changes if the key does."""
    sub_key = hmac.new(key_manager.get_key(), b"encryptor blind index v1", hashlib.sha256).digest()
    return hmac.new(sub_key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def encrypt_bytes(data, key=None):
    """Encrypt ``data`` (same input kinds as write_encrypted) to bytes, with
    the remembered key unless ``key`` is given."""
    return b"".join(_encrypt_chunks(data, key or key_manager.get_key()))


def _decrypt_stream(f, key=None):
    aead = AESGCM(key or key_manager.get_key())
    block = CHUNK_SIZE + _TAG
    header = f.read(len(MAGIC) + _PREFIX_LEN)
    if len(header) != len(MAGIC) + _PREFIX_LEN or not header.startswith(MAGIC):
        raise InvalidToken("not an encryptor file")
    prefix = header[len(MAGIC):]
    counter = 0
    cur = f.read(block)
    while True:
        nxt = f.read(block) if len(cur) == block else b""
        last = not nxt
        try:
            yield aead.decrypt(_nonce(prefix, counter, last), cur, None)
        except InvalidTag:
            raise InvalidToken("wrong key or corrupted file") from None
        if last:
            return
        counter, cur = counter + 1, nxt


def encrypt_stream(chunks, key=None):
    """Yield the ciphertext file as byte pieces for an iterable/stream of plaintext
    chunks, holding only a couple of 64 KB chunks in memory at a time."""
    return _encrypt_chunks(chunks, key or key_manager.get_key())


class DecryptedFile(io.RawIOBase):
    """A read-only, seekable view of the plaintext of an encrypted file object.

    The format is independent 64 KB AES-GCM chunks, so any byte range is read by decrypting only the
    chunks that cover it. The whole file is never held in memory or decrypted to disk: at most
    ``cache_chunks`` decrypted chunks are kept, and ``close`` drops them. ``raw`` must be a seekable
    binary file (it is closed with this object).
    """

    def __init__(self, raw, key=None, cache_chunks=4):
        super().__init__()
        self._raw = raw
        self._aead = AESGCM(key or key_manager.get_key())
        self._cache_max = cache_chunks
        self._cache = {}  # chunk index -> plaintext, oldest first
        self._pos = 0
        self._hdr = len(MAGIC) + _PREFIX_LEN
        self._block = CHUNK_SIZE + _TAG
        raw.seek(0)
        header = raw.read(self._hdr)
        if len(header) != self._hdr or not header.startswith(MAGIC):
            raise InvalidToken("not an encryptor file")
        self._prefix = header[len(MAGIC):]
        total = raw.seek(0, io.SEEK_END)
        body = total - self._hdr
        if body < _TAG:
            raise InvalidToken("wrong key or corrupted file")
        self._nblocks = -(-body // self._block)
        self._size = body - _TAG * self._nblocks

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self._pos

    def seek(self, offset, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def _chunk(self, i):
        got = self._cache.get(i)
        if got is not None:
            return got
        self._raw.seek(self._hdr + i * self._block)
        blob = self._raw.read(self._block)
        try:
            plain = self._aead.decrypt(_nonce(self._prefix, i, i == self._nblocks - 1), blob, None)
        except InvalidTag:
            raise InvalidToken("wrong key or corrupted file") from None
        if len(self._cache) >= self._cache_max:
            self._cache.pop(next(iter(self._cache)))
        self._cache[i] = plain
        return plain

    def readinto(self, buf):
        want = min(len(buf), max(0, self._size - self._pos))
        done = 0
        while done < want:
            i, off = divmod(self._pos, CHUNK_SIZE)
            piece = self._chunk(i)[off:off + want - done]
            buf[done:done + len(piece)] = piece
            done += len(piece)
            self._pos += len(piece)
        return done

    def close(self):
        if not self.closed:
            self._cache.clear()
            self._raw.close()
        super().close()


def decrypt_bytes(blob, key=None):
    """Decrypt bytes produced by encrypt_bytes / read from a .enc file, with
    the remembered key unless ``key`` is given."""
    return b"".join(_decrypt_stream(io.BytesIO(blob), key))


def iter_decrypted(path):
    """Generator yielding the decrypted contents of ``path`` chunk by chunk."""
    with open(_enc_path(path), "rb") as f:
        yield from _decrypt_stream(f)


def read_encrypted(path):
    """Return the whole decrypted file as bytes (all-or-nothing)."""
    return b"".join(iter_decrypted(path))


def open_encrypted(path):
    """Return the decrypted file as a seekable in-memory binary stream."""
    return io.BytesIO(read_encrypted(path))


def main(argv=None):
    p = argparse.ArgumentParser(description="Encrypt stdin to disk / decrypt to stdout.")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("encrypt", help="encrypt stdin to OUT.enc")
    e.add_argument("out")
    c = sub.add_parser("cat", help="decrypt FILE.enc to stdout")
    c.add_argument("file")
    args = p.parse_args(argv)
    key_manager.prompt_for_key("tty")  # stdin may carry the data, so ask on the terminal
    try:
        if args.cmd == "encrypt":
            print(write_encrypted(args.out, sys.stdin.buffer))
        else:
            for chunk in iter_decrypted(args.file):
                sys.stdout.buffer.write(chunk)
    except InvalidToken as exc:
        sys.exit(f"error: {exc or 'could not decrypt'}")
    except OSError as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
