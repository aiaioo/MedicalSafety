"""Maps stable, DB-held identifiers to actual binary file locations.

Nothing in the schema (see schema.sql) ever stores a filesystem path or URL --
only the natural-key columns that already exist on a row (document_id,
doc_type, filename, ...). A "storage key" is derived from those columns by
the pure functions below, then handed to a StorageBackend to actually read,
write, delete, or serve the bytes. Swapping backends (e.g. local disk ->
S3) later is then a one-line change (get_storage_backend), never a data
migration, because no stored value has to change.

Today's key layout intentionally mirrors the existing on-disk layout under
documents/ and storage/snippets/, so migrating existing files costs nothing:
they can stay exactly where they are.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Storage keys: pure functions of a row's own natural-key columns.
# ---------------------------------------------------------------------------

def document_storage_key(document_id: str, doc_type: str) -> str:
    """Key for an uploaded source document's bytes (documents.id/doc_type)."""
    return f"documents/{document_id}.{doc_type}"


def snippet_storage_key(document_id: str, doc_type: str, filename: str) -> str:
    """Key for a cropped snippet PNG (snippets.document_id + filename), with
    doc_type folded into the folder name exactly as storage/snippets/ does
    today (<doc_id>__<norm_type>/<filename>)."""
    norm_type = "docx" if doc_type in ("doc", "docx") else "pdf"
    return f"storage/snippets/{document_id}__{norm_type}/{filename}"


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
    (the project's own documents/ and storage/ folders)."""

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

    def copy_in(self, key: str, source_path: Path) -> None:
        """Migration/import helper: register an existing file at `key`
        without reading it into memory first."""
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.resolve() != source_path.resolve():
            shutil.copy2(source_path, dest)


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


# ---------------------------------------------------------------------------
# The single place that decides which backend is active.
# ---------------------------------------------------------------------------

_backend: Optional[StorageBackend] = None


def get_storage_backend() -> StorageBackend:
    """Returns the process-wide storage backend, chosen by env var so
    switching backends never requires a code change at call sites.

    STORAGE_BACKEND=local (default): STORAGE_ROOT (default: the project
    directory containing documents/ and storage/) on local disk.
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
    return _backend
