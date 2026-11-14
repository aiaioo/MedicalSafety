"""The pure part of splitting a document: which pages go to which segment, and how references to the old
document's pages and snippet images are rewritten to point at the new ones. No database, no files, no Flask.

A split is described by a "plan" (stored as JSON in document_splits.plan):

    {"page_count": 10,
     "segments": [{"id": "<new document id>", "start": 1, "end": 5, "title": "..."}, ...],
     "snippets": [{"id": ..., "page": 7, "filename": "p7_ab12.png",
                   "segment": 1, "new_page": 2, "new_filename": "p2_ab12.png"}, ...]}

Each split point is the first page of a segment (page 1 is always one), so points [1, 6] split a 10-page
document into pages 1-5 and 6-10.
"""

from __future__ import annotations

import re

MAX_SEGMENTS = 200

_FILENAME_RE = re.compile(r"^p(\d+)_(.+)$")
_PAGE_IMAGE_RE = re.compile(r"^p(\d+)_page\.png$")


def normalize_points(points, page_count: int) -> list[int]:
    """The sorted, de-duplicated split points, always starting with 1. Raises ValueError for a point that is
    not a whole number from 1 to `page_count`, or for more segments than MAX_SEGMENTS."""
    clean = {1}
    for p in points or []:
        if isinstance(p, bool) or not isinstance(p, int):
            raise ValueError("Page numbers must be whole numbers.")
        if p < 1 or p > page_count:
            raise ValueError(f"Page {p} is outside the document (1 to {page_count}).")
        clean.add(p)
    if len(clean) > MAX_SEGMENTS:
        raise ValueError(f"A document can be split into at most {MAX_SEGMENTS} parts.")
    return sorted(clean)


def segment_bounds(points: list[int], page_count: int) -> list[tuple[int, int]]:
    """[(first page, last page), ...] for normalized `points`."""
    return [(s, (points[i + 1] - 1) if i + 1 < len(points) else page_count) for i, s in enumerate(points)]


def locate(segments: list[dict], page: int) -> tuple[int, int] | None:
    """(segment index, page number within that segment) of source `page`, or None if it is outside every segment."""
    for i, seg in enumerate(segments):
        if seg["start"] <= page <= seg["end"]:
            return i, page - seg["start"] + 1
    return None


def moved_snippet_name(filename: str, new_page: int) -> str:
    """A snippet image's filename for its new page. Names are "p<page>_<snippet id>.png"; the page is part of
    the name (references read the page from it), so it changes with the page."""
    m = _FILENAME_RE.match(filename)
    return f"p{new_page}_{m.group(2) if m else filename}"


def build_plan(page_count: int, points: list[int], segment_ids: list[str], titles: list[str], snippets: list[dict]) -> dict:
    """The plan for splitting a `page_count`-page document at normalized `points`; `snippets` are the document's
    snippet rows ({"id", "page", "filename"})."""
    bounds = segment_bounds(points, page_count)
    segments = [{"id": segment_ids[i], "start": s, "end": e, "title": titles[i]} for i, (s, e) in enumerate(bounds)]
    moved = []
    for sn in snippets:
        where = locate(segments, sn["page"])
        if where is None:
            continue  # a snippet on a page the document no longer has: nothing to move it to
        index, new_page = where
        moved.append({"id": sn["id"], "page": sn["page"], "filename": sn["filename"], "segment": index,
                      "new_page": new_page, "new_filename": moved_snippet_name(sn["filename"], new_page)})
    return {"page_count": page_count, "segments": segments, "snippets": moved}


def parse_ranges(text: str) -> list[tuple[int, int]]:
    """[(lo, hi), ...] from a page range like "1-3, 7" (already validated by the caller)."""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        out.append((int(lo), int(hi or lo)))
    return out


def map_page_range(text: str, start: int, end: int) -> str:
    """The pages of range `text` that fall in source pages start..end, renumbered to begin at 1, as a range
    string ("" if none do)."""
    parts = []
    for lo, hi in parse_ranges(text):
        lo, hi = max(lo, start), min(hi, end)
        if lo <= hi:
            a, b = lo - start + 1, hi - start + 1
            parts.append(str(a) if a == b else f"{a}-{b}")
    return ", ".join(parts)


def url_remapper(old_doc_id: str, plan: dict):
    """A function that rewrites every `/media/snippets/<old doc>/<file>` in a string to the new document and
    file -- a snippet image by its recorded move, a whole-page image ("p<N>_page.png") by its page. A file the
    plan knows nothing about is left as it is."""
    by_name = {s["filename"]: s for s in plan["snippets"]}
    segments = plan["segments"]
    pattern = re.compile(r"/media/snippets/" + re.escape(old_doc_id) + r"/([^/?#\s\"'\\]+)")

    def one(m):
        name = m.group(1)
        sn = by_name.get(name)
        if sn is not None:
            return f"/media/snippets/{segments[sn['segment']]['id']}/{sn['new_filename']}"
        pm = _PAGE_IMAGE_RE.match(name)
        where = locate(segments, int(pm.group(1))) if pm else None
        if where is not None:
            index, new_page = where
            return f"/media/snippets/{segments[index]['id']}/p{new_page}_page.png"
        return m.group(0)

    def remap(text: str) -> str:
        return pattern.sub(one, text) if old_doc_id in text else text

    return remap


def remap_json(node, remap):
    """`node` (parsed JSON) with `remap` applied to every string in it. Returns (new node, whether anything changed)."""
    if isinstance(node, str):
        new = remap(node)
        return new, new != node
    if isinstance(node, list):
        changed = False
        out = []
        for item in node:
            new, c = remap_json(item, remap)
            out.append(new)
            changed = changed or c
        return out, changed
    if isinstance(node, dict):
        changed = False
        out = {}
        for k, v in node.items():
            new, c = remap_json(v, remap)
            out[k] = new
            changed = changed or c
        return out, changed
    return node, False
