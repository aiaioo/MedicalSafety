import base64
import io
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import fitz  # PyMuPDF
from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.image.image import Image as DocxImage
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Pt, RGBColor
from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory, url_for

from converters import ConversionError, convert_to_pdf

BASE_DIR = Path(__file__).resolve().parent
DOCUMENTS_DIR = BASE_DIR / "documents"
STORAGE_DIR = BASE_DIR / "storage"
CACHE_DIR = STORAGE_DIR / "cache"
ANNOTATIONS_DIR = STORAGE_DIR / "annotations"
SNIPPETS_DIR = STORAGE_DIR / "snippets"
REPORTS_DIR = STORAGE_DIR / "reports"
CASES_DIR = STORAGE_DIR / "cases"
ALLEGATIONS_DIR = STORAGE_DIR / "allegations"
DOC_META_DIR = STORAGE_DIR / "doc_meta"

for d in (DOCUMENTS_DIR, CACHE_DIR, ANNOTATIONS_DIR, SNIPPETS_DIR, REPORTS_DIR, CASES_DIR, ALLEGATIONS_DIR, DOC_META_DIR):
    d.mkdir(parents=True, exist_ok=True)

DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_ANNOTATION_COLOR = "#e02424"

# Report page margins, in points — left/right/header/footer. Defaults match the
# geometry render_report_pdf has always used (fitz mediabox inset of
# (36, 46, -36, -46)pt), so existing reports render unchanged until a user
# explicitly opens Page setup and changes them.
REPORT_DEFAULT_MARGINS = {"left": 36, "right": 36, "header": 46, "footer": 46}
REPORT_MARGIN_MIN = 0
REPORT_MARGIN_MAX = 200  # pt; keeps left+right and header+footer well under A4's 595x842pt


def sanitize_margins(raw, fallback=None):
    fallback = fallback if isinstance(fallback, dict) else REPORT_DEFAULT_MARGINS
    result = {}
    for key, default in REPORT_DEFAULT_MARGINS.items():
        val = raw.get(key) if isinstance(raw, dict) else None
        try:
            val = float(val)
        except (TypeError, ValueError):
            val = None
        if val is None or not (REPORT_MARGIN_MIN <= val <= REPORT_MARGIN_MAX):
            val = fallback.get(key, default)
        result[key] = val
    return result


# Report page-number settings: where to print them (or "none"), how many
# leading pages to leave unnumbered (e.g. a cover page), and the font/size to
# draw them in. Font names mirror the #pageNumberFontInput <option> values in
# reports.html (quoted where the CSS family name has a space, so the same
# string can be dropped straight into a font-family declaration).
REPORT_DEFAULT_PAGE_NUMBERS = {"position": "top-center", "skip": 0, "font": "Arial", "fontSize": 11}
REPORT_PAGE_NUMBER_POSITIONS = {
    "top-left", "top-center", "top-right",
    "bottom-left", "bottom-center", "bottom-right",
    "none",
}
REPORT_PAGE_NUMBER_FONTS = {
    "Arial", "Georgia", "'Times New Roman'", "'Courier New'",
    "Verdana", "'Trebuchet MS'", "'Comic Sans MS'",
}
REPORT_PAGE_NUMBER_SKIP_MAX = 50
REPORT_PAGE_NUMBER_FONT_SIZE_MIN = 6
REPORT_PAGE_NUMBER_FONT_SIZE_MAX = 72


def sanitize_page_numbers(raw, fallback=None):
    fallback = fallback if isinstance(fallback, dict) else REPORT_DEFAULT_PAGE_NUMBERS

    position = raw.get("position") if isinstance(raw, dict) else None
    if position not in REPORT_PAGE_NUMBER_POSITIONS:
        position = fallback.get("position")
        if position not in REPORT_PAGE_NUMBER_POSITIONS:
            position = REPORT_DEFAULT_PAGE_NUMBERS["position"]

    skip = raw.get("skip") if isinstance(raw, dict) else None
    try:
        skip = int(skip)
    except (TypeError, ValueError):
        skip = None
    if skip is None or not (0 <= skip <= REPORT_PAGE_NUMBER_SKIP_MAX):
        skip = fallback.get("skip")
        if not isinstance(skip, int) or not (0 <= skip <= REPORT_PAGE_NUMBER_SKIP_MAX):
            skip = REPORT_DEFAULT_PAGE_NUMBERS["skip"]

    font = raw.get("font") if isinstance(raw, dict) else None
    if font not in REPORT_PAGE_NUMBER_FONTS:
        font = fallback.get("font")
        if font not in REPORT_PAGE_NUMBER_FONTS:
            font = REPORT_DEFAULT_PAGE_NUMBERS["font"]

    font_size = raw.get("fontSize") if isinstance(raw, dict) else None
    try:
        font_size = float(font_size)
    except (TypeError, ValueError):
        font_size = None
    if font_size is None or not (REPORT_PAGE_NUMBER_FONT_SIZE_MIN <= font_size <= REPORT_PAGE_NUMBER_FONT_SIZE_MAX):
        font_size = fallback.get("fontSize")
        if not isinstance(font_size, (int, float)) or not (REPORT_PAGE_NUMBER_FONT_SIZE_MIN <= font_size <= REPORT_PAGE_NUMBER_FONT_SIZE_MAX):
            font_size = REPORT_DEFAULT_PAGE_NUMBERS["fontSize"]

    return {"position": position, "skip": skip, "font": font, "fontSize": font_size}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB, generous for scanned case files

UPLOAD_EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".doc": "docx"}


class DocumentError(Exception):
    def __init__(self, message, status=404):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(DocumentError)
