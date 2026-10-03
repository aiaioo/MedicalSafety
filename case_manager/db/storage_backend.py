"""Maps stable, DB-held identifiers to actual binary file locations.

Nothing in the schema (see schema.sql) ever stores a filesystem path or URL --
only the natural-key columns that already exist on a row (document_id,
doc_type, filename, ...). A "storage key" is derived from those columns by
the pure functions below, then handed to a StorageBackend to actually read,
write, delete, or serve the bytes. Swapping backends (e.g. local disk ->
S3) later is then a one-line change (get_storage_backend), never a data
migration, because no stored value has to change.

Each user's files live under storage/<owner>/ (documents/, snippets/, cache/),
where <owner> is a hash of the email of the user who owns the cause the
document was uploaded for -- see owner_dir_name().
"""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import encryptor


# ---------------------------------------------------------------------------
# Storage keys: pure functions of a row's own natural-key columns.
# ---------------------------------------------------------------------------

def owner_dir_name(email: str) -> str:
    """The per-user folder name: the first 32 hex chars of the SHA-256 of the
    lowercased email. It is computed once, when a document is uploaded, and
    stored on the document's row (documents.storage_owner), so a later change
    of email address never strands files. Each user's value is also kept in
    users.storage_dir, set when the account is created."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:32]


def _owner_root(owner: Optional[str]) -> str:
    # `owner` is None only for a document nobody owns (nothing can reach it);
    # those stay in the flat layout used before files were organised per user.
    return f"storage/{owner}" if owner else "storage"


def document_storage_key(document_id: str, doc_type: str, owner: Optional[str] = None) -> str:
    """Key for an uploaded source document's bytes (documents.id/doc_type),
    under the owner of the cause it was uploaded for."""
    return f"{_owner_root(owner)}/documents/{document_id}.{doc_type}"


def snippet_storage_key(document_id: str, doc_type: str, filename: str, owner: Optional[str] = None) -> str:
    """Key for a cropped snippet PNG (snippets.document_id + filename), with
    doc_type folded into the folder name (<doc_id>__<norm_type>/<filename>),
    beside the document it was cropped from."""
    norm_type = "docx" if doc_type in ("doc", "docx") else "pdf"
    return f"{_owner_root(owner)}/snippets/{document_id}__{norm_type}/{filename}"


def cache_storage_dir(owner: Optional[str] = None) -> str:
    """Folder (relative to the storage root) for a user's docx->pdf render cache."""
    return f"{_owner_root(owner)}/cache"


# ---------------------------------------------------------------------------
# Backend interface + implementations.
# ---------------------------------------------------------------------------

class StorageBackend(ABC):
    """Read/write/delete binary blobs addressed by a storage key (see above).

    A key is a '/'-separated relative path with no leading slash and no
    '..' segments -- backends should treat it as opaque beyond that.
    """

    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    def url_for(self, key: str) -> Optional[str]:
        """A directly-fetchable URL for this key (e.g. a presigned S3 URL),
        or None if callers must instead fetch bytes through this backend
        and serve them themselves (the local backend always returns None --
        Flask serves the bytes via send_file)."""
        return None


def _validate_key(key: str) -> None:
    if not key or key.startswith("/") or ".." in Path(key).parts:
        raise ValueError(f"invalid storage key: {key!r}")


class LocalFilesystemStorage(StorageBackend):
    """Backend used today: files live under a root directory on local disk
    (the project's own storage/ folder)."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _resolve(self, key: str) -> Path:
        _validate_key(key)
        path = (self.root / key).resolve()
        if self.root not in path.parents and path != self.root:
            raise ValueError(f"storage key escapes root: {key!r}")
        return path

    def read_bytes(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def write_bytes(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()


class S3Storage(StorageBackend):
    """Sketch of the eventual object-storage backend -- not wired up yet.

    Swapping to this only requires changing get_storage_backend() below;
    every caller already speaks in storage keys, not paths, so no other
    code (or database row) needs to change.
    """

    def __init__(self, bucket: str, prefix: str = "", client=None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client  # a boto3 S3 client, injected lazily to avoid a hard dependency

    def _object_key(self, key: str) -> str:
        _validate_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def read_bytes(self, key: str) -> bytes:
        obj = self._client.get_object(Bucket=self.bucket, Key=self._object_key(key))
        return obj["Body"].read()

    def write_bytes(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=self._object_key(key), Body=data)

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._object_key(key))

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError  # local import: optional dependency
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._object_key(key))
            return True
        except ClientError:
            return False

    def url_for(self, key: str) -> Optional[str]:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": self._object_key(key)}, ExpiresIn=3600,
        )


class EncryptedStorage(StorageBackend):
    """Wraps another backend so that nothing but ciphertext ever reaches it.

    Bytes are encrypted in memory (encryptor.py) before being handed to the
    inner backend and decrypted in memory on the way back; the inner key gets
    a '.enc' suffix. Callers keep using the plain keys from the functions
    above and never see the difference.
    """

    def __init__(self, inner: StorageBackend):
        self.inner = inner

    @staticmethod
    def _enc_key(key: str) -> str:
        return key + encryptor.SUFFIX

    def read_bytes(self, key: str) -> bytes:
        return encryptor.decrypt_bytes(self.inner.read_bytes(self._enc_key(key)))

    def write_bytes(self, key: str, data: bytes) -> None:
        self.inner.write_bytes(self._enc_key(key), encryptor.encrypt_bytes(data))

    def delete(self, key: str) -> None:
        self.inner.delete(self._enc_key(key))

    def exists(self, key: str) -> bool:
        return self.inner.exists(self._enc_key(key))

    def url_for(self, key: str) -> Optional[str]:
        return None  # a direct URL would serve ciphertext; bytes must come through read_bytes

    @property
    def root(self) -> Path:
        return self.inner.root


# ---------------------------------------------------------------------------
# The single place that decides which backend is active.
# ---------------------------------------------------------------------------

_backend: Optional[StorageBackend] = None


def get_storage_backend() -> StorageBackend:
    """Returns the process-wide storage backend, chosen by env var so
    switching backends never requires a code change at call sites.

    STORAGE_BACKEND=local (default): STORAGE_ROOT (default: the project
    directory containing storage/) on local disk.
    STORAGE_BACKEND=s3: STORAGE_S3_BUCKET (+ optional STORAGE_S3_PREFIX).
    """
    global _backend
    if _backend is not None:
        return _backend

    kind = os.environ.get("STORAGE_BACKEND", "local")
    if kind == "local":
        root = Path(os.environ.get("STORAGE_ROOT", Path(__file__).resolve().parent.parent))
        _backend = LocalFilesystemStorage(root)
    elif kind == "s3":
        import boto3  # local import: optional dependency, only needed for this backend
        bucket = os.environ["STORAGE_S3_BUCKET"]
        prefix = os.environ.get("STORAGE_S3_PREFIX", "")
        _backend = S3Storage(bucket, prefix, client=boto3.client("s3"))
    else:
        raise ValueError(f"unknown STORAGE_BACKEND: {kind!r}")
    _backend = EncryptedStorage(_backend)
    return _backend
