"""Runs a document split (see storage.begin_split for the journal and the steps).

The work is done a piece at a time so memory stays small however long the document is:
  - the source PDF is read lazily through its decrypting file object (only the pages being kept are read),
  - each new PDF is written as a stream, encrypted as it goes (storage.split_write_segment), never assembled
    in memory or written to disk unencrypted.
Every step can be repeated safely, which is what makes recovery simple: after a crash, run the job again.

A job runs in a background thread of the server process. If the process dies, the journal row stays "building"
(the document stays locked) or "committed"; resume_unfinished() carries on from there once the encryption key
has been entered again.
"""

from __future__ import annotations

import logging
import threading

import pikepdf

import storage
from pdf_stream import stream_pikepdf

log = logging.getLogger(__name__)

_running: set[str] = set()  # ids of the jobs this process is working on now
_running_lock = threading.Lock()


def is_running(split_id: str) -> bool:
    with _running_lock:
        return split_id in _running


def start(split_id: str) -> bool:
    """Runs the job in a background thread. False if this process is already working on it."""
    with _running_lock:
        if split_id in _running:
            return False
        _running.add(split_id)
    threading.Thread(target=_run, args=(split_id,), name=f"split-{split_id}", daemon=True).start()
    return True


def resume_unfinished() -> int:
    """Restarts every job that was cut short (by a crash or restart). Returns how many were started."""
    started = 0
    for split in storage.list_unfinished_splits():
        if start(split["id"]):
            started += 1
    return started


def _run(split_id: str) -> None:
    try:
        split = storage.get_split(split_id)
        if split is None:
            return
        if split["status"] == "building":
            try:
                _write_files(split)
            except Exception as exc:
                log.exception("Splitting document %s failed; rolling back", split["document_id"])
                _roll_back(split, exc)
                return
            try:
                storage.commit_split(split_id)
            except Exception as exc:
                log.exception("Committing the split of document %s failed; rolling back", split["document_id"])
                _roll_back(split, exc)
                return
            split = storage.get_split(split_id)
        if split and split["status"] == "committed":
            # Only deleting the old snippet images is left; if it fails, the next resume tries again.
            storage.split_remove_old_snippet_images(split)
            storage.finish_split(split_id)
    except Exception:
        log.exception("Split job %s stopped; it will be resumed", split_id)
    finally:
        with _running_lock:
            _running.discard(split_id)


def _roll_back(split: dict, exc: Exception) -> None:
    """Removes what a failed split wrote and unlocks the document. If even that fails, the job stays "building"
    and is retried by resume_unfinished()."""
    try:
        storage.split_discard_new_files(split)
        storage.cancel_split(split["id"], f"{type(exc).__name__}: {exc}")
    except Exception:
        log.exception("Rolling back the split of document %s failed", split["document_id"])


def _write_files(split: dict) -> None:
    segments = split["plan"]["segments"]
    for index, seg in enumerate(segments):
        if storage.split_segment_exists(split, index):
            continue  # written before a crash; a file only ever appears whole
        _write_segment(split, index, seg["start"], seg["end"])
    storage.split_copy_snippet_images(split)
    for index, seg in enumerate(segments):
        _check_segment(split, index, seg["end"] - seg["start"] + 1)


# What a PDF keeps outside its pages that can point at pages: bookmarks, named destinations, form fields, the
# structure tree, reading threads. Left in a part they would drag in the pages that were cut away.
_PAGE_REFERRING_ROOT_KEYS = ("/Outlines", "/Names", "/Dests", "/OpenAction", "/StructTreeRoot", "/Threads", "/AA",
                             "/AcroForm", "/PageLabels")


def _write_segment(split: dict, index: int, first: int, last: int) -> None:
    """Writes pages first..last of the document as part `index`. The source is opened afresh and every other
    page is removed from it before it is saved: qpdf then streams the kept pages out of the (decrypting) source
    as it writes, so memory use doesn't grow with the size of the document or the part. (Copying pages into a
    new document instead reads all their data into memory first.)"""
    src_file = storage.split_open_source(split)
    try:
        with pikepdf.open(src_file) as pdf:
            total = len(pdf.pages)
            if total != split["plan"]["page_count"]:
                raise ValueError(f"The document has {total} pages, not {split['plan']['page_count']}.")
            del pdf.pages[last:]
            del pdf.pages[:first - 1]
            for key in _PAGE_REFERRING_ROOT_KEYS:
                if key in pdf.Root:
                    del pdf.Root[key]
            for page in pdf.pages:
                _drop_internal_links(page)
                if "/B" in page.obj:
                    del page.obj["/B"]
            storage.split_write_segment(split, index, stream_pikepdf(pdf))
    finally:
        src_file.close()


def _drop_internal_links(page) -> None:
    """Removes links that jump to another place in the document: in a part they would point at a page that
    may no longer be there (and keep it alive in the file). Links that open a web address are kept."""
    annots = page.obj.get("/Annots")
    if annots is None:
        return
    keep = [a for a in annots if not _is_internal_link(a)]
    if len(keep) != len(annots):
        if keep:
            page.obj.Annots = pikepdf.Array(keep)
        else:
            del page.obj["/Annots"]


def _is_internal_link(annot) -> bool:
    try:
        if annot.get("/Subtype") != "/Link":
            return False
        if "/Dest" in annot:
            return True
        action = annot.get("/A")
        return action is not None and action.get("/S") in ("/GoTo", "/GoToR", "/Named")
    except Exception:
        return False


def _check_segment(split: dict, index: int, expected_pages: int) -> None:
    f = storage.split_open_segment(split, index)
    try:
        with pikepdf.open(f) as pdf:
            if len(pdf.pages) != expected_pages:
                raise ValueError(f"Part {index + 1} has {len(pdf.pages)} pages, expected {expected_pages}.")
    finally:
        f.close()