def handle_document_error(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": err.message}), err.status
    return render_template("error.html", message=err.message), err.status


# ---------------------------------------------------------------------------
# Document resolution helpers
# ---------------------------------------------------------------------------

def normalize_type(raw_type):
    t = (raw_type or "pdf").strip().lower()
    if t == "pdf":
        return "pdf"
    if t in ("docx", "doc", "word"):
        return "docx"
    raise DocumentError(f"Unsupported document type: {raw_type!r}", 400)


def check_doc_id(doc_id):
    if not doc_id or not DOC_ID_RE.match(doc_id):
        raise DocumentError(f"Invalid document id: {doc_id!r}", 400)


def check_report_id(report_id):
    if not report_id or not DOC_ID_RE.match(report_id):
        raise DocumentError(f"Invalid report id: {report_id!r}", 400)


def list_source_docs():
    """All uploaded source documents (pdf/docx/doc) in documents/, as
    {"id", "type", "title"} dicts -- the same shape rendered into the
    annotations and reports pages' source pickers."""
    pdf_ids = {f.stem for f in DOCUMENTS_DIR.glob("*.pdf")}
    docx_ids = {f.stem for f in DOCUMENTS_DIR.glob("*.docx")} | {f.stem for f in DOCUMENTS_DIR.glob("*.doc")}
    docs = [{"id": i, "type": "pdf"} for i in sorted(pdf_ids)]
    docs += [{"id": i, "type": "docx"} for i in sorted(docx_ids - pdf_ids)]
    docs.sort(key=lambda d: d["id"])
    for d in docs:
        d["title"] = get_doc_title(d["id"])
    return docs


def resolve_pdf_path(doc_id, raw_type):
    check_doc_id(doc_id)
    norm_type = normalize_type(raw_type)

    if norm_type == "pdf":
        path = DOCUMENTS_DIR / f"{doc_id}.pdf"
        if not path.exists():
            raise DocumentError(f"No PDF found for doc '{doc_id}' (expected {path.name} in documents/)")
        return path, norm_type

    src = None
    for ext in (".docx", ".doc"):
        candidate = DOCUMENTS_DIR / f"{doc_id}{ext}"
        if candidate.exists():
            src = candidate
            break
    if src is None:
        raise DocumentError(f"No Word document found for doc '{doc_id}' (expected .docx or .doc in documents/)")

    cached = CACHE_DIR / f"{doc_id}.pdf"
    if not cached.exists() or src.stat().st_mtime > cached.stat().st_mtime:
        try:
            converted = convert_to_pdf(src, CACHE_DIR)
        except ConversionError as exc:
            raise DocumentError(str(exc), 500) from exc
        if converted != cached:
            converted.replace(cached)
    return cached, norm_type


# ---------------------------------------------------------------------------
# JSON storage helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def annotations_path(doc_id, norm_type):
    return ANNOTATIONS_DIR / f"{doc_id}__{norm_type}.json"


def snippets_meta_path(doc_id, norm_type):
    return SNIPPETS_DIR / f"{doc_id}__{norm_type}.json"


def snippets_dir(doc_id, norm_type):
    return SNIPPETS_DIR / f"{doc_id}__{norm_type}"


def doc_meta_path(doc_id):
    return DOC_META_DIR / f"{doc_id}.json"


def get_doc_title(doc_id):
    """A source document's display title -- defaults to its id (the file name,
    minus extension) until someone edits it on the annotations page."""
    meta = load_json(doc_meta_path(doc_id), default={})
    title = meta.get("title") if isinstance(meta, dict) else None
    return title.strip() if isinstance(title, str) and title.strip() else doc_id


def report_path(report_id):
    return REPORTS_DIR / f"{report_id}.json"


def slugify_report_name(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:50] or "document"


def allegation_case_path(case_id):
    return CASES_DIR / f"{case_id}.json"


def allegation_item_path(allegation_id):
    return ALLEGATIONS_DIR / f"{allegation_id}.json"


# Allegations are stored one-file-per-record (see api_allegations below), so
# there's no single array whose element order can just be preserved on save
# the way case-scoped allegations used to be ordered. This tracks the
# user's chosen display order separately; ids that fall out of it (freshly
# created, or the order file predating them) are appended in creation order.
ALLEGATION_ORDER_PATH = ALLEGATIONS_DIR / "allegation_order.json"


def load_allegation_order():
    order = load_json(ALLEGATION_ORDER_PATH, default=[])
    return order if isinstance(order, list) else []


def ordered_allegation_ids(item_ids_by_created_at):
    order = load_allegation_order()
    known = set(item_ids_by_created_at)
    ordered = [i for i in order if i in known]
    ordered += [i for i in item_ids_by_created_at if i not in set(ordered)]
    return ordered


# ---------------------------------------------------------------------------
# Allegations workspace: allegations are standalone records (see
# templates/allegations.html, static/allegations.js), each carrying its own
# ordered inculpatory/exculpatory evidence lists plus an optional set of
# linked case ids -- an allegation can pertain to zero or more cases rather
# than belonging to exactly one, so the link lives on the allegation, not
# nested inside a case file. Plain text only (no rich formatting), so
# sanitizing is just shape/length whitelisting -- unlike sanitize_report_doc
# there's no HTML/ProseMirror tree to walk.
# ---------------------------------------------------------------------------
ALLEGATION_MAX_TITLE_CHARS = 300
ALLEGATION_MAX_TEXT_CHARS = 10_000
EVIDENCE_MAX_ITEMS = 300
CASE_MAX_COURT_CHARS = 200
CASE_MAX_NUMBER_CHARS = 100
CASE_MAX_DATE_CHARS = 40
HEARING_MAX_ITEMS = 300
HEARING_DOC_MAX_ITEMS = 100
ALLEGATION_MAX_CASE_LINKS = 50


def _sanitize_item_id(raw):
    return raw[:64] if isinstance(raw, str) and raw else uuid.uuid4().hex[:12]


def _sanitize_text(raw, max_chars):
    return raw.strip()[:max_chars] if isinstance(raw, str) else ""


def sanitize_evidence_list(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:EVIDENCE_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        # An evidence item may link to at most one report (the editable
        # reports managed in reports.html) -- drop the link rather than
        # storing a dangling reference if that report no longer exists.
        report_id = item.get("report_id")
        has_report = isinstance(report_id, str) and DOC_ID_RE.match(report_id) and report_path(report_id).exists()
        out.append({
            "id": _sanitize_item_id(item.get("id")),
            "text": _sanitize_text(item.get("text"), ALLEGATION_MAX_TEXT_CHARS),
            "report_id": report_id if has_report else "",
        })
    return out


def sanitize_case_ids(raw):
    """Keep only ids that look valid and name a case that actually exists on
    disk, so a link never points at something the cases workspace can't
    resolve."""
    if not isinstance(raw, list):
        return []
    out = []
    for cid in raw[:ALLEGATION_MAX_CASE_LINKS]:
        if isinstance(cid, str) and DOC_ID_RE.match(cid) and cid not in out and allegation_case_path(cid).exists():
            out.append(cid)
    return out


def _source_doc_exists(doc_id, doc_type):
    if not isinstance(doc_id, str) or not DOC_ID_RE.match(doc_id):
        return False
    if doc_type in ("docx", "doc", "word"):
        return (DOCUMENTS_DIR / f"{doc_id}.docx").exists() or (DOCUMENTS_DIR / f"{doc_id}.doc").exists()
    return (DOCUMENTS_DIR / f"{doc_id}.pdf").exists()


def sanitize_hearing_doc_list(raw):
    """A hearing's submitted/received document list -- each entry links to an
    uploaded source document (documents/), not a report. Drop entries whose
    document no longer exists rather than storing a dangling reference."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:HEARING_DOC_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        doc_id = item.get("doc_id")
        doc_type = "docx" if item.get("doc_type") in ("docx", "doc", "word") else "pdf"
        if not _source_doc_exists(doc_id, doc_type):
            continue
        out.append({
            "id": _sanitize_item_id(item.get("id")),
            "doc_id": doc_id,
            "doc_type": doc_type,
        })
    return out


def unlink_document_from_hearings(doc_id):
    """Strip a deleted source document from every case's hearings' submitted/
    received document lists, so it disappears from those lists immediately
    rather than lingering until the hearing is next saved (sanitize_hearings
    already drops dangling doc_ids on save, but a delete shouldn't have to
    wait for that)."""
    for f in CASES_DIR.glob("*.json"):
        data = load_json(f, default=None)
        if not isinstance(data, dict):
            continue
        changed = False
        for hearing in data.get("hearings") or []:
            if not isinstance(hearing, dict):
                continue
            for kind in ("submitted_docs", "received_docs"):
                docs = hearing.get(kind)
                if not isinstance(docs, list):
                    continue
                kept = [d for d in docs if not (isinstance(d, dict) and d.get("doc_id") == doc_id)]
                if len(kept) != len(docs):
                    hearing[kind] = kept
                    changed = True
        if changed:
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_json(f, data)


def sanitize_hearings(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:HEARING_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        out.append({
            "id": _sanitize_item_id(item.get("id")),
            "date": _sanitize_text(item.get("date"), CASE_MAX_DATE_CHARS),
            "title": _sanitize_text(item.get("title"), ALLEGATION_MAX_TITLE_CHARS),
            "summary": _sanitize_text(item.get("summary"), ALLEGATION_MAX_TEXT_CHARS),
            "submitted_docs": sanitize_hearing_doc_list(item.get("submitted_docs")),
            "received_docs": sanitize_hearing_doc_list(item.get("received_docs")),
        })
    return out


# ---------------------------------------------------------------------------
# Baking annotations into a snippet render
# ---------------------------------------------------------------------------

def _clamp01(value):
    return max(0.0, min(1.0, float(value)))


def _hex_to_rgb01(hex_color):
    hex_color = hex_color.lstrip("#")
    return (
        int(hex_color[0:2], 16) / 255,
        int(hex_color[2:4], 16) / 255,
        int(hex_color[4:6], 16) / 255,
    )


def sanitize_annotations(raw, max_count=500, max_points=2000):
    """Validate/clamp client-submitted annotations before drawing them into a PDF page."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:max_count]:
        if not isinstance(item, dict):
            continue
        color = item.get("color")
        if not (isinstance(color, str) and HEX_COLOR_RE.match(color)):
            color = DEFAULT_ANNOTATION_COLOR

        if item.get("kind") == "rect":
            try:
                x, y, w, h = (_clamp01(item[k]) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError):
                continue
            if w <= 0 or h <= 0:
                continue
            out.append({"kind": "rect", "color": color, "x": x, "y": y, "w": w, "h": h})

        elif item.get("kind") == "freehand":
            pts = item.get("points")
            if not isinstance(pts, list):
                continue
            clean_pts = []
            for pt in pts[:max_points]:
                if not (isinstance(pt, list) and len(pt) == 2):
                    continue
                try:
                    clean_pts.append((_clamp01(pt[0]), _clamp01(pt[1])))
                except (TypeError, ValueError):
                    continue
            if len(clean_pts) >= 2:
                out.append({"kind": "freehand", "color": color, "points": clean_pts})
    return out


def draw_annotations_on_page(page, annotations, page_rect):
    """Burn sanitized annotations into a fitz page's content stream (in memory only,
    never saved back to disk) so a subsequent get_pixmap() render includes them
    as sharp vector shapes at whatever DPI is requested."""
    if not annotations:
        return
    shape = page.new_shape()
    line_width = max(1.2, page_rect.width * 0.0025)
    for a in annotations:
        color = _hex_to_rgb01(a["color"])
        if a["kind"] == "rect":
            r = fitz.Rect(
                page_rect.x0 + a["x"] * page_rect.width,
                page_rect.y0 + a["y"] * page_rect.height,
                page_rect.x0 + (a["x"] + a["w"]) * page_rect.width,
                page_rect.y0 + (a["y"] + a["h"]) * page_rect.height,
            )
            shape.draw_rect(r)
            shape.finish(color=color, width=line_width)
        elif a["kind"] == "freehand":
            pts = [
                fitz.Point(page_rect.x0 + px * page_rect.width, page_rect.y0 + py * page_rect.height)
                for px, py in a["points"]
            ]
            shape.draw_polyline(pts)
            shape.finish(color=color, width=line_width, closePath=False)
    shape.commit()


# ---------------------------------------------------------------------------
# Report editor: the document is Tiptap/ProseMirror JSON (see
# static/src/editor.js), not HTML -- the browser is the one thing that ever
# turns it into markup (for display) or draws it (this module renders PDF/
# DOCX straight from the JSON, see "Multilevel numbering" below). Sanitizing
# means walking that JSON against a fixed whitelist of node/mark types and
# attribute shapes matching the editor's actual schema, dropping anything
# else -- structurally safer than the old HTML-tag sanitizer (no HTML-parser
# edge cases, no way for a stray tag soup to confuse it) and it doubles as
# schema validation before either exporter ever touches the tree.
# ---------------------------------------------------------------------------

REPORT_SAFE_URL_RE = re.compile(
    r"^(https?://|mailto:|/media/|data:image/(png|jpeg|jpg|gif|webp);base64,)", re.IGNORECASE
)
REPORT_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
REPORT_FONT_FAMILY_RE = re.compile(r"^[A-Za-z0-9 ,'\-]{1,60}$")
REPORT_FONT_SIZE_RE = re.compile(r"^\d{1,3}pt$")
REPORT_TEXT_ALIGN_VALUES = {"left", "center", "right", "justify"}
REPORT_LEVEL_TYPE_VALUES = {"decimal", "alpha", "upalpha", "roman", "uproman"}
REPORT_LEVEL_WRAP_VALUES = {"none", "period", "paren", "trail"}
REPORT_MAX_DOC_JSON_CHARS = 1_000_000


def _safe_url(value):
    return value if isinstance(value, str) and REPORT_SAFE_URL_RE.match(value.strip()) else None


def _sanitize_num_levels(raw):
    if not isinstance(raw, list):
        return None
    out = []
    for entry in raw[:LIST_MAX_LEVELS]:
        if not isinstance(entry, dict):
            entry = {}
        t = entry.get("type") if entry.get("type") in REPORT_LEVEL_TYPE_VALUES else "decimal"
        w = entry.get("wrap") if entry.get("wrap") in REPORT_LEVEL_WRAP_VALUES else "period"
        out.append({"type": t, "wrap": w})
    return out or None


def _sanitize_marks(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        t = m.get("type")
        attrs = m.get("attrs") if isinstance(m.get("attrs"), dict) else {}
        if t in ("bold", "italic", "strike", "underline", "code"):
            out.append({"type": t})
        elif t == "textStyle":
            clean = {}
            color = attrs.get("color")
            if isinstance(color, str) and REPORT_HEX_COLOR_RE.match(color):
                clean["color"] = color
            font = attrs.get("fontFamily")
            if isinstance(font, str) and REPORT_FONT_FAMILY_RE.match(font):
                clean["fontFamily"] = font
            size = attrs.get("fontSize")
            if isinstance(size, str) and REPORT_FONT_SIZE_RE.match(size):
                clean["fontSize"] = size
            if clean:
                out.append({"type": "textStyle", "attrs": clean})
        elif t == "highlight":
            color = attrs.get("color")
            if isinstance(color, str) and REPORT_HEX_COLOR_RE.match(color):
                out.append({"type": "highlight", "attrs": {"color": color}})
        elif t == "link":
            href = _safe_url(attrs.get("href"))
            if href:
                out.append({"type": "link", "attrs": {"href": href}})
    return out


REPORT_BLOCK_TYPES = {
    "paragraph", "heading", "bulletList", "orderedList", "listItem",
    "blockquote", "horizontalRule", "codeBlock", "image", "hardBreak",
}


def sanitize_report_doc(raw, max_chars=REPORT_MAX_DOC_JSON_CHARS):
    """Whitelist-validate a client-submitted ProseMirror document (see
    static/src/editor.js) against the editor's actual schema before it's
    stored or ever fed to an exporter -- unrecognized node/mark types or
    attribute values are dropped, not merely escaped."""
    if not isinstance(raw, dict) or raw.get("type") != "doc":
        return {"type": "doc", "content": []}
    try:
        if len(json.dumps(raw)) > max_chars:
            return {"type": "doc", "content": []}
    except (TypeError, ValueError):
        return {"type": "doc", "content": []}

    def sanitize_node(node):
        if not isinstance(node, dict):
            return None
        t = node.get("type")
        if t == "text":
            text = node.get("text")
            if not isinstance(text, str) or not text:
                return None
            return {"type": "text", "text": text, "marks": _sanitize_marks(node.get("marks"))}
        if t not in REPORT_BLOCK_TYPES:
            return None

        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        out = {"type": t}
        clean_attrs = {}
        if t == "heading":
            level = attrs.get("level")
            clean_attrs["level"] = level if level in (1, 2, 3, 4) else 1
        if t in ("heading", "paragraph"):
            align = attrs.get("textAlign")
            if align in REPORT_TEXT_ALIGN_VALUES:
                clean_attrs["textAlign"] = align
            indent = attrs.get("indent")
            if isinstance(indent, int) and 0 <= indent <= 10:
                clean_attrs["indent"] = indent
        if t == "orderedList":
            start = attrs.get("start")
            if isinstance(start, int) and start > 0:
                clean_attrs["start"] = start
            levels = _sanitize_num_levels(attrs.get("numLevels"))
            if levels:
                clean_attrs["numLevels"] = levels
                clean_attrs["numCascade"] = bool(attrs.get("numCascade"))
        if t == "image":
            src = _safe_url(attrs.get("src"))
            if not src:
                return None
            clean_attrs["src"] = src
            if isinstance(attrs.get("alt"), str):
                clean_attrs["alt"] = attrs["alt"][:500]
            if isinstance(attrs.get("width"), (int, float)):
                clean_attrs["width"] = attrs["width"]
            if isinstance(attrs.get("height"), (int, float)):
                clean_attrs["height"] = attrs["height"]
            if attrs.get("align") in ("left", "center", "right"):
                clean_attrs["align"] = attrs["align"]
        if clean_attrs:
            out["attrs"] = clean_attrs

        if t not in ("image", "hardBreak"):
            content = []
            for child in node.get("content") or []:
                cleaned = sanitize_node(child)
                if cleaned is not None:
                    content.append(cleaned)
            out["content"] = content
        return out

    content = []
    for child in raw.get("content") or []:
        cleaned = sanitize_node(child)
        if cleaned is not None:
            content.append(cleaned)
    return {"type": "doc", "content": content}


def inline_doc_images(node):
    """Replaces image nodes' `/media/snippets/...` src with a base64 data URI
    read directly from disk, so the exported PDF/DOCX is self-contained --
    Story (PDF) has no network access, and python-docx needs raw bytes
    either way. Returns a new tree; `node` itself is left untouched."""
    if not isinstance(node, dict):
        return node
    if node.get("type") == "image":
        attrs = dict(node.get("attrs") or {})
        src = attrs.get("src", "")
        parsed = urlsplit(src)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path.startswith("/media/snippets/") and len(parts) >= 2:
            filename, url_doc_id = parts[-1], parts[-2]
            if DOC_ID_RE.match(url_doc_id) and "/" not in filename and "\\" not in filename:
                try:
                    url_type = normalize_type(parse_qs(parsed.query).get("type", ["pdf"])[0])
                    f = snippets_dir(url_doc_id, url_type) / filename
                    if f.exists():
                        b64 = base64.b64encode(f.read_bytes()).decode("ascii")
                        attrs["src"] = f"data:image/png;base64,{b64}"
                except DocumentError:
                    pass
        return {**node, "attrs": attrs}
    if "content" in node:
        return {**node, "content": [inline_doc_images(c) for c in node["content"]]}
    return node


REPORT_PDF_CSS = """
  body { font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.5; color: #1c1c1c; }
  h1 { font-size: 20pt; margin-bottom: 4pt; }
  h2 { font-size: 15pt; }
  h3 { font-size: 13pt; }
  img { max-width: 100%; }
  p.img-p { margin: 12pt 0; }
  table { border-collapse: collapse; width: 100%; }
  td, th { border: 1px solid #ccc; padding: 4pt; text-align: left; }
  ol, ul { list-style: none; margin: 0; padding-left: 0; }
  li { margin: 2pt 0; }
  li ol, li ul { margin-left: 18pt; }
"""


INLINE_MARK_TAGS = {"bold": "strong", "italic": "em", "underline": "u", "strike": "s", "code": "code"}


def _mark_style_attrs(mark_type, attrs):
    if mark_type == "textStyle":
        decls = []
        if attrs.get("color"):
            decls.append(f"color: {attrs['color']}")
        if attrs.get("fontFamily"):
            decls.append(f"font-family: {attrs['fontFamily']}")
        if attrs.get("fontSize"):
            decls.append(f"font-size: {attrs['fontSize']}")
        return "; ".join(decls)
    if mark_type == "highlight":
        return f"background-color: {attrs.get('color', '#fff59d')}"
    return ""


def _json_inline_to_html(nodes):
    """Renders a run of inline JSON nodes (text/hardBreak) to an HTML
    string, applying each text node's marks -- the JSON equivalent of the
    nested <strong>/<em>/... tags the old contenteditable editor produced,
    just read from a flat `marks` list instead of tag nesting."""
    out = []
    for node in nodes:
        t = node.get("type")
        if t == "text":
            html = html_escape(node.get("text", ""))
            href = None
            for mark in node.get("marks") or []:
                mt = mark.get("type")
                mattrs = mark.get("attrs") or {}
                if mt == "link":
                    href = mattrs.get("href")
                elif mt in INLINE_MARK_TAGS:
                    tag = INLINE_MARK_TAGS[mt]
                    html = f"<{tag}>{html}</{tag}>"
                elif mt in ("textStyle", "highlight"):
                    style = _mark_style_attrs(mt, mattrs)
                    if style:
                        html = f'<span style="{html_escape(style, quote=True)}">{html}</span>'
            if href:
                html = f'<a href="{html_escape(href, quote=True)}">{html}</a>'
            out.append(html)
        elif t == "hardBreak":
            out.append("<br>")
    return "".join(out)


def _json_block_style(attrs):
    decls = []
    if attrs.get("textAlign"):
        decls.append(f"text-align: {attrs['textAlign']}")
    if attrs.get("indent"):
        decls.append(f"margin-left: {18 * attrs['indent']}pt")
    return f' style="{html_escape("; ".join(decls), quote=True)}"' if decls else ""


def _json_blocks_to_html(nodes, num_state=None, depth=0):
    """Walks the sanitized ProseMirror JSON tree, computing the same section
    numbers the live editor's ListMarkers decoration plugin renders (see
    _ListNumberingState above and static/src/listNumbering.js), and emits an
    HTML string with each numbered <li>'s marker as literal text -- mirrors
    _docx_render_blocks' recursion below, just building HTML instead of docx
    paragraphs. Neither exporter can rely on a live browser: DOCX is
    assembled directly via python-docx, and PDF's fitz.Story is a
    lightweight HTML/CSS engine of unverified ::before/counter support."""
    out = []
    for node in nodes:
        t = node.get("type")
        attrs = node.get("attrs") or {}
        content = node.get("content") or []
        if t == "heading":
            level = attrs.get("level", 1)
            out.append(f"<h{level}{_json_block_style(attrs)}>{_json_inline_to_html(content)}</h{level}>")
        elif t == "paragraph":
            out.append(f"<p{_json_block_style(attrs)}>{_json_inline_to_html(content)}</p>")
        elif t == "blockquote":
            out.append(f"<blockquote>{_json_blocks_to_html(content, depth=depth)}</blockquote>")
        elif t == "horizontalRule":
            out.append("<hr>")
        elif t == "codeBlock":
            out.append(f"<pre><code>{html_escape(_flatten_json_text(node))}</code></pre>")
        elif t == "image":
            # fitz.Story's CSS support doesn't include auto-margin block
            # centering (verified empirically -- margin-left/right:auto on
            # the <img> itself left it flush left), so alignment is done
            # the way any renderer is virtually guaranteed to support:
            # text-align on a wrapping paragraph around a plain (non-block)
            # <img>.
            decls = []
            if attrs.get("width"):
                decls.append(f"width:{attrs['width']}px")
                decls.append(f"height:{attrs.get('height', 'auto')}px")
            style = f' style="{html_escape("; ".join(decls), quote=True)}"' if decls else ""
            align = attrs.get("align", "left")
            out.append(
                f'<p class="img-p" style="text-align:{align}">'
                f'<img src="{html_escape(attrs.get("src", ""), quote=True)}"{style}></p>'
            )
        elif t == "bulletList":
            out.append(f"<ul>{_json_blocks_to_html(content, depth=depth + 1)}</ul>")
        elif t == "orderedList":
            child_depth = depth + 1
            child_state = num_state or _ListNumberingState(attrs.get("numCascade"), attrs.get("numLevels"))
            child_state.enter_list(child_depth, attrs.get("start"))
            out.append(f"<ol>{_json_blocks_to_html(content, num_state=child_state, depth=child_depth)}</ol>")
        elif t == "listItem":
            marker = ""
            if num_state is not None:
                num_state.next_value(depth)
                marker = html_escape(num_state.marker_text(depth))
            first = content[0] if content else None
            rest = content[1:]
            first_html = _json_inline_to_html(first.get("content") or []) if first and first.get("type") in ("paragraph", "heading") else ""
            out.append(f"<li>{marker}{first_html}{_json_blocks_to_html(rest, num_state=num_state, depth=depth)}</li>")
    return "".join(out)


def _flatten_json_text(node):
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_flatten_json_text(c) for c in node.get("content") or [])


def _pdf_draw_page_number(device, mediabox, m, position, page_num, font, font_size):
    """Draws a page-number string into the header or footer margin band of
    the page already written to `device`, reusing fitz.Story for
    layout/alignment rather than hand-computing text placement.

    fitz's Story lays a block out from the top of whatever rect it's given,
    and (at least for a fresh Story with no stylesheet) reserves noticeably
    more line-box height than the font size alone would suggest -- handing
    it a rect exactly as tall as the header/footer margin, as a naive
    top/bottom-anchored placement would, can silently overflow (Story then
    draws nothing at all) once the font size is large relative to the
    margin, e.g. the "large page-number font, default margins" combination
    Page setup now allows. So this measures the real single-line height
    first with a throwaway Story placed in a tall scratch rect, then hands a
    *second* Story (place() consumes a Story's layout state) a rect of that
    measured height, centered on the margin band's vertical midpoint --
    matching the vertical centering the on-screen preview achieves via
    line-height (see .page-number-label in style.css) while still fitting
    whatever font size was chosen."""
    vert, _, horiz = position.partition("-")
    band_height = m["header"] if vert == "top" else m["footer"]
    if band_height <= 0:
        return  # no room in the margin to draw into
    css = f"font-family: {font}, Helvetica, Arial, sans-serif; font-size: {font_size}pt; color: #555; margin: 0;"
    html = f'<p style="text-align:{horiz}; {css}">{page_num}</p>'

    x0, x1 = mediabox.x0 + m["left"], mediabox.x1 - m["right"]
    probe_rect = fitz.Rect(x0, mediabox.y0, x1, mediabox.y1)
    more, filled = fitz.Story(html=html).place(probe_rect)
    line_height = (filled[3] - filled[1]) if not more else font_size * 1.3

    band_top = mediabox.y0 if vert == "top" else mediabox.y1 - band_height
    center_y = band_top + band_height / 2
    rect = fitz.Rect(x0, center_y - line_height / 2, x1, center_y + line_height / 2)

    story = fitz.Story(html=html)
    story.place(rect)
    story.draw(device)


def render_report_pdf(title, doc_json, margins=None, page_numbers=None):
    numbered_body_html = _json_blocks_to_html(doc_json.get("content") or [])

    heading = f"<h1>{html_escape(title)}</h1>" if title else ""
    full_html = f"<html><head><style>{REPORT_PDF_CSS}</style></head><body>{heading}{numbered_body_html}</body></html>"

    m = sanitize_margins(margins)
    pn = sanitize_page_numbers(page_numbers)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (m["left"], m["header"], -m["right"], -m["footer"])
    story = fitz.Story(html=full_html)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    more = 1
    page_index = 0
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        if pn["position"] != "none" and page_index >= pn["skip"]:
            _pdf_draw_page_number(device, mediabox, m, pn["position"], page_index - pn["skip"] + 1, pn["font"], pn["fontSize"])
        writer.end_page()
        page_index += 1
    writer.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Word (.docx) export
#
# python-docx has no HTML/JSON importer, so this walks the sanitized
# ProseMirror JSON tree (see sanitize_report_doc above) directly and builds
# the document node by node.
# ---------------------------------------------------------------------------

DOCX_HEADING_STYLE = {1: "Heading 1", 2: "Heading 2", 3: "Heading 3", 4: "Heading 4"}


def _docx_image_stream(src):
    if not src or not src.startswith("data:image/"):
        return None
    try:
        header, b64 = src.split(",", 1)
        return io.BytesIO(base64.b64decode(b64))
    except (ValueError, base64.binascii.Error):
        return None


def _docx_add_hyperlink(paragraph, url, text, fmt):
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
    hyperlink = paragraph._p.makeelement(qn("w:hyperlink"), {qn("r:id"): r_id})
    run_el = paragraph._p.makeelement(qn("w:r"), {})
    rpr = paragraph._p.makeelement(qn("w:rPr"), {})
    color = paragraph._p.makeelement(qn("w:color"), {qn("w:val"): "1155CC"})
    underline = paragraph._p.makeelement(qn("w:u"), {qn("w:val"): "single"})
    rpr.append(color)
    rpr.append(underline)
    if fmt.get("bold"):
        rpr.append(paragraph._p.makeelement(qn("w:b"), {}))
    if fmt.get("italic"):
        rpr.append(paragraph._p.makeelement(qn("w:i"), {}))
    run_el.append(rpr)
    text_el = paragraph._p.makeelement(qn("w:t"), {})
    text_el.text = text
    text_el.set(qn("xml:space"), "preserve")
    run_el.append(text_el)
    hyperlink.append(run_el)
    paragraph._p.append(hyperlink)


def _docx_apply_run_format(run, fmt):
    run.bold = bool(fmt.get("bold"))
    run.italic = bool(fmt.get("italic"))
    run.underline = bool(fmt.get("underline"))
    run.font.strike = bool(fmt.get("strike"))
    if fmt.get("code"):
        run.font.name = "Courier New"
    if fmt.get("color"):
        try:
            run.font.color.rgb = RGBColor.from_string(fmt["color"].lstrip("#")[:6].ljust(6, "0"))
        except ValueError:
            pass


EMU_PER_CSS_PX = 9525  # 914400 EMU/inch / 96 CSS px/inch


def _docx_add_image(attrs, paragraph, max_width_emu):
    stream = _docx_image_stream(attrs.get("src"))
    if stream is None:
        return
    if isinstance(attrs.get("width"), (int, float)) and attrs["width"] > 0:
        # Editor-resized (or snippet-computed) width, in the same CSS px
        # convention used everywhere else in this app -- reflects what the
        # user actually saw/set, unlike the image file's own native size.
        width = Emu(int(attrs["width"] * EMU_PER_CSS_PX))
    else:
        try:
            width = Emu(int(DocxImage.from_blob(stream.getvalue()).width))
        except Exception:
            return
    if max_width_emu:
        width = Emu(min(int(width), max_width_emu))
    stream.seek(0)
    run = paragraph.add_run()
    run.add_picture(stream, width=width)


def _docx_run_format_from_marks(marks):
    fmt = {}
    for mark in marks:
        t = mark.get("type")
        attrs = mark.get("attrs") or {}
        if t in ("bold", "italic", "underline", "strike", "code"):
            fmt[t] = True
        elif t == "textStyle" and attrs.get("color"):
            fmt["color"] = attrs["color"]
    return fmt


def _docx_render_inline(nodes, paragraph, max_width_emu):
    for node in nodes:
        t = node.get("type")
        if t == "text":
            fmt = _docx_run_format_from_marks(node.get("marks") or [])
            href = next((m["attrs"]["href"] for m in node.get("marks") or [] if m.get("type") == "link"), None)
            text = node.get("text", "")
            if href and text:
                _docx_add_hyperlink(paragraph, href, text, fmt)
            elif text:
                run = paragraph.add_run(text)
                _docx_apply_run_format(run, fmt)
        elif t == "hardBreak":
            paragraph.add_run().add_break()
        elif t == "image":
            _docx_add_image(node.get("attrs") or {}, paragraph, max_width_emu)


# ---------------------------------------------------------------------------
# Multilevel numbering ("section numbers") -- shared by the PDF and DOCX
# exporters below. Mirrors the template/restart-or-continue attributes the
# live editor's OrderedList node carries (numCascade/numLevels/start -- see
# static/src/listNumbering.js): a template is `numCascade`/`numLevels` on
# the outermost <orderedList> of a nesting chain, and a restart/continue
# override is the native `start` attribute on any <orderedList> (mid-list
# restarts split the list into two sibling nodes -- see
# splitListForRestart-equivalent logic in listNumbering.js). Neither
# exporter can rely on the live browser's own rendering, so both compute the
# same numbers here in Python instead and render them as literal marker
# text.
# ---------------------------------------------------------------------------
LIST_MAX_LEVELS = 6
LIST_WRAPS = {
    "none": ("", ""),
    "period": ("", "."),
    "paren": ("(", ")"),
    "trail": ("", ")"),
}


def _to_alpha(n, upper=False):
    s = ""
    while n > 0:
        n -= 1
        s = chr(97 + (n % 26)) + s
        n //= 26
    return s.upper() if upper else s


_ROMAN_TABLE = [
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
    (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
]


def _to_roman(n, upper=False):
    s = ""
    for value, sym in _ROMAN_TABLE:
        while n >= value:
            s += sym
            n -= value
    return s.upper() if upper else s


def _format_counter_value(n, type_):
    if type_ == "alpha":
        return _to_alpha(n)
    if type_ == "upalpha":
        return _to_alpha(n, upper=True)
    if type_ == "roman":
        return _to_roman(n)
    if type_ == "uproman":
        return _to_roman(n, upper=True)
    return str(n)


def _normalize_levels(levels):
    """levels[i] = {"type", "wrap"} for level i+1 -- falls back to plain
    decimal/period (the default, untemplated look) for any level without an
    explicit entry, matching the editor's own default when numLevels is
    absent."""
    out = []
    for i in range(LIST_MAX_LEVELS):
        lvl = levels[i] if isinstance(levels, list) and i < len(levels) and isinstance(levels[i], dict) else None
        out.append(lvl if lvl else {"type": "decimal", "wrap": "period"})
    return out


class _ListNumberingState:
    """One of these per top-level <orderedList>, threaded through the
    orderedList/listItem recursion of both exporters below. Tracks a
    running per-level counter, including any restart/continue override
    found in an element's own `start` attribute -- mirrors the ListMarkers
    decoration plugin's ListNumberingState in static/src/listNumbering.js."""

    def __init__(self, cascade, levels):
        self.cascade = bool(cascade)
        self.levels = _normalize_levels(levels)
        self.counters = [0] * LIST_MAX_LEVELS

    def enter_list(self, depth, start):
        if isinstance(start, int):
            self.counters[depth - 1] = start - 1

    def next_value(self, depth):
        self.counters[depth - 1] += 1
        return self.counters[depth - 1]

    def marker_text(self, depth):
        start = 1 if self.cascade else depth
        parts = []
        for i in range(start, depth + 1):
            lvl = self.levels[i - 1]
            pre, suf = LIST_WRAPS.get(lvl.get("wrap"), LIST_WRAPS["period"])
            parts.append(pre + _format_counter_value(self.counters[i - 1], lvl.get("type")) + suf)
        return "".join(parts) + " "


def _docx_render_blocks(nodes, doc, max_width_emu, num_state=None, depth=0):
    for node in nodes:
        t = node.get("type")
        attrs = node.get("attrs") or {}
        content = node.get("content") or []

        if t == "heading":
            p = doc.add_paragraph(style=DOCX_HEADING_STYLE.get(attrs.get("level", 1), "Heading 1"))
            _docx_render_inline(content, p, max_width_emu)
        elif t == "horizontalRule":
            doc.add_paragraph().add_run("—" * 20)
        elif t == "paragraph":
            p = doc.add_paragraph()
            if attrs.get("indent"):
                p.paragraph_format.left_indent = Pt(18 * attrs["indent"])
            _docx_render_inline(content, p, max_width_emu)
        elif t == "blockquote":
            for child in content:
                if child.get("type") == "paragraph":
                    p = doc.add_paragraph(style="Intense Quote")
                    _docx_render_inline(child.get("content") or [], p, max_width_emu)
                else:
                    _docx_render_blocks([child], doc, max_width_emu)
        elif t == "bulletList":
            _docx_render_blocks(content, doc, max_width_emu, depth=depth + 1)
        elif t == "orderedList":
            child_depth = depth + 1
            child_state = num_state or _ListNumberingState(attrs.get("numCascade"), attrs.get("numLevels"))
            child_state.enter_list(child_depth, attrs.get("start"))
            _docx_render_blocks(content, doc, max_width_emu, num_state=child_state, depth=child_depth)
        elif t == "listItem":
            first = content[0] if content else None
            rest = content[1:]
            if num_state is not None:
                num_state.next_value(depth)
                p = doc.add_paragraph(style="List Paragraph")
                p.paragraph_format.left_indent = Pt(18 * depth)
                p.paragraph_format.first_line_indent = Pt(-18)
                p.add_run(num_state.marker_text(depth))
            else:
                p = doc.add_paragraph(style="List Bullet")
            if first and first.get("type") in ("paragraph", "heading"):
                _docx_render_inline(first.get("content") or [], p, max_width_emu)
            elif first:
                rest = [first] + rest
            _docx_render_blocks(rest, doc, max_width_emu, num_state=num_state, depth=depth)
        elif t == "codeBlock":
            run = doc.add_paragraph().add_run(_flatten_json_text(node))
            run.font.name = "Courier New"
        elif t == "image":
            p = doc.add_paragraph()
            p.alignment = {
                "center": WD_ALIGN_PARAGRAPH.CENTER,
                "right": WD_ALIGN_PARAGRAPH.RIGHT,
            }.get(attrs.get("align"), WD_ALIGN_PARAGRAPH.LEFT)
            _docx_add_image(attrs, p, max_width_emu)


def _docx_field_run(paragraph, tag, text=None, **attrs):
    run = paragraph.add_run()
    el = OxmlElement(tag)
    for key, val in attrs.items():
        el.set(qn(key), val)
    if text is not None:
        el.text = text
        el.set(qn("xml:space"), "preserve")
    run._r.append(el)
    return run


def _docx_add_page_field(paragraph):
    """Inserts a plain Word PAGE field: { PAGE }. The literal "1" is only the
    cached display value Word shows before it first recalculates fields."""
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "begin"})
    _docx_field_run(paragraph, "w:instrText", " PAGE ")
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "separate"})
    paragraph.add_run("1")
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "end"})


