import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF
from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory, url_for

from converters import ConversionError, convert_to_pdf

BASE_DIR = Path(__file__).resolve().parent
DOCUMENTS_DIR = BASE_DIR / "documents"
STORAGE_DIR = BASE_DIR / "storage"
CACHE_DIR = STORAGE_DIR / "cache"
ANNOTATIONS_DIR = STORAGE_DIR / "annotations"
SNIPPETS_DIR = STORAGE_DIR / "snippets"

for d in (DOCUMENTS_DIR, CACHE_DIR, ANNOTATIONS_DIR, SNIPPETS_DIR):
    d.mkdir(parents=True, exist_ok=True)

DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_ANNOTATION_COLOR = "#e02424"

app = Flask(__name__)


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
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    pdf_ids = {f.stem for f in DOCUMENTS_DIR.glob("*.pdf")}
    docx_ids = {f.stem for f in DOCUMENTS_DIR.glob("*.docx")} | {f.stem for f in DOCUMENTS_DIR.glob("*.doc")}
    docs = [{"id": i, "type": "pdf"} for i in sorted(pdf_ids)]
    docs += [{"id": i, "type": "docx"} for i in sorted(docx_ids - pdf_ids)]
    docs.sort(key=lambda d: d["id"])
    return render_template("index.html", docs=docs)


@app.route("/annotations")
def page_view():
    doc_id = request.args.get("doc", "")
    raw_type = request.args.get("type", "pdf")
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

    return render_template(
        "annotations.html",
        doc_id=doc_id,
        doc_type=raw_type,
        norm_type=norm_type,
        page=page,
        page_count=page_count,
    )


# ---------------------------------------------------------------------------
# API: document info / rendering
# ---------------------------------------------------------------------------

@app.route("/api/doc/<doc_id>/info")
def api_doc_info(doc_id):
    path, _ = resolve_pdf_path(doc_id, request.args.get("type", "pdf"))
    with fitz.open(path) as d:
        pages = [{"width": p.rect.width, "height": p.rect.height} for p in d]
    return jsonify({"page_count": len(pages), "pages": pages})


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