def _docx_add_conditional_page_field(paragraph, skip):
    """Inserts { IF { PAGE } > skip "{ = { PAGE } - skip }" "" } -- Word
    evaluates this per rendered page, so it correctly hides the number on
    the first `skip` pages (and restarts the visible count at 1 right after)
    regardless of where python-docx's own generation loop happened to put
    paragraph/section boundaries (which don't correspond to physical pages
    -- only Word's own layout does)."""
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "begin"})
    _docx_field_run(paragraph, "w:instrText", " IF ")
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "begin"})
    _docx_field_run(paragraph, "w:instrText", " PAGE ")
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "end"})
    _docx_field_run(paragraph, "w:instrText", f' > {skip} "')
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "begin"})
    _docx_field_run(paragraph, "w:instrText", " = ")
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "begin"})
    _docx_field_run(paragraph, "w:instrText", " PAGE ")
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "end"})
    _docx_field_run(paragraph, "w:instrText", f" - {skip} ")
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "end"})
    _docx_field_run(paragraph, "w:instrText", '" "" ')
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "separate"})
    paragraph.add_run("")
    _docx_field_run(paragraph, "w:fldChar", **{"w:fldCharType": "end"})


def _docx_clean_font_name(name):
    """Strips the CSS-quoting single quotes multi-word font values carry
    (e.g. "'Times New Roman'", matching the #pageNumberFontInput option
    values in reports.html) -- python-docx's run.font.name wants the bare
    family name, not a CSS font-family value."""
    return name.strip("'") if isinstance(name, str) else name


def _docx_apply_page_numbers(doc, page_numbers):
    position = page_numbers.get("position", "none")
    if position == "none":
        return
    vert, _, horiz = position.partition("-")
    align = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
    }[horiz]

    section = doc.sections[0]
    container = section.header if vert == "top" else section.footer
    container.is_linked_to_previous = False
    paragraph = container.paragraphs[0] if container.paragraphs else container.add_paragraph()
    paragraph.alignment = align

    skip = page_numbers.get("skip", 0)
    if skip > 0:
        _docx_add_conditional_page_field(paragraph, skip)
    else:
        _docx_add_page_field(paragraph)

    font_name = _docx_clean_font_name(page_numbers.get("font"))
    font_size = page_numbers.get("fontSize")
    for run in paragraph.runs:
        if font_name:
            run.font.name = font_name
        if font_size:
            run.font.size = Pt(font_size)

    # Word normally shows a field's *cached* value until it's recalculated
    # (on manual refresh, or a print); force recalculation on open so the
    # page numbers are correct the first time the file is viewed.
    update_fields = OxmlElement("w:updateFields")
    update_fields.set(qn("w:val"), "true")
    doc.settings.element.append(update_fields)


def render_report_docx(title, doc_json, margins=None, page_numbers=None):
    doc = DocxDocument()
    doc.styles["Normal"].font.name = "Arial"
    doc.styles["Normal"].font.size = Pt(11)

    section = doc.sections[0]
    m = sanitize_margins(margins)
    section.left_margin = Pt(m["left"])
    section.right_margin = Pt(m["right"])
    section.top_margin = Pt(m["header"])
    section.bottom_margin = Pt(m["footer"])
    max_width_emu = int(section.page_width - section.left_margin - section.right_margin)

    _docx_apply_page_numbers(doc, sanitize_page_numbers(page_numbers))

    if title:
        doc.add_paragraph(title, style="Heading 1")

    _docx_render_blocks(doc_json.get("content") or [], doc, max_width_emu)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/annotations")
def page_view():
    doc_id = request.args.get("doc", "")
    raw_type = request.args.get("type", "pdf")

    if not doc_id:
        return render_template("annotations.html", doc_id="", docs=list_source_docs())

    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        raise DocumentError("page must be an integer", 400)

    path, norm_type = resolve_pdf_path(doc_id, raw_type)
    with fitz.open(path) as d:
        page_count = d.page_count
    if page_count == 0:
        raise DocumentError("Document has no pages", 400)
    page = max(1, min(page, page_count))

    # Build "/render/__PAGE__"-style URL templates (page=1 always exists; the
    # literal "/1" segment right after the fixed route prefix is unambiguous
    # since doc_id can never contain "/", so this string-replace is safe).
    render_url_base = url_for("api_render", doc_id=doc_id, page=1, type=raw_type).replace("/render/1", "/render/__PAGE__")
    annotations_url_base = url_for("api_annotations", doc_id=doc_id, page=1, type=raw_type).replace(
        "/annotations/1", "/annotations/__PAGE__"
    )
    snippet_url_base = url_for("api_create_snippet", doc_id=doc_id, page=1, type=raw_type).replace(
        "/snippet/1", "/snippet/__PAGE__"
    )

    return render_template(
        "annotations.html",
        doc_id=doc_id,
        doc_type=raw_type,
        norm_type=norm_type,
        doc_title=get_doc_title(doc_id),
        title_url=url_for("api_doc_title", doc_id=doc_id),
        page=page,
        page_count=page_count,
        render_url_base=render_url_base,
        annotations_url_base=annotations_url_base,
        snippet_url_base=snippet_url_base,
    )


@app.route("/reports")
def documents_view():
    source_docs = list_source_docs()

    report_id = request.args.get("report", "")
    if report_id:
        check_report_id(report_id)
        if not report_path(report_id).exists():
            raise DocumentError(f"No report with id {report_id!r}", 404)

    preselect_source = request.args.get("source", "")
    preselect_type = request.args.get("type", "pdf")
    if preselect_source:
        check_doc_id(preselect_source)

    return render_template(
        "reports.html",
        source_docs=source_docs,
        report_id=report_id,
        preselect_source=preselect_source,
        preselect_type=preselect_type,
    )


@app.route("/allegations")
def allegations_view():
    # Allegations are global (see api_allegations below); "case" is only an
    # optional hint from the cases workspace to pre-filter the list to
    # allegations linked to that case, not a page the allegation belongs to.
    filter_case_id = request.args.get("case", "")
    if filter_case_id:
        check_report_id(filter_case_id)
        if not allegation_case_path(filter_case_id).exists():
            raise DocumentError(f"No case with id {filter_case_id!r}", 404)

    return render_template("allegations.html", filter_case_id=filter_case_id)


@app.route("/cases")
def cases_view():
    return render_template("cases.html")


# ---------------------------------------------------------------------------
# API: document upload
# ---------------------------------------------------------------------------

@app.route("/api/documents/upload", methods=["POST"])
def api_upload_document():
    f = request.files.get("file")
    if f is None or not f.filename:
        raise DocumentError("No file uploaded (expected multipart field 'file')", 400)

    ext = Path(f.filename).suffix.lower()
    norm_type = UPLOAD_EXTENSIONS.get(ext)
    if norm_type is None:
        raise DocumentError(f"Unsupported file type {ext!r}. Upload a .pdf, .docx, or .doc file.", 400)

    data = f.read()
    if not data:
        raise DocumentError("Uploaded file is empty", 400)

    if norm_type == "pdf":
        try:
            with fitz.open(stream=data, filetype="pdf") as d:
                if d.page_count == 0:
                    raise DocumentError("PDF has no pages", 400)
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError(f"File is not a valid PDF: {exc}", 400) from exc
    else:
        try:
            DocxDocument(io.BytesIO(data))
        except Exception as exc:
            raise DocumentError(f"File is not a valid Word document: {exc}", 400) from exc

    stem = slugify_report_name(Path(f.filename).stem)
    doc_id = stem
    while any((DOCUMENTS_DIR / f"{doc_id}{e}").exists() for e in (".pdf", ".docx", ".doc")):
        doc_id = f"{stem}-{uuid.uuid4().hex[:6]}"

    out_path = DOCUMENTS_DIR / f"{doc_id}{ext}"
    out_path.write_bytes(data)

    return jsonify({"id": doc_id, "type": norm_type, "filename": out_path.name})


# ---------------------------------------------------------------------------
# API: document info / rendering
# ---------------------------------------------------------------------------

@app.route("/api/doc/<doc_id>/info")
def api_doc_info(doc_id):
    path, _ = resolve_pdf_path(doc_id, request.args.get("type", "pdf"))
    with fitz.open(path) as d:
        pages = [{"width": p.rect.width, "height": p.rect.height} for p in d]
    return jsonify({"page_count": len(pages), "pages": pages})


@app.route("/api/doc/<doc_id>/title", methods=["POST"])
def api_doc_title(doc_id):
    check_doc_id(doc_id)
    if not any((DOCUMENTS_DIR / f"{doc_id}{ext}").exists() for ext in (".pdf", ".docx", ".doc")):
        raise DocumentError(f"No document with id {doc_id!r}", 404)

    body = request.get_json(silent=True) or {}
    title = str(body.get("title") or "").strip()[:200] or doc_id
    save_json(doc_meta_path(doc_id), {"title": title})
    return jsonify({"title": title})


def _doc_files(doc_id):
    """Every file on disk for a source document, across whichever extension
    it was uploaded with."""
    return [DOCUMENTS_DIR / f"{doc_id}{ext}" for ext in (".pdf", ".docx", ".doc") if (DOCUMENTS_DIR / f"{doc_id}{ext}").exists()]


def _doc_norm_type(doc_id):
    if (DOCUMENTS_DIR / f"{doc_id}.pdf").exists():
        return "pdf"
    if (DOCUMENTS_DIR / f"{doc_id}.docx").exists() or (DOCUMENTS_DIR / f"{doc_id}.doc").exists():
        return "docx"
    return None


def _doc_has_annotations(doc_id, norm_type):
    data = load_json(annotations_path(doc_id, norm_type), default={})
    return isinstance(data, dict) and any(isinstance(v, list) and v for v in data.values())


def _doc_has_snippets(doc_id, norm_type):
    meta = load_json(snippets_meta_path(doc_id, norm_type), default=[])
    return isinstance(meta, list) and len(meta) > 0


@app.route("/api/document/<doc_id>", methods=["DELETE"])
def api_delete_document(doc_id):
    check_doc_id(doc_id)
    norm_type = _doc_norm_type(doc_id)
    if norm_type is None:
        raise DocumentError(f"No document with id {doc_id!r}", 404)

    if _doc_has_annotations(doc_id, norm_type):
        raise DocumentError("Cannot delete a document that has annotations. Remove them first.", 400)
    if _doc_has_snippets(doc_id, norm_type):
        raise DocumentError("Cannot delete a document that has snippets. Remove them first.", 400)

    for f in _doc_files(doc_id):
        f.unlink(missing_ok=True)
    (CACHE_DIR / f"{doc_id}.pdf").unlink(missing_ok=True)
    doc_meta_path(doc_id).unlink(missing_ok=True)
    annotations_path(doc_id, norm_type).unlink(missing_ok=True)
    snippets_meta_path(doc_id, norm_type).unlink(missing_ok=True)
    shutil.rmtree(snippets_dir(doc_id, norm_type), ignore_errors=True)

    unlink_document_from_hearings(doc_id)
    return jsonify({"ok": True})


@app.route("/api/doc/<doc_id>/render/<int:page>")
def api_render(doc_id, page):
    path, _ = resolve_pdf_path(doc_id, request.args.get("type", "pdf"))
    try:
        dpi = int(request.args.get("dpi", 150))
    except ValueError:
        dpi = 150
    dpi = max(50, min(dpi, 600))

    with fitz.open(path) as d:
        if not (1 <= page <= d.page_count):
            raise DocumentError("Page out of range", 404)
        zoom = dpi / 72
        pix = d[page - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        png_bytes = pix.tobytes("png")

    resp = Response(png_bytes, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# API: annotations (rectangles + freehand strokes)
# ---------------------------------------------------------------------------

@app.route("/api/doc/<doc_id>/annotations")
def api_all_annotations(doc_id):
    """All pages' annotation lists in one call, keyed by page number as a string.

    Used by the continuous-view toggle so opening a multi-page document doesn't
    require one GET per page; single-page mode doesn't need this.
    """
    check_doc_id(doc_id)
    norm_type = normalize_type(request.args.get("type", "pdf"))
    data = load_json(annotations_path(doc_id, norm_type), default={})
    return jsonify(data)


@app.route("/api/doc/<doc_id>/annotations/<int:page>", methods=["GET", "POST"])
def api_annotations(doc_id, page):
    check_doc_id(doc_id)
    norm_type = normalize_type(request.args.get("type", "pdf"))
    path = annotations_path(doc_id, norm_type)

    if request.method == "GET":
        data = load_json(path, default={})
        return jsonify(data.get(str(page), []))

    body = request.get_json(silent=True) or {}
    anns = body.get("annotations")
    if not isinstance(anns, list):
        raise DocumentError("Body must contain an 'annotations' list", 400)

    data = load_json(path, default={})
    data[str(page)] = anns
    save_json(path, data)
    return jsonify({"status": "ok", "count": len(anns)})


# ---------------------------------------------------------------------------
# API: rectangular snippet extraction
# ---------------------------------------------------------------------------

@app.route("/api/doc/<doc_id>/snippet/<int:page>", methods=["POST"])
def api_create_snippet(doc_id, page):
    raw_type = request.args.get("type", "pdf")
    pdf_path, norm_type = resolve_pdf_path(doc_id, raw_type)

    body = request.get_json(silent=True) or {}
    try:
        x, y, w, h = float(body["x"]), float(body["y"]), float(body["w"]), float(body["h"])
    except (KeyError, TypeError, ValueError):
        raise DocumentError("Body must contain numeric x, y, w, h fractions (0-1)", 400)
    if w <= 0 or h <= 0 or not (0 <= x <= 1 and 0 <= y <= 1):
        raise DocumentError("Invalid rectangle: x, y must be in [0,1] and w, h > 0", 400)

    try:
        dpi = int(body.get("dpi", 300))
    except (TypeError, ValueError):
        dpi = 300
    dpi = max(72, min(dpi, 900))

    annotations = sanitize_annotations(body.get("annotations"))

    with fitz.open(pdf_path) as d:
        if not (1 <= page <= d.page_count):
            raise DocumentError("Page out of range", 404)
        p = d[page - 1]
        pr = p.rect
        draw_annotations_on_page(p, annotations, pr)
        clip = fitz.Rect(
            pr.x0 + x * pr.width,
            pr.y0 + y * pr.height,
            pr.x0 + min(x + w, 1.0) * pr.width,
            pr.y0 + min(y + h, 1.0) * pr.height,
        ) & pr
        zoom = dpi / 72
        pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
        png_bytes = pix.tobytes("png")

    snippet_id = uuid.uuid4().hex[:12]
    out_dir = snippets_dir(doc_id, norm_type)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"p{page}_{snippet_id}.png"
    (out_dir / filename).write_bytes(png_bytes)

    meta_path = snippets_meta_path(doc_id, norm_type)
    meta = load_json(meta_path, default=[])
    entry = {
        "id": snippet_id,
        "page": page,
        "filename": filename,
        "rect": {"x": x, "y": y, "w": w, "h": h},
        "annotated": bool(annotations),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    meta.append(entry)
    save_json(meta_path, meta)

    result = dict(entry)
    result["url"] = url_for("api_snippet_file", doc_id=doc_id, filename=filename, type=raw_type)
    return jsonify(result)


@app.route("/api/doc/<doc_id>/download")
def api_download_annotated(doc_id):
    raw_type = request.args.get("type", "pdf")
    pdf_path, norm_type = resolve_pdf_path(doc_id, raw_type)

    data = load_json(annotations_path(doc_id, norm_type), default={})

    with fitz.open(pdf_path) as d:
        for page_key, raw_anns in data.items():
            try:
                page_num = int(page_key)
            except ValueError:
                continue
            if not (1 <= page_num <= d.page_count):
                continue
            annotations = sanitize_annotations(raw_anns)
            if not annotations:
                continue
            p = d[page_num - 1]
            draw_annotations_on_page(p, annotations, p.rect)
        pdf_bytes = d.tobytes(deflate=True)

    resp = Response(pdf_bytes, mimetype="application/pdf")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Disposition"] = f'attachment; filename="{doc_id}-annotated.pdf"'
    return resp


@app.route("/api/doc/<doc_id>/snippets")
def api_list_snippets(doc_id):
    check_doc_id(doc_id)
    norm_type = normalize_type(request.args.get("type", "pdf"))
    page = request.args.get("page", type=int)

    meta = load_json(snippets_meta_path(doc_id, norm_type), default=[])
    if page is not None:
        meta = [m for m in meta if m["page"] == page]
    raw_type = request.args.get("type", "pdf")
    for m in meta:
        m["url"] = url_for("api_snippet_file", doc_id=doc_id, filename=m["filename"], type=raw_type)
    return jsonify(meta)


@app.route("/api/doc/<doc_id>/snippet/<snippet_id>", methods=["DELETE"])
def api_delete_snippet(doc_id, snippet_id):
    check_doc_id(doc_id)
    norm_type = normalize_type(request.args.get("type", "pdf"))
    meta_path = snippets_meta_path(doc_id, norm_type)
    meta = load_json(meta_path, default=[])
    remaining = [m for m in meta if m["id"] != snippet_id]
    removed = [m for m in meta if m["id"] == snippet_id]
    if not removed:
        raise DocumentError(f"No snippet with id {snippet_id}", 404)
    save_json(meta_path, remaining)
    for m in removed:
        f = snippets_dir(doc_id, norm_type) / m["filename"]
        if f.exists():
            f.unlink()
    return jsonify({"status": "ok"})


@app.route("/api/reports", methods=["GET", "POST"])
def api_reports():
    if request.method == "GET":
        items = []
        for f in REPORTS_DIR.glob("*.json"):
            data = load_json(f, default=None)
            if not isinstance(data, dict):
                continue
            items.append({
                "id": f.stem,
                "name": data.get("name") or f.stem,
                "source_doc": data.get("source_doc", ""),
                "source_type": data.get("source_type", "pdf"),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
            })
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return jsonify(items)

    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:200]
    if not name:
        raise DocumentError("A report name is required", 400)

    source_doc = str(body.get("source_doc") or "")
    source_type = "pdf"
    if source_doc:
        check_doc_id(source_doc)
        source_type = normalize_type(body.get("source_type", "pdf"))

    report_id = f"{slugify_report_name(name)}-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "name": name,
        "doc": {"type": "doc", "content": []},
        "source_doc": source_doc,
        "source_type": source_type,
        "margins": dict(REPORT_DEFAULT_MARGINS),
        "pageNumbers": dict(REPORT_DEFAULT_PAGE_NUMBERS),
        "created_at": now,
        "updated_at": now,
    }
    save_json(report_path(report_id), data)
    return jsonify({"id": report_id, **data})


def unlink_report_from_evidence(report_id):
    """Strip references to a deleted report from every allegation's evidence
    lists, so a dangling report_id never lingers on an evidence card after
    the report it pointed at is gone."""
    for f in ALLEGATIONS_DIR.glob("*.json"):
        data = load_json(f, default=None)
        if not isinstance(data, dict):
            continue
        changed = False
        for kind in ("inculpatory", "exculpatory"):
            for item in data.get(kind) or []:
                if isinstance(item, dict) and item.get("report_id") == report_id:
                    item["report_id"] = ""
                    changed = True
        if changed:
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_json(f, data)


@app.route("/api/report/<report_id>", methods=["GET", "POST", "DELETE"])
def api_report(report_id):
    check_report_id(report_id)
    path = report_path(report_id)
    existing = load_json(path, default=None)
    if existing is None:
        raise DocumentError(f"No report with id {report_id!r}", 404)

    if request.method == "GET":
        resp = dict(existing)
        resp["margins"] = sanitize_margins(existing.get("margins"))
        resp["pageNumbers"] = sanitize_page_numbers(existing.get("pageNumbers"))
        return jsonify(resp)

    if request.method == "DELETE":
        path.unlink(missing_ok=True)
        unlink_report_from_evidence(report_id)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    name = str(body.get("name", existing.get("name", ""))).strip()[:200]
    if not name:
        raise DocumentError("A report name is required", 400)

    source_doc = str(body.get("source_doc", existing.get("source_doc", "")))
    source_type = "pdf"
    if source_doc:
        check_doc_id(source_doc)
        source_type = normalize_type(body.get("source_type", existing.get("source_type", "pdf")))

    doc_json = sanitize_report_doc(body.get("doc", existing.get("doc")))
    margins = sanitize_margins(body.get("margins"), existing.get("margins"))
    page_numbers = sanitize_page_numbers(body.get("pageNumbers"), existing.get("pageNumbers"))
    data = {
        "name": name,
        "doc": doc_json,
        "source_doc": source_doc,
        "source_type": source_type,
        "margins": margins,
        "pageNumbers": page_numbers,
        "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(path, data)
    return jsonify(data)


@app.route("/api/report/<report_id>/export")
def api_report_export(report_id):
    check_report_id(report_id)
    data = load_json(report_path(report_id), default=None)
    if data is None:
        raise DocumentError(f"No report with id {report_id!r}", 404)

    title = data.get("name") or report_id
    doc_json = data.get("doc")
    if doc_json is None:
        raise DocumentError("This report was saved by an older editor version — open and save it once to upgrade it before exporting", 400)
    if not doc_json.get("content"):
        raise DocumentError("Report is empty — add some content before exporting", 400)

    pdf_bytes = render_report_pdf(title, inline_doc_images(doc_json), data.get("margins"), data.get("pageNumbers"))

    resp = Response(pdf_bytes, mimetype="application/pdf")
    resp.headers["Cache-Control"] = "no-store"
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-") or report_id
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}.pdf"'
    return resp


@app.route("/api/report/<report_id>/export.docx")
def api_report_export_docx(report_id):
    check_report_id(report_id)
    data = load_json(report_path(report_id), default=None)
    if data is None:
        raise DocumentError(f"No report with id {report_id!r}", 404)

    title = data.get("name") or report_id
    doc_json = data.get("doc")
    if doc_json is None:
        raise DocumentError("This report was saved by an older editor version — open and save it once to upgrade it before exporting", 400)
    if not doc_json.get("content"):
        raise DocumentError("Report is empty — add some content before exporting", 400)

    docx_bytes = render_report_docx(title, inline_doc_images(doc_json), data.get("margins"), data.get("pageNumbers"))

    resp = Response(
        docx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    resp.headers["Cache-Control"] = "no-store"
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-") or report_id
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}.docx"'
    return resp


@app.route("/api/source-documents")
def api_source_documents():
    return jsonify(list_source_docs())


def count_linked_allegations():
    """Case id -> number of global allegations that link to it."""
    counts = {}
    for f in ALLEGATIONS_DIR.glob("*.json"):
        data = load_json(f, default=None)
        if not isinstance(data, dict):
            continue
        for cid in data.get("case_ids") or []:
            counts[cid] = counts.get(cid, 0) + 1
    return counts


@app.route("/api/allegation-cases", methods=["GET", "POST"])
def api_allegation_cases():
    if request.method == "GET":
        allegation_counts = count_linked_allegations()
        items = []
        for f in CASES_DIR.glob("*.json"):
            data = load_json(f, default=None)
            if not isinstance(data, dict):
                continue
            items.append({
                "id": f.stem,
                "name": data.get("name") or f.stem,
                "court": data.get("court", ""),
                "case_number": data.get("case_number", ""),
                "summary": data.get("summary", ""),
                "allegation_count": allegation_counts.get(f.stem, 0),
                "hearing_count": len(data.get("hearings") or []),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
            })
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return jsonify(items)

    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:200]
    if not name:
        raise DocumentError("A case name is required", 400)

    case_id = f"{slugify_report_name(name)}-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "name": name,
        "court": _sanitize_text(body.get("court"), CASE_MAX_COURT_CHARS),
        "case_number": _sanitize_text(body.get("case_number"), CASE_MAX_NUMBER_CHARS),
        "summary": _sanitize_text(body.get("summary"), ALLEGATION_MAX_TEXT_CHARS),
        "hearings": [],
        "created_at": now,
        "updated_at": now,
    }
    save_json(allegation_case_path(case_id), data)
    return jsonify({"id": case_id, **data})


@app.route("/api/allegation-case/<case_id>", methods=["GET", "POST", "DELETE"])
def api_allegation_case(case_id):
    check_report_id(case_id)
    path = allegation_case_path(case_id)
    existing = load_json(path, default=None)
    if existing is None:
        raise DocumentError(f"No case with id {case_id!r}", 404)

    if request.method == "GET":
        return jsonify(existing)

    if request.method == "DELETE":
        if existing.get("hearings"):
            raise DocumentError("Cannot delete a case that still has hearings", 400)
        path.unlink(missing_ok=True)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    name = str(body.get("name", existing.get("name", ""))).strip()[:200]
    if not name:
        raise DocumentError("A case name is required", 400)

    # Each caller (the allegations editor, the cases workspace) only ever
    # sends the fields it owns -- falling back to the value already on disk
    # for everything else (via dict.get's default, not truthiness) means one
    # page's save can never clobber the other's data.
    data = {
        "name": name,
        "court": _sanitize_text(body.get("court", existing.get("court", "")), CASE_MAX_COURT_CHARS),
        "case_number": _sanitize_text(body.get("case_number", existing.get("case_number", "")), CASE_MAX_NUMBER_CHARS),
        "summary": _sanitize_text(body.get("summary", existing.get("summary", "")), ALLEGATION_MAX_TEXT_CHARS),
        "hearings": sanitize_hearings(body.get("hearings", existing.get("hearings", []))),
        "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(path, data)
    return jsonify(data)


# ---------------------------------------------------------------------------
# API: global allegations (each optionally linked to one or more cases)
# ---------------------------------------------------------------------------

@app.route("/api/allegations", methods=["GET", "POST"])
def api_allegations():
    if request.method == "GET":
        by_id = {}
        for f in ALLEGATIONS_DIR.glob("*.json"):
            data = load_json(f, default=None)
            if not isinstance(data, dict):
                continue
            by_id[f.stem] = data
        creation_order = sorted(by_id, key=lambda i: by_id[i].get("created_at", ""))
        items = [{"id": i, **by_id[i]} for i in ordered_allegation_ids(creation_order)]
        return jsonify(items)

    body = request.get_json(silent=True) or {}
    allegation_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "title": _sanitize_text(body.get("title"), ALLEGATION_MAX_TITLE_CHARS),
        "description": _sanitize_text(body.get("description"), ALLEGATION_MAX_TEXT_CHARS),
        "inculpatory": sanitize_evidence_list(body.get("inculpatory")),
        "exculpatory": sanitize_evidence_list(body.get("exculpatory")),
        "case_ids": sanitize_case_ids(body.get("case_ids")),
        "created_at": now,
        "updated_at": now,
    }
    save_json(allegation_item_path(allegation_id), data)
    return jsonify({"id": allegation_id, **data})


@app.route("/api/allegation/<allegation_id>", methods=["GET", "POST", "DELETE"])
def api_allegation_item(allegation_id):
    check_report_id(allegation_id)
    path = allegation_item_path(allegation_id)
    existing = load_json(path, default=None)
    if existing is None:
        raise DocumentError(f"No allegation with id {allegation_id!r}", 404)

    if request.method == "GET":
        return jsonify({"id": allegation_id, **existing})

    if request.method == "DELETE":
        path.unlink(missing_ok=True)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    data = {
        "title": _sanitize_text(body.get("title", existing.get("title", "")), ALLEGATION_MAX_TITLE_CHARS),
        "description": _sanitize_text(body.get("description", existing.get("description", "")), ALLEGATION_MAX_TEXT_CHARS),
        "inculpatory": sanitize_evidence_list(body.get("inculpatory", existing.get("inculpatory", []))),
        "exculpatory": sanitize_evidence_list(body.get("exculpatory", existing.get("exculpatory", []))),
        "case_ids": sanitize_case_ids(body.get("case_ids", existing.get("case_ids", []))),
        "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(path, data)
    return jsonify({"id": allegation_id, **data})


@app.route("/api/allegations/order", methods=["POST"])
def api_allegations_order():
    body = request.get_json(silent=True) or {}
    raw_order = body.get("order")
    if not isinstance(raw_order, list):
        raise DocumentError("order must be a list of allegation ids", 400)
    seen = []
    for aid in raw_order[:2000]:
        if isinstance(aid, str) and DOC_ID_RE.match(aid) and aid not in seen and allegation_item_path(aid).exists():
            seen.append(aid)
    save_json(ALLEGATION_ORDER_PATH, seen)
    return jsonify({"order": seen})


@app.route("/media/snippets/<doc_id>/<path:filename>")
def api_snippet_file(doc_id, filename):
    check_doc_id(doc_id)
    norm_type = normalize_type(request.args.get("type", "pdf"))
    directory = snippets_dir(doc_id, norm_type)
    if "/" in filename or "\\" in filename:
        abort(400)
    return send_from_directory(directory, filename)


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 5050))
    app.run(host="127.0.0.1", port=port, debug=True)
