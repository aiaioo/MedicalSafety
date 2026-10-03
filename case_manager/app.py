import base64
import io
import json
import os
import re
import uuid
from datetime import datetime, timezone
from html import escape as html_escape, unescape
from pathlib import Path
from urllib.parse import urlsplit

import fitz  # PyMuPDF
import psycopg2.errors
from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.image.image import Image as DocxImage
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Pt, RGBColor
from PIL import Image, ImageOps, UnidentifiedImageError
from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import auth
import mailer
import storage
from cities import CITIES_BY_COUNTRY, DEFAULT_CITY_BY_COUNTRY

DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_ANNOTATION_COLOR = "#e02424"
# Text-box annotations: fitz base-14 font names, line height and inner padding
# (points) -- static/viewer.js uses the same values so the box wraps identically.
TEXT_ANNOTATION_FONTS = {"Helvetica": "helv", "Times": "tiro", "Courier": "cour"}
TEXT_ANNOTATION_DEFAULT_SIZE = 12
TEXT_ANNOTATION_LINE_HEIGHT = 1.2
TEXT_ANNOTATION_PAD = 3

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
REPORT_DEFAULT_PAGE_NUMBERS = {"position": "top-center", "skip": 0, "first": 1, "font": "Arial", "fontSize": 11, "shape": "none", "color": "#555555"}
ANNEXURE_DEFAULT_PAGE_NUMBERS = {**REPORT_DEFAULT_PAGE_NUMBERS, "position": "top-center", "shape": "circle", "color": "#777777"}
REPORT_PAGE_NUMBER_SHAPES = {"none", "circle", "rectangle"}
REPORT_PAGE_NUMBER_POSITIONS = {
    "top-left", "top-center", "top-right",
    "bottom-left", "bottom-center", "bottom-right",
    "none",
}
REPORT_PAGE_NUMBER_FONTS = {
    "Arial", "Georgia", "'Times New Roman'", "'Courier New'",
    "Verdana", "'Trebuchet MS'", "'Comic Sans MS'", "'Bookman Old Style'", "Calibri",
}
PAGE_NUMBER_TEXT_DROP = 0.18  # tuned so the number's glyphs sit optically centred in the outline
ANNEXURE_PAGE_NUMBER_BAND = 46  # pt of header/footer band the annexure page numbers sit in (the preview mirrors it)
REPORT_PAGE_NUMBER_SKIP_MAX = 50
REPORT_PAGE_NUMBER_FIRST_MAX = 100000
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

    first = raw.get("first") if isinstance(raw, dict) else None
    try:
        first = int(first)
    except (TypeError, ValueError):
        first = None
    if first is None or not (1 <= first <= REPORT_PAGE_NUMBER_FIRST_MAX):
        first = fallback.get("first")
        if not isinstance(first, int) or not (1 <= first <= REPORT_PAGE_NUMBER_FIRST_MAX):
            first = REPORT_DEFAULT_PAGE_NUMBERS["first"]

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

    shape = raw.get("shape") if isinstance(raw, dict) else None
    if shape not in REPORT_PAGE_NUMBER_SHAPES:
        shape = fallback.get("shape")
        if shape not in REPORT_PAGE_NUMBER_SHAPES:
            shape = REPORT_DEFAULT_PAGE_NUMBERS["shape"]

    color = raw.get("color") if isinstance(raw, dict) else None
    if not (isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
        color = fallback.get("color")
        if not (isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
            color = REPORT_DEFAULT_PAGE_NUMBERS["color"]

    return {"position": position, "skip": skip, "first": first, "font": font, "fontSize": font_size,
            "shape": shape, "color": color.lower()}

# Annexure document numbers: an optional "<name> <prefix><n>" label (e.g.
# "Annexure P-1") stamped in the top margin of each annexed document's first
# page, in the page numbers' font, size and colour.
ANNEXURE_DEFAULT_DOC_NUMBERS = {"enabled": True, "name": "Annexure", "prefix": "", "first": 1}
ANNEXURE_DOC_NUMBER_NAMES = ("Annexure", "Document", "Attachment")
ANNEXURE_DOC_NUMBER_PREFIX_MAX = 6


def sanitize_doc_numbers(raw, fallback=None):
    fallback = fallback if isinstance(fallback, dict) else ANNEXURE_DEFAULT_DOC_NUMBERS
    raw = raw if isinstance(raw, dict) else {}

    def pick(key, ok, convert=lambda v: v):
        for src in (raw, fallback):
            try:
                v = convert(src.get(key))
            except (TypeError, ValueError):
                continue
            if ok(v):
                return v
        return ANNEXURE_DEFAULT_DOC_NUMBERS[key]

    return {
        "enabled": bool(raw["enabled"]) if "enabled" in raw else bool(fallback.get("enabled", ANNEXURE_DEFAULT_DOC_NUMBERS["enabled"])),
        "name": pick("name", lambda v: v in ANNEXURE_DOC_NUMBER_NAMES),
        "prefix": pick("prefix", lambda v: isinstance(v, str) and len(v) <= ANNEXURE_DOC_NUMBER_PREFIX_MAX and v.isprintable(),
                       lambda v: v.strip() if isinstance(v, str) else v),
        "first": pick("first", lambda v: 1 <= v <= REPORT_PAGE_NUMBER_FIRST_MAX, int),
    }


def doc_number_label(dn, index):
    """The label of the `index`th (0-based) numbered document."""
    return f"{dn['name']} {dn['prefix']}{dn['first'] + index}"


def stamp_doc_numbers(doc, dn, pn, m, first_pages):
    """Stamps each document's label in the top margin band of its first page
    (`first_pages`: page indexes into `doc`, one per numbered document) --
    at the top right, or the top left when the page number is top right."""
    if not dn["enabled"] or m["header"] <= 0:
        return
    fs = pn["fontSize"]
    align = "left" if pn["position"] == "top-right" else "right"
    css = f"font-family: {pn['font']}, Helvetica, Arial, sans-serif; font-size: {fs}pt; color: {pn['color']}; margin: 0; text-align: {align};"
    box_h = fs * 1.6
    for n, i in enumerate(first_pages):
        r = doc[i].rect
        left, right = r.x0 + m["left"], r.x1 - m["right"]
        half = (left, (left + right) / 2) if align == "left" else ((left + right) / 2, right)
        mid = r.y0 + m["header"] / 2
        box = fitz.Rect(half[0], mid - box_h / 2, half[1], mid + box_h / 2)
        doc[i].insert_htmlbox(box + (0, PAGE_NUMBER_TEXT_DROP * fs, 0, PAGE_NUMBER_TEXT_DROP * fs),
                              f'<p style="{css}">{html_escape(doc_number_label(dn, n))}</p>')


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB, generous for scanned case files
# Behind a reverse proxy every request arrives from the proxy's address, so
# take the client's from X-Forwarded-For -- but only as many proxies' worth as
# TRUSTED_PROXY_HOPS says (0 = none, e.g. local development), since a client
# can put anything it likes in that header. X-Forwarded-Proto is trusted the
# same way so absolute links (e.g. share-key URLs, built from the domain the
# visitor used) come out as https://, not http://.
_proxy_hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))
if _proxy_hops:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxy_hops, x_proto=_proxy_hops)
# Sign-up/sign-in/sign-out, plus a before-request gate that keeps every other
# route behind a signed-in session and sets g.user -- see auth.py.
app.register_blueprint(auth.bp)

UPLOAD_EXTENSIONS = {".pdf": "pdf"}


class DocumentError(Exception):
    def __init__(self, message, status=404, unlock=None, signin=False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.unlock = unlock  # (kind, object id) whose key prompt to offer with the message
        self.signin = signin  # send a page request to the sign-in page rather than show the error


@app.errorhandler(storage.DeleteBlocked)
def handle_delete_blocked(err):
    return handle_document_error(DocumentError(str(err), 400))


@app.errorhandler(DocumentError)
def handle_document_error(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": err.message}), err.status
    next_url = request.full_path if request.query_string else request.path
    if err.signin:
        return redirect(url_for("auth.signin", next=next_url))
    if err.unlock:
        return auth.render_unlock(*err.unlock, next_url, err.message, err.status)
    return render_template("error.html", message=err.message), err.status


# ---------------------------------------------------------------------------
# Per-object access (see db/migrations/001_users_and_access.sql). A viewer
# can read an object, an editor can also change it -- and, for a cause,
# create cases under it; for a cause or case, create reports / upload
# sources associated with it --
# and only its owner -- whoever created it -- can delete it. A user with no
# role on an object gets the same 404 as for one that doesn't exist, so ids
# can't be probed for.
# ---------------------------------------------------------------------------
ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}
ACCESS_LABELS = {"cause": "cause", "case": "case", "report": "report", "source": "document", "allegation": "allegation"}


def raise_no_access(kind, object_id, message):
    """The 404 for an object the user can't reach -- except that a guest
    whose key for it has been switched off or deleted is told so."""
    if g.user.is_guest:
        if storage.has_deactivated_key(-g.user.id, kind, object_id):
            raise DocumentError(auth.KEY_DEACTIVATED_MESSAGE, 403)
        if storage.has_deleted_key(-g.user.id, kind, object_id):
            # Offer the key prompt too, if the object has other keys to try.
            unlock = (kind, object_id) if storage.object_has_keys(kind, object_id) else None
            raise DocumentError(auth.KEY_DELETED_MESSAGE, 403, unlock=unlock)
        # A key holder with no access and no key of theirs to blame: offer the
        # key prompt if the object has keys, otherwise sign in.
        if storage.object_has_keys(kind, object_id):
            raise DocumentError(message, 404, unlock=(kind, object_id))
        raise DocumentError(message, 404, signin=True)
    raise DocumentError(message, 404)


def has_role(kind, object_id, min_role):
    role = storage.get_role(g.user.id, kind, object_id)
    return role is not None and ROLE_RANK[role] >= ROLE_RANK[min_role]


def require_role(kind, object_id, min_role="viewer"):
    label = ACCESS_LABELS[kind]
    role = storage.get_role(g.user.id, kind, object_id)
    if role is None:
        raise_no_access(kind, object_id, f"No {label} with id {object_id!r}")
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        action = "delete" if min_role == "owner" else "change"
        raise DocumentError(f"You don't have permission to {action} this {label}", 403)
    return role


def require_allegation_role(allegation, min_role="viewer"):
    """An allegation is governed by the better of the user's role on its
    cause and their role on the allegation itself (a collaborator may be
    given one without the other)."""
    roles = [storage.get_role(g.user.id, "cause", allegation["cause_id"]),
             storage.get_role(g.user.id, "allegation", allegation["id"])]
    best = max((r for r in roles if r), key=ROLE_RANK.get, default=None)
    if best is None:
        raise_no_access("allegation", allegation["id"], f"No allegation with id {allegation['id']!r}")
    if ROLE_RANK[best] < ROLE_RANK[min_role]:
        raise DocumentError("You don't have permission to change this allegation", 403)
    return best


def allegation_roles(allegations):
    """{allegation id: role} -- the better of the user's roles on the
    allegation's cause and on the allegation itself (as in
    require_allegation_role)."""
    cause_roles = {c["id"]: c["role"] for c in storage.list_causes(g.user.id)}
    roles = {}
    for a in allegations:
        candidates = [cause_roles.get(a["cause_id"])]
        if not any(r in ("owner", "editor") for r in candidates):
            candidates.append(storage.get_role(g.user.id, "allegation", a["id"]))
        roles[a["id"]] = max((r for r in candidates if r), key=ROLE_RANK.get, default="viewer")
    return roles


def with_allegation_roles(allegations):
    roles = allegation_roles(allegations)
    return [{**a, "role": roles[a["id"]]} for a in allegations]


def visible_allegations():
    return storage.list_allegations(storage.accessible_ids(g.user.id, "cause"), storage.accessible_ids(g.user.id, "allegation"))


def require_link_target(fields, what):
    """The one cause or case a report/source is being associated with,
    from `fields` (a JSON body or form) carrying exactly one of cause_id /
    case_id -- the user must be able to edit it. Returns ("cause" | "case",
    id), the shape storage's link functions take."""
    cause_id, case_id = fields.get("cause_id"), fields.get("case_id")
    if bool(cause_id) == bool(case_id):
        raise DocumentError(f"Choose one cause or case to associate {what} with", 400)
    target_kind, target_id = ("cause", cause_id) if cause_id else ("case", case_id)
    if not isinstance(target_id, str) or not DOC_ID_RE.match(target_id):
        raise DocumentError(f"Invalid {target_kind} id: {target_id!r}", 400)
    require_role(target_kind, target_id, "editor")
    return target_kind, target_id


def can_create_items():
    """Whether this user can create new causes' children (cases,
    allegations, reports, uploads): anyone signed in can, since a cause is
    made on the fly; a guest (key session) only if the key gave them a
    cause they can edit."""
    return not g.user.is_guest or resolve_default_cause_id(create=False) is not None


def editable_cases():
    """{"id", "name"} of every case the user can edit, most recently
    updated first."""
    cases = [c for c in storage.list_cases(g.user.id) if c["role"] in ("owner", "editor")]
    cases.sort(key=lambda c: c["updated_at"], reverse=True)
    return [{"id": c["id"], "name": c["name"] or c["id"]} for c in cases]


def editable_causes():
    """{"id", "title"} of every cause the user can edit, most recently
    updated first."""
    causes = [c for c in storage.list_causes(g.user.id) if c["role"] in ("owner", "editor")]
    causes.sort(key=lambda c: c["updated_at"], reverse=True)
    return [{"id": c["id"], "title": c["title"] or c["id"]} for c in causes]


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


def add_cause_titles(items, only_cause=None):
    """Sets item["cause_titles"] on each report/document dict: the titles of
    the causes it is associated with directly (cause_ids) or through a
    linked case (case_ids), sorted, for display on its card. With
    `only_cause` (a cause id, or a collection of them), returns just the
    items associated with any of those causes. Also sets item["all_cause_ids"]."""
    only = {only_cause} if isinstance(only_cause, str) else set(only_cause or ())
    titles = {c["id"]: c["title"] or c["id"] for c in storage.list_causes(g.user.id)}
    case_cause = {c["id"]: c["cause_id"] for c in storage.list_cases(g.user.id)}
    kept = []
    for item in items:
        ids = set(item.get("cause_ids", ())) | {case_cause[i] for i in item.get("case_ids", ()) if i in case_cause}
        if only and not only & ids:
            continue
        item["all_cause_ids"] = sorted(ids)
        item["cause_titles"] = sorted(titles[i] for i in ids if i in titles)
        kept.append(item)
    return kept


def default_cause_only(create=False):
    """The id to filter the reports/documents lists by when the request
    carries ?default_cause=1 (the cause picked in the title bar), else None."""
    return resolve_default_cause_id(create=create) if request.args.get("default_cause") else None


def list_source_docs(only_cause=None):
    """All uploaded source documents (pdf/docx/doc), as {"id", "type",
    "title"} dicts -- the same shape rendered into the annotations and
    reports pages' source pickers."""
    docs = storage.list_documents(g.user.id)
    for d in docs:
        d["type"] = normalize_type(d["type"])
    return add_cause_titles(docs, only_cause)


def _get_pdf_bytes(doc_id, raw_type, min_role="viewer"):
    """Validates doc_id/raw_type and the user's access to that document, and
    returns (pdf_bytes, norm_type) -- storage.get_document_pdf_bytes handles
    fetching the right bytes and, for a Word document, converting+caching a
    PDF rendering of it; this is just the existence/access check and error
    translation for callers who don't already know the document exists."""
    check_doc_id(doc_id)
    norm_type = normalize_type(raw_type)
    require_role("source", doc_id, min_role)
    if storage.get_document_type(doc_id) is None:
        raise DocumentError(f"No document with id {doc_id!r}", 404)
    try:
        return storage.get_document_pdf_bytes(doc_id), norm_type
    except storage.StorageError as exc:
        raise DocumentError(str(exc), 500) from exc


def slugify_report_name(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:50] or "document"


def require_editable_cause(raw):
    """A cause a case is being put under -- the user must be able to edit
    it (a case is a child of its cause)."""
    if not isinstance(raw, str) or not DOC_ID_RE.match(raw):
        raise DocumentError(f"Invalid cause id: {raw!r}", 400)
    require_role("cause", raw, "editor")
    return raw


def create_default_cause():
    if g.user.is_guest:
        raise DocumentError("Guests can't create new items. Sign in with an account to do that.", 403)
    return storage.create_general_cause(g.user.id)


def resolve_default_cause_id(exclude_cause_id=None, create=True):
    """The user's default cause (see users.default_cause_id) -- the cause
    they last selected, as long as they can still edit it; else the most
    recently updated cause they can edit; else (when `create`) a freshly
    created "General" cause they own, so a case or new report/source is
    never left without one. `exclude_cause_id` is passed when reassigning
    cases off a cause that's about to be deleted, so that cause is never
    offered back as its own replacement."""
    default = storage.get_default_cause(g.user.id)
    if default and default != exclude_cause_id and has_role("cause", default, "editor"):
        return default
    fallback = storage.most_recently_updated_editable_cause_id(g.user.id, exclude=exclude_cause_id)
    return fallback or (create_default_cause() if create else None)


def default_cause_link():
    """The association every newly created report or uploaded source gets:
    the user's default cause. Whatever resolve_default_cause_id fell back to
    is saved as the default, so it stays put from then on."""
    cause_id = resolve_default_cause_id()
    storage.set_default_cause(g.user.id, cause_id)
    return "cause", cause_id


def default_cause_for_display():
    """{"id", "title"} of the cause new reports/uploads will be associated
    with, for showing on the page -- None if one would have to be created
    first (resolving it here, on a GET, never creates anything)."""
    cause_id = resolve_default_cause_id(create=False)
    cause = storage.get_cause(cause_id) if cause_id else None
    return {"id": cause_id, "title": cause["title"] or cause_id} if cause else None


# The site's display name follows the domain the visitor used (the same app
# and database serve every domain); unknown hosts, e.g. localhost, get the default.
SITE_NAMES = {"medicalsafety.in": "Medical Safety", "caseplan.in": "Case Plan"}
SITE_URLS = {"Medical Safety": "https://medicalsafety.in", "Case Plan": "https://caseplan.in"}
DEFAULT_SITE_NAME = "Case Plan"


@app.context_processor
def inject_site_name():
    host = request.host.split(":")[0].lower().removeprefix("www.")
    name = SITE_NAMES.get(host, DEFAULT_SITE_NAME)
    other = next(n for n in SITE_URLS if n != name)
    return {"site_name": name, "other_site_name": other, "other_site_url": SITE_URLS[other]}


@app.context_processor
def inject_cause_picker():
    """The title bar's cause picker (templates/_cause_picker.html): the
    causes this user can edit -- only those can be a default -- and which
    one is the default now."""
    if not getattr(g, "user", None) or g.user.is_guest:
        return {}
    causes = [
        {"id": c["id"], "title": c["title"] or c["id"]}
        for c in storage.list_causes(g.user.id) if c["role"] in ("owner", "editor")
    ]
    # Every user has a default cause: if theirs is gone (e.g. it was deleted
    # from under them by a collaborator), pick or create one and keep it.
    current = resolve_default_cause_id()
    if current != storage.get_default_cause(g.user.id):
        storage.set_default_cause(g.user.id, current)
    if current not in {c["id"] for c in causes}:
        causes = [{"id": current, "title": (storage.get_cause(current) or {}).get("title") or current}, *causes]
    return {"picker_causes": causes, "picker_current": current}


# ---------------------------------------------------------------------------
# Allegations workspace: allegations are standalone records (see
# templates/allegations.html, static/allegations.js), each carrying its own
# ordered inculpatory/exculpatory evidence lists, an ordered "to_prove" list
# (each item optionally linking to one or more evidence items from those
# same lists), plus an optional set of linked case ids -- an allegation can
# pertain to zero or more cases rather than belonging to exactly one, so the
# link lives on the allegation, not nested inside a case file. Plain text
# only (no rich formatting), so sanitizing is just shape/length
# whitelisting -- unlike sanitize_report_doc there's no HTML/ProseMirror
# tree to walk.
# ---------------------------------------------------------------------------
ALLEGATION_MAX_TITLE_CHARS = 300
ALLEGATION_MAX_TEXT_CHARS = 10_000
EVIDENCE_MAX_ITEMS = 300
CASE_MAX_COURT_CHARS = 200
CASE_MAX_NUMBER_CHARS = 100
CASE_ROLES = ("Complainant", "Plaintiff", "Appellant", "Defendant", "Petitioner", "Claimant", "Respondent")
CASE_MAX_PARTIES = 50
CASE_MAX_PARTY_CHARS = 300
CAUSE_TITLE_FONTS = ("", "Times New Roman", "Georgia", "Garamond", "Arial", "Helvetica", "Verdana", "Courier New", "Bookman Old Style", "Calibri")
CAUSE_TITLE_MIN_FONT_SIZE, CAUSE_TITLE_MAX_FONT_SIZE = 6, 72
CAUSE_TITLE_TEMPLATE_MAX_CHARS = 20000
CASE_MAX_DATE_CHARS = 40
HEARING_MAX_ITEMS = 300
HEARING_DOC_MAX_ITEMS = 100
ALLEGATION_MAX_CASE_LINKS = 50


def _sanitize_item_id(raw):
    return raw[:64] if isinstance(raw, str) and raw else uuid.uuid4().hex[:12]


def _sanitize_text(raw, max_chars):
    return raw.strip()[:max_chars] if isinstance(raw, str) else ""


def sanitize_parties(raw, existing):
    """The case's parties, in order. `raw` is a list of {id, side, name};
    entries with no name or an unknown side are dropped."""
    if raw is None:
        return existing
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:CASE_MAX_PARTIES]:
        if not isinstance(item, dict) or item.get("side") not in ("complainant", "respondent"):
            continue
        name = _sanitize_text(item.get("name"), CASE_MAX_PARTY_CHARS)
        if not name:
            continue
        # Fresh ids on every save: party ids are global primary keys, so
        # never trust one sent by the client.
        out.append({"id": uuid.uuid4().hex[:12], "side": item["side"], "name": name})
    return out


def sanitize_cause_title_settings(body, existing):
    """Court location, template choice and font for the generated cause
    title -- each falls back to the saved value when not sent."""
    font = body.get("cause_title_font", existing.get("cause_title_font", ""))
    size = body.get("cause_title_font_size", existing.get("cause_title_font_size", 0))
    template_id = body.get("cause_title_template_id", existing.get("cause_title_template_id"))
    # A hand-edited cause title (ProseMirror doc); null/absent-with-no-saved
    # one means "generate it from the template".
    doc = body["cause_title_doc"] if "cause_title_doc" in body else existing.get("cause_title_doc")
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError:
            doc = None
    doc = sanitize_report_doc(doc, CAUSE_TITLE_TEMPLATE_MAX_CHARS) if isinstance(doc, dict) else None
    known_ids = {t["id"] for t in storage.list_cause_title_templates()}
    role = body.get("case_role", existing.get("case_role", CASE_ROLES[0]))
    return {
        "case_role": role if role in CASE_ROLES else CASE_ROLES[0],
        "party_in_person": bool(body.get("party_in_person", existing.get("party_in_person", False))),
        "court_location": _sanitize_text(body.get("court_location", existing.get("court_location", "")), CASE_MAX_COURT_CHARS),
        "cause_title_template_id": template_id if template_id in known_ids else None,
        "cause_title_one_line_parties": bool(body.get("cause_title_one_line_parties", existing.get("cause_title_one_line_parties", False))),
        "cause_title_doc": json.dumps(doc, separators=(",", ":")) if doc is not None else None,
        "cause_title_font": font if font in CAUSE_TITLE_FONTS else "",
        "cause_title_font_size": size if isinstance(size, int) and not isinstance(size, bool)
                                 and CAUSE_TITLE_MIN_FONT_SIZE <= size <= CAUSE_TITLE_MAX_FONT_SIZE else 0,
    }


def linkable_ids(kind, already_linked=()):
    """Ids a save may link to: anything of that kind the user can view, plus
    whatever the record already linked (possibly added by a collaborator
    with wider access) -- so a user can never newly link something they
    can't see, and saving never silently drops someone else's links."""
    return storage.accessible_ids(g.user.id, kind) | set(already_linked)


def sanitize_evidence_list(raw, allowed_report_ids):
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
        has_report = (isinstance(report_id, str) and report_id in allowed_report_ids
                      and DOC_ID_RE.match(report_id) and storage.report_exists(report_id))
        out.append({
            "id": _sanitize_item_id(item.get("id")),
            "text": _sanitize_text(item.get("text"), ALLEGATION_MAX_TEXT_CHARS),
            "report_id": report_id if has_report else "",
        })
    return out


def sanitize_to_prove_list(raw, valid_evidence_ids):
    """Each "to prove" item can link to zero or more evidence items from the
    same allegation; `valid_evidence_ids` is the id set of that allegation's
    freshly-sanitized inculpatory+exculpatory lists, so a link to evidence
    that was deleted (in this save or an earlier one) is dropped rather than
    left dangling."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:EVIDENCE_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        evidence_ids = item.get("evidence_ids")
        linked = []
        if isinstance(evidence_ids, list):
            for eid in evidence_ids[:EVIDENCE_MAX_ITEMS]:
                if isinstance(eid, str) and eid in valid_evidence_ids and eid not in linked:
                    linked.append(eid)
        out.append({
            "id": _sanitize_item_id(item.get("id")),
            "title": _sanitize_text(item.get("title"), ALLEGATION_MAX_TITLE_CHARS),
            "summary": _sanitize_text(item.get("summary"), ALLEGATION_MAX_TEXT_CHARS),
            "evidence_ids": linked,
        })
    return out


def sanitize_case_ids(raw, allowed_case_ids):
    """Keep only ids that look valid, are in `allowed_case_ids` (see
    linkable_ids), and name a case that actually exists, so a link never
    points at something the cases workspace can't resolve."""
    if not isinstance(raw, list):
        return []
    out = []
    for cid in raw[:ALLEGATION_MAX_CASE_LINKS]:
        if (isinstance(cid, str) and cid in allowed_case_ids and DOC_ID_RE.match(cid)
                and cid not in out and storage.case_exists(cid)):
            out.append(cid)
    return out


def _source_doc_exists(doc_id, doc_type):
    if not isinstance(doc_id, str) or not DOC_ID_RE.match(doc_id):
        return False
    actual_type = storage.get_document_type(doc_id)
    if actual_type is None:
        return False
    wanted_family = "docx" if doc_type in ("docx", "doc", "word") else "pdf"
    return normalize_type(actual_type) == wanted_family


def sanitize_hearing_doc_list(raw, allowed_doc_ids):
    """A hearing's submitted/received document list -- each entry links to an
    uploaded source document (storage/<owner>/documents/), not a report. Drop entries whose
    document no longer exists (or isn't in `allowed_doc_ids`, see
    linkable_ids) rather than storing a dangling reference."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:HEARING_DOC_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        doc_id = item.get("doc_id")
        doc_type = "docx" if item.get("doc_type") in ("docx", "doc", "word") else "pdf"
        if doc_id not in allowed_doc_ids or not _source_doc_exists(doc_id, doc_type):
            continue
        out.append({
            "id": _sanitize_item_id(item.get("id")),
            "doc_id": doc_id,
            "doc_type": doc_type,
        })
    return out


def sanitize_hearings(raw, allowed_doc_ids):
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
            "submitted_docs": sanitize_hearing_doc_list(item.get("submitted_docs"), allowed_doc_ids),
            "received_docs": sanitize_hearing_doc_list(item.get("received_docs"), allowed_doc_ids),
        })
    return out


# ---------------------------------------------------------------------------
# Causes workspace: a cause is just a title/description (see
# api_causes below) holding an ordered list of goals. Each goal is likewise
# only a title/description, plus an optional set of linked case ids -- the
# same many-to-many shape allegations use for their case links, so a goal
# can pertain to zero or more cases.
# ---------------------------------------------------------------------------
CAUSE_MAX_GOAL_ITEMS = 300


def sanitize_goals(raw, allowed_case_ids):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:CAUSE_MAX_GOAL_ITEMS]:
        if not isinstance(item, dict):
            continue
        out.append({
            "id": _sanitize_item_id(item.get("id")),
            "title": _sanitize_text(item.get("title"), ALLEGATION_MAX_TITLE_CHARS),
            "description": _sanitize_text(item.get("description"), ALLEGATION_MAX_TEXT_CHARS),
            "case_ids": sanitize_case_ids(item.get("case_ids"), allowed_case_ids),
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

        if item.get("kind") in ("rect", "blackout"):
            kind = item["kind"]
            try:
                x, y, w, h = (_clamp01(item[k]) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError):
                continue
            if w <= 0 or h <= 0:
                continue
            out.append({"kind": kind, "color": color, "x": x, "y": y, "w": w, "h": h})

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

        elif item.get("kind") == "text":
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                x, y, w, h = (_clamp01(item[k]) for k in ("x", "y", "w", "h"))
                size = float(item.get("size", TEXT_ANNOTATION_DEFAULT_SIZE))
            except (KeyError, TypeError, ValueError):
                continue
            if w <= 0 or h <= 0:
                continue
            font = item.get("font")
            if font not in TEXT_ANNOTATION_FONTS:
                font = "Helvetica"
            size = min(72.0, max(4.0, size))
            out.append({"kind": "text", "color": color, "x": x, "y": y, "w": w, "h": h,
                        "text": text[:5000], "font": font, "size": size})
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
        elif a["kind"] == "blackout":
            r = fitz.Rect(
                page_rect.x0 + a["x"] * page_rect.width,
                page_rect.y0 + a["y"] * page_rect.height,
                page_rect.x0 + (a["x"] + a["w"]) * page_rect.width,
                page_rect.y0 + (a["y"] + a["h"]) * page_rect.height,
            )
            shape.draw_rect(r)
            shape.finish(color=color, fill=color, width=0)
        elif a["kind"] == "freehand":
            pts = [
                fitz.Point(page_rect.x0 + px * page_rect.width, page_rect.y0 + py * page_rect.height)
                for px, py in a["points"]
            ]
            shape.draw_polyline(pts)
            shape.finish(color=color, width=line_width, closePath=False)
        elif a["kind"] == "text":
            r = fitz.Rect(
                page_rect.x0 + a["x"] * page_rect.width,
                page_rect.y0 + a["y"] * page_rect.height,
                page_rect.x0 + (a["x"] + a["w"]) * page_rect.width,
                page_rect.y0 + (a["y"] + a["h"]) * page_rect.height,
            )
            shape.draw_rect(r)
            shape.finish(color=color, width=line_width * 0.5)
            inner = fitz.Rect(r.x0 + TEXT_ANNOTATION_PAD, r.y0 + TEXT_ANNOTATION_PAD,
                              r.x1 - TEXT_ANNOTATION_PAD, r.y1 - TEXT_ANNOTATION_PAD)
            # A negative return means the text overflowed; grow the box downwards
            # (the client normally already sized it to fit).
            for _ in range(20):
                rc = shape.insert_textbox(
                    inner, a["text"], fontname=TEXT_ANNOTATION_FONTS[a["font"]], fontsize=a["size"],
                    color=color, lineheight=TEXT_ANNOTATION_LINE_HEIGHT)
                if rc >= 0:
                    break
                inner.y1 += a["size"] * TEXT_ANNOTATION_LINE_HEIGHT
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
            if t == "paragraph" and attrs.get("tight") is True:
                clean_attrs["tight"] = True
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
            if attrs.get("refStyle") in ("both", "reference"):
                clean_attrs["refStyle"] = attrs["refStyle"]
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


def annexure_ref_texts(report_id):
    return annexure_ref_data(report_id)[0]


def annexure_ref_data(report_id):
    """(texts, positions, total): texts is {document id: {source page (int): "Annexure P-1, page 3"}} for every
    page the report's annexure includes, as the annexure numbers them now (page numbers are left off when the
    annexure is unnumbered); positions maps (document id, source page) to that page's 0-based index among the
    annexure's document pages (not counting the List of Documents); total is how many such pages there are."""
    payload = _annexure_payload(report_id, storage.get_annexure(report_id))
    dn, pn = payload["docNumbers"], payload["pageNumbers"]
    refs, positions, shown, numbered = {}, {}, 0, 0
    for d in payload["documents"]:
        pdf_bytes, _ = _get_pdf_bytes(d["id"], d["type"])
        with fitz.open(stream=pdf_bytes, filetype="pdf") as src:
            pages = list(annexure_selected_pages(d, src.page_count))
        if not pages:
            continue
        label = doc_number_label(dn, numbered) if dn["enabled"] else (d["description"] or d["title"]).strip()
        numbered += 1
        texts = refs[d["id"]] = {}
        for i, p in enumerate(pages):
            number = annexure_page_range(shown + i, shown + i, pn) if pn["position"] != "none" else "-"
            texts.setdefault(p, label if number == "-" else f"{label}, page {number}")
            positions.setdefault((d["id"], p), shown + i)
        shown += len(pages)
    return refs, positions, shown


ANNEX_REF_MISSING = "[not in annexure]"
SNIPPET_SRC_RE = re.compile(r"/media/snippets/([^/]+)/p(\d+)_[^/]*$")


def snippet_source(src):
    """(document id, source page) of a snippet image's `src`, or None if it isn't one."""
    m = SNIPPET_SRC_RE.search(urlsplit(src or "").path)
    return (m.group(1), int(m.group(2))) if m else None


def resolve_snippet_refs(node, refs, links=False):
    """`node` with each snippet image that has a refStyle replaced by what that style shows: the image and a
    paragraph of its annexure reference ("both"), or just the paragraph ("reference"), aligned like the image.
    `refs` is as from annexure_ref_texts. With `links`, the images and reference paragraphs that point at an annexed
    page carry an "annexLink" attr ("<document id>:<page>") for the PDF to turn into a link to that page."""
    if not isinstance(node, dict) or "content" not in node:
        return node
    content = []
    for child in node["content"]:
        attrs = child.get("attrs") or {} if isinstance(child, dict) else {}
        source = snippet_source(attrs.get("src")) if child.get("type") == "image" else None
        if source is None:
            content.append(resolve_snippet_refs(child, refs, links))
            continue
        text = refs.get(source[0], {}).get(source[1])
        link = {"annexLink": f"{source[0]}:{source[1]}"} if links and text else {}
        if attrs.get("refStyle") not in ("both", "reference"):
            content.append({**child, "attrs": {**attrs, **link}})
            continue
        if attrs["refStyle"] == "both":
            content.append({**child, "attrs": {**attrs, **link}})
        content.append({"type": "paragraph", "attrs": {"textAlign": attrs.get("align", "left")},
                        "content": [{"type": "text", "text": text or ANNEX_REF_MISSING, **link}]})
    return {**node, "content": content}


def inline_doc_images(node):
    """Replaces image nodes' `/media/snippets/...` (or `/media/report-images/...`)
    src with a base64 data URI read directly from storage, so the exported PDF/DOCX is self-contained --
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
            if (DOC_ID_RE.match(url_doc_id) and "/" not in filename and "\\" not in filename
                    and storage.can_view_snippet_images(g.user.id, url_doc_id)):
                data = storage.read_snippet_bytes(url_doc_id, filename)
                if data is not None:
                    b64 = base64.b64encode(data).decode("ascii")
                    attrs["src"] = f"data:image/png;base64,{b64}"
        elif parsed.path.startswith("/media/report-images/") and len(parts) >= 3:
            url_report_id, image_id = parts[-2], parts[-1]
            image = storage.get_report_image(image_id) if DOC_ID_RE.match(image_id) else None
            if image is not None and image["report_id"] == url_report_id and has_role("report", url_report_id, "viewer"):
                b64 = base64.b64encode(image["data"]).decode("ascii")
                attrs["src"] = f"data:{image['content_type']};base64,{b64}"
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


def _json_inline_to_html(nodes, raw_tabs=False):
    """Renders a run of inline JSON nodes (text/hardBreak) to an HTML
    string, applying each text node's marks -- the JSON equivalent of the
    nested <strong>/<em>/... tags the old contenteditable editor produced,
    just read from a flat `marks` list instead of tag nesting."""
    out = []
    for node in nodes:
        t = node.get("type")
        if t == "text":
            html = html_escape(node.get("text", ""))
            if not raw_tabs:
                html = html.replace("\t", "&nbsp;" * 6)  # a tab is 40px wide; HTML collapses a raw tab
            href = f"#snipref:{node['annexLink']}" if node.get("annexLink") else None
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
    if attrs.get("tight"):
        decls.append("margin-top: 0; margin-bottom: 0")
    return f' style="{html_escape("; ".join(decls), quote=True)}"' if decls else ""


def _json_blocks_to_html(nodes, num_state=None, depth=0, raw_tabs=False):
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
            out.append(f"<h{level}{_json_block_style(attrs)}>{_json_inline_to_html(content, raw_tabs)}</h{level}>")
        elif t == "paragraph":
            out.append(f"<p{_json_block_style(attrs)}>{_json_inline_to_html(content, raw_tabs)}</p>")
        elif t == "blockquote":
            out.append(f"<blockquote>{_json_blocks_to_html(content, depth=depth, raw_tabs=raw_tabs)}</blockquote>")
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
            img = f'<img src="{html_escape(attrs.get("src", ""), quote=True)}"{style}>'
            if attrs.get("annexLink"):
                img = f'<a href="#snipimg:{html_escape(attrs["annexLink"], quote=True)}">{img}</a>'
            out.append(f'<p class="img-p" style="text-align:{align}">{img}</p>')
        elif t == "bulletList":
            out.append(f"<ul>{_json_blocks_to_html(content, depth=depth + 1, raw_tabs=raw_tabs)}</ul>")
        elif t == "orderedList":
            child_depth = depth + 1
            child_state = num_state or _ListNumberingState(attrs.get("numCascade"), attrs.get("numLevels"))
            child_state.enter_list(child_depth, attrs.get("start"))
            out.append(f"<ol>{_json_blocks_to_html(content, num_state=child_state, depth=child_depth, raw_tabs=raw_tabs)}</ol>")
        elif t == "listItem":
            marker = ""
            if num_state is not None:
                num_state.next_value(depth)
                marker = html_escape(num_state.marker_text(depth))
            first = content[0] if content else None
            rest = content[1:]
            first_html = _json_inline_to_html(first.get("content") or [], raw_tabs) if first and first.get("type") in ("paragraph", "heading") else ""
            out.append(f"<li>{marker}{first_html}{_json_blocks_to_html(rest, num_state=num_state, depth=depth, raw_tabs=raw_tabs)}</li>")
    return "".join(out)


def _flatten_json_text(node):
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_flatten_json_text(c) for c in node.get("content") or [])


def stamp_page_numbers(doc, pn, m):
    """Draws page numbers onto the already-built PDF `doc`, from page index
    pn["skip"] on (numbered from 1), centred vertically in the header or
    footer margin band `m` (a sanitize_margins dict) and against its
    left/centre/right edge, in the chosen font, size and colour, optionally
    inside a circle or rectangle. The number gets a box just wide enough for
    it (and the outline); the text itself is laid out by insert_htmlbox in a
    slightly wider rect so a font-metrics mismatch can't wrap it."""
    if pn["position"] == "none":
        return
    vert, _, horiz = pn["position"].partition("-")
    band = m["header"] if vert == "top" else m["footer"]
    if band <= 0:
        return  # no room in the margin to draw into
    fs = pn["fontSize"]
    color = pn["color"]
    rgb = tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    css = f"font-family: {pn['font']}, Helvetica, Arial, sans-serif; font-size: {fs}pt; color: {color}; margin: 0; text-align: center;"
    box_h = fs * 1.6
    for i in range(pn["skip"], doc.page_count):
        page = doc[i]
        r = page.rect
        label = str(i - pn["skip"] + pn["first"])
        box_w = max(box_h, fitz.get_text_length(label, fontsize=fs) + fs * 0.6) if pn["shape"] != "none" else fitz.get_text_length(label, fontsize=fs)
        left, right = r.x0 + m["left"], r.x1 - m["right"]
        x0 = {"left": left, "center": (left + right - box_w) / 2, "right": right - box_w}[horiz]
        mid = r.y0 + band / 2 if vert == "top" else r.y1 - band / 2
        box = fitz.Rect(x0, mid - box_h / 2, x0 + box_w, mid + box_h / 2)
        if pn["shape"] == "circle":
            page.draw_oval(box, color=rgb, width=1)
        elif pn["shape"] == "rectangle":
            page.draw_rect(box, color=rgb, width=1)
        drop = PAGE_NUMBER_TEXT_DROP * fs
        page.insert_htmlbox(box + (-fs, drop, fs, drop), f'<p style="{css}">{label}</p>')


def _link_rects(pdf_bytes, found):
    """[(page index, "<document id>:<page>", rect)] for the snippet links the story reported in `found`
    ((page index, href, rect) per piece of linked content): one rect per reference text line and one per image,
    the image's taken from the PDF itself since the story's own rect for it is offset."""
    out = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for pno in sorted({f[0] for f in found}):
            here = [f for f in found if f[0] == pno]
            images = doc[pno].get_image_info()
            lines = {}
            for _, href, r in here:
                if href.startswith("#snipref:"):
                    key = (href, round(r.y0, 1))
                    lines[key] = lines[key] | r if key in lines else fitz.Rect(r)
            out += [(pno, k[0][len("#snipref:"):], r) for k, r in lines.items()]
            for _, href, r in here:
                if not href.startswith("#snipimg:") or r.width <= 0:
                    continue
                line = next((o for _, h, o in here if h == href and o.width == 0 and abs(o.x0 - r.x0) < 0.6), None)
                box = next((fitz.Rect(i["bbox"]) for i in images
                            if abs(i["bbox"][0] - r.x0) < 0.6 and abs(i["bbox"][2] - r.x1) < 0.6
                            and (line is None or line.y0 - 1 <= i["bbox"][3] <= line.y1 + 1)), None)
                if box is not None:
                    out.append((pno, href[len("#snipimg:"):], box))
    return out


def render_report_pdf(title, doc_json, margins=None, page_numbers=None, links=None):
    """The report as a PDF. A list passed as `links` is filled with (page index, "<document id>:<page>", rect)
    for each element carrying an "annexLink" attr."""
    numbered_body_html = _json_blocks_to_html(doc_json.get("content") or [])

    full_html = f"<html><head><style>{REPORT_PDF_CSS}</style></head><body>{numbered_body_html}</body></html>"

    m = sanitize_margins(margins)
    pn = sanitize_page_numbers(page_numbers)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (m["left"], m["header"], -m["right"], -m["footer"])
    story = fitz.Story(html=full_html)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    more, pno, found = 1, 0, []
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        if links is not None:
            def collect(p):
                if p.href:
                    found.append((pno, p.href, fitz.Rect(p.rect)))
            story.element_positions(collect, {"page": pno})
        story.draw(device)
        writer.end_page()
        pno += 1
    writer.close()
    if links is not None:
        links.extend(_link_rects(buf.getvalue(), found))
    if pn["position"] == "none":
        return buf.getvalue()
    with fitz.open(stream=buf.getvalue(), filetype="pdf") as numbered:
        stamp_page_numbers(numbered, pn, m)
        return numbered.tobytes()


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


DOCX_TEXT_ALIGN = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}


def _docx_render_blocks(nodes, doc, max_width_emu, num_state=None, depth=0):
    for node in nodes:
        t = node.get("type")
        attrs = node.get("attrs") or {}
        content = node.get("content") or []

        if t == "heading":
            p = doc.add_paragraph(style=DOCX_HEADING_STYLE.get(attrs.get("level", 1), "Heading 1"))
            p.alignment = DOCX_TEXT_ALIGN.get(attrs.get("textAlign"))
            _docx_render_inline(content, p, max_width_emu)
        elif t == "horizontalRule":
            doc.add_paragraph().add_run("—" * 20)
        elif t == "paragraph":
            p = doc.add_paragraph()
            p.alignment = DOCX_TEXT_ALIGN.get(attrs.get("textAlign"))
            # Standard spacing is a 1em gap after the paragraph; "tight" has none.
            p.paragraph_format.space_after = Pt(0 if attrs.get("tight") else 11)
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


def _docx_add_conditional_page_field(paragraph, skip, first=1):
    """Inserts { IF { PAGE } > skip "{ = { PAGE } - skip + first - 1 }" "" } -- Word
    evaluates this per rendered page, so it correctly hides the number on
    the first `skip` pages (and restarts the visible count at `first` right after)
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
    delta = first - 1 - skip
    _docx_field_run(paragraph, "w:instrText", f" {'+' if delta >= 0 else '-'} {abs(delta)} ")
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
    first = page_numbers.get("first", 1)
    if skip > 0 or first != 1:
        _docx_add_conditional_page_field(paragraph, skip, first)
    else:
        _docx_add_page_field(paragraph)

    font_name = _docx_clean_font_name(page_numbers.get("font"))
    font_size = page_numbers.get("fontSize")
    color = page_numbers.get("color")
    for run in paragraph.runs:
        if color:
            run.font.color.rgb = RGBColor.from_string(color[1:].upper())
        if page_numbers.get("shape", "none") != "none":
            # Word has no simple circle around a field, so both shapes come out as a box.
            bdr = OxmlElement("w:bdr")
            for attr, val in (("val", "single"), ("sz", "6"), ("space", "1"), ("color", color[1:].upper())):
                bdr.set(qn(f"w:{attr}"), val)
            run._r.get_or_add_rPr().append(bdr)
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

    _docx_render_blocks(doc_json.get("content") or [], doc, max_width_emu)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@app.context_processor
def inject_notifications():
    """The title bar's notification bell (templates/_notifications.html)."""
    if not getattr(g, "user", None):
        return {}
    return {"notification_count": storage.count_notifications(g.user.id)}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # A signed-out visitor lands on the public content aggregation page
    # instead of being sent to sign in -- see auth.PUBLIC_ENDPOINTS.
    if g.user is None:
        return redirect(url_for("public_view"))
    return render_template("index.html")


@app.route("/api/settings/advanced", methods=["PUT"])
def api_set_show_advanced():
    """Saves whether this user has "View advanced features" turned on."""
    show = (request.get_json(silent=True) or {}).get("show")
    if not isinstance(show, bool):
        return jsonify({"error": "show must be true or false"}), 400
    storage.set_show_advanced(g.user.id, show)
    return jsonify({"show": show})


@app.route("/api/report-sections", methods=["GET", "PUT"])
def api_report_sections():
    """The user's reports-page sections (names, order, and which report
    cards sit in each; unlisted reports are in the implicit General)."""
    if request.method == "GET":
        return jsonify(storage.get_report_sections(g.user.id))
    data = request.get_json(silent=True)
    if not isinstance(data, list) or len(data) > 100:
        return jsonify({"error": "sections must be a list"}), 400
    clean = []
    for sec in data:
        if not (isinstance(sec, dict) and isinstance(sec.get("id"), str) and isinstance(sec.get("name"), str)
                and isinstance(sec.get("reports"), list)):
            return jsonify({"error": "invalid section"}), 400
        clean.append({
            "id": sec["id"][:40], "name": sec["name"].strip()[:80],
            "reports": [r[:100] for r in sec["reports"][:2000] if isinstance(r, str)],
        })
    storage.set_report_sections(g.user.id, clean)
    return jsonify(clean)


ACCOUNT_FIELD_MAX_CHARS = 100
PHOTO_MAX_PIXELS = 512
DEFAULT_COUNTRY = "India"
COUNTRIES = [
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda", "Argentina", "Armenia",
    "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados", "Belarus", "Belgium",
    "Belize", "Benin", "Bhutan", "Bolivia", "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei", "Bulgaria",
    "Burkina Faso", "Burundi", "Cabo Verde", "Cambodia", "Cameroon", "Canada", "Central African Republic", "Chad",
    "Chile", "China", "Colombia", "Comoros", "Congo", "Costa Rica", "Croatia", "Cuba", "Cyprus", "Czechia",
    "Democratic Republic of the Congo", "Denmark", "Djibouti", "Dominica", "Dominican Republic", "Ecuador", "Egypt",
    "El Salvador", "Equatorial Guinea", "Eritrea", "Estonia", "Eswatini", "Ethiopia", "Fiji", "Finland", "France",
    "Gabon", "Gambia", "Georgia", "Germany", "Ghana", "Greece", "Grenada", "Guatemala", "Guinea", "Guinea-Bissau",
    "Guyana", "Haiti", "Honduras", "Hungary", "Iceland", "India", "Indonesia", "Iran", "Iraq", "Ireland", "Israel",
    "Italy", "Ivory Coast", "Jamaica", "Japan", "Jordan", "Kazakhstan", "Kenya", "Kiribati", "Kuwait", "Kyrgyzstan",
    "Laos", "Latvia", "Lebanon", "Lesotho", "Liberia", "Libya", "Liechtenstein", "Lithuania", "Luxembourg",
    "Madagascar", "Malawi", "Malaysia", "Maldives", "Mali", "Malta", "Marshall Islands", "Mauritania", "Mauritius",
    "Mexico", "Micronesia", "Moldova", "Monaco", "Mongolia", "Montenegro", "Morocco", "Mozambique", "Myanmar",
    "Namibia", "Nauru", "Nepal", "Netherlands", "New Zealand", "Nicaragua", "Niger", "Nigeria", "North Korea",
    "North Macedonia", "Norway", "Oman", "Pakistan", "Palau", "Palestine", "Panama", "Papua New Guinea", "Paraguay",
    "Peru", "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia", "Rwanda", "Saint Kitts and Nevis",
    "Saint Lucia", "Saint Vincent and the Grenadines", "Samoa", "San Marino", "Sao Tome and Principe",
    "Saudi Arabia", "Senegal", "Serbia", "Seychelles", "Sierra Leone", "Singapore", "Slovakia", "Slovenia",
    "Solomon Islands", "Somalia", "South Africa", "South Korea", "South Sudan", "Spain", "Sri Lanka", "Sudan",
    "Suriname", "Sweden", "Switzerland", "Syria", "Taiwan", "Tajikistan", "Tanzania", "Thailand", "Timor-Leste",
    "Togo", "Tonga", "Trinidad and Tobago", "Tunisia", "Turkey", "Turkmenistan", "Tuvalu", "Uganda", "Ukraine",
    "United Arab Emirates", "United Kingdom", "United States", "Uruguay", "Uzbekistan", "Vanuatu", "Vatican City",
    "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
]


@app.route("/account")
def account_view():
    profile = storage.get_profile(g.user.id)
    country = profile["country"] or DEFAULT_COUNTRY
    # A country saved earlier as free text stays selectable, so it isn't silently replaced.
    options = COUNTRIES if country in COUNTRIES or country == "Other" else [country] + COUNTRIES
    return render_template("account.html", profile=profile, countries=options, country=country, cities=CITIES_BY_COUNTRY,
                           default_cities=DEFAULT_CITY_BY_COUNTRY)


@app.route("/account/photo")
def account_photo():
    photo = storage.get_photo(g.user.id)
    if photo is None:
        abort(404)
    return Response(photo, mimetype="image/jpeg", headers={"Cache-Control": "private, no-cache"})


@app.route("/api/account/details", methods=["PUT"])
def api_account_details():
    body = request.get_json(silent=True) or {}
    values = []
    for field in ("full_name", "city", "country"):
        value = body.get(field, "")
        if not isinstance(value, str) or len(value.strip()) > ACCOUNT_FIELD_MAX_CHARS:
            raise DocumentError(f"Each detail must be at most {ACCOUNT_FIELD_MAX_CHARS} characters.", 400)
        values.append(value.strip())
    storage.set_profile(g.user.id, *values)
    return jsonify({"ok": True})


@app.route("/api/account/photo", methods=["POST", "DELETE"])
def api_account_photo():
    if request.method == "DELETE":
        storage.set_photo(g.user.id, None)
        return jsonify({"ok": True})
    upload = request.files.get("photo")
    if upload is None:
        raise DocumentError("Please choose a photo.", 400)
    try:
        img = Image.open(upload.stream)
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise DocumentError("That file isn't an image we can read. Please choose a JPEG or PNG photo.", 400)
    img.thumbnail((PHOTO_MAX_PIXELS, PHOTO_MAX_PIXELS))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)  # re-encoding also drops any metadata or embedded payload
    storage.set_photo(g.user.id, out.getvalue())
    return jsonify({"ok": True})


@app.route("/api/account/password", methods=["POST"])
def api_account_password():
    body = request.get_json(silent=True) or {}
    old, new, confirm = (str(body.get(k) or "") for k in ("old_password", "new_password", "confirm_password"))
    # The challenge is checked (and used up) first, so old passwords can't be guessed without solving one per try.
    if not auth.captcha_passed(str(body.get("captcha_id") or ""), str(body.get("captcha_answer") or "")):
        raise DocumentError("The characters you typed didn't match the image. Please try the new one.", 400)
    if not g.user.check_password(old[:auth.PASSWORD_MAX_CHARS]):
        raise DocumentError("Your current password is incorrect.", 403)
    if len(new) < auth.PASSWORD_MIN_CHARS:
        raise DocumentError(f"Your new password must be at least {auth.PASSWORD_MIN_CHARS} characters long.", 400)
    if len(new) > auth.PASSWORD_MAX_CHARS:
        raise DocumentError("That password is too long.", 400)
    if new != confirm:
        raise DocumentError("The two new passwords don't match.", 400)
    storage.set_password_hash(
        g.user.id, auth.User.hash_password(new), auth._hash_token(request.cookies.get(auth.SESSION_COOKIE, "")),
    )
    return jsonify({"ok": True})


@app.route("/annotations")
def page_view():
    doc_id = request.args.get("doc", "")
    raw_type = request.args.get("type", "pdf")

    if not doc_id:
        return render_template("annotations.html", doc_id="", docs=list_source_docs(resolve_default_cause_id(create=False)), default_cause=default_cause_for_display(), can_create=can_create_items())

    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        raise DocumentError("page must be an integer", 400)

    pdf_bytes, norm_type = _get_pdf_bytes(doc_id, raw_type)
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
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
        doc_title=storage.get_document_title(doc_id),
        doc_description=storage.get_document_description(doc_id),
        description_url=url_for("api_doc_description", doc_id=doc_id),
        can_edit=has_role("source", doc_id, "editor"),
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
        require_role("report", report_id)
        if not storage.report_exists(report_id):
            raise DocumentError(f"No report with id {report_id!r}", 404)

    can_edit = bool(report_id) and has_role("report", report_id, "editor")

    report_cause_titles = []
    if report_id:
        this_report = add_cause_titles([r for r in storage.list_reports(g.user.id) if r["id"] == report_id])
        if this_report:
            report_cause_titles = this_report[0]["cause_titles"]
            # "Insert from": only documents in the report's cause(s) -- plus
            # the report's own source document, so it stays selectable.
            report_causes = set(this_report[0]["all_cause_ids"])
            source_docs = [
                d for d in source_docs
                if d["id"] == this_report[0]["source_doc"] or report_causes & set(d["all_cause_ids"])
            ]

    preselect_source = request.args.get("source", "")
    preselect_type = request.args.get("type", "pdf")
    if preselect_source:
        check_doc_id(preselect_source)
        if not any(d["id"] == preselect_source for d in source_docs):
            preselect_source = ""

    return render_template(
        "reports.html",
        source_docs=source_docs,
        default_cause=default_cause_for_display(),
        report_id=report_id,
        can_edit=can_edit,
        can_create=can_create_items(),
        report_cause_titles=report_cause_titles,
        preselect_source=preselect_source,
        preselect_type=preselect_type,
    )


@app.route("/annexures")
def annexures_view():
    report_id = request.args.get("annexure", "")
    check_report_id(report_id)
    require_role("report", report_id)
    report = storage.get_report(report_id)
    if report is None:
        raise DocumentError(f"No report with id {report_id!r}", 404)
    return render_template(
        "annexures.html",
        report_id=report_id,
        report_name=report["name"],
        annexure_url=url_for("api_report_annexure", report_id=report_id),
        can_edit=has_role("report", report_id, "editor"),
    )


@app.route("/allegations")
def allegations_view():
    # Allegations are scoped by the user's access to their cause (see
    # api_allegations below); "case" is only an optional hint from the cases workspace to pre-filter the list to
    # allegations linked to that case, not a page the allegation belongs to.
    filter_case_id = request.args.get("case", "")
    if filter_case_id:
        check_report_id(filter_case_id)
        require_role("case", filter_case_id)

    return render_template("allegations.html", filter_case_id=filter_case_id, can_create=can_create_items())


@app.route("/causes")
def causes_view():
    return render_template("causes.html", default_cause_id=storage.get_default_cause(g.user.id) or "", can_create=not g.user.is_guest)


@app.route("/cases")
def cases_view():
    return render_template("cases.html", can_create=can_create_items())


@app.route("/case-details")
def case_details_view():
    return render_template("case_details.html", is_admin=g.user.is_admin)


# ---------------------------------------------------------------------------
# Collaborations: invite another user by email; once they accept, share
# objects you own with them (viewer or editor) -- see
# db/migrations/015_collaborations.sql.
# ---------------------------------------------------------------------------

STATUS_LABELS = {"sent": "Invitation Sent", "received": "Invitation Received", "confirmed": "Confirmed"}


def own_collaboration(collaboration_id, *, confirmed=False):
    """The collaboration with this id, provided the user is a party to it
    (else the usual 404), plus the other party's id."""
    row = storage.get_collaboration(collaboration_id)
    if row is None or g.user.id not in (row["inviter_id"], row["invitee_id"]):
        raise DocumentError("No such collaboration", 404)
    if confirmed and row["status"] != "confirmed":
        raise DocumentError("That collaboration hasn't been accepted yet", 400)
    other_id = row["invitee_id"] if row["inviter_id"] == g.user.id else row["inviter_id"]
    return row, other_id


@app.route("/collaborations")
def collaborations_view():
    return render_template("collaborations.html")


@app.route("/api/collaborations/captcha")
def api_collaborations_captcha():
    return jsonify(auth.new_captcha())


@app.route("/api/collaborations", methods=["GET", "POST"])
def api_collaborations():
    if request.method == "GET":
        cards = storage.list_collaborations(g.user.id)
        for card in cards:
            card["status_label"] = STATUS_LABELS[card["status"]]
            if card["status"] == "confirmed":
                card["shared_by_me"] = storage.list_shared_objects(g.user.id, card["other_id"])
                card["shared_with_me"] = storage.list_shared_objects(card["other_id"], g.user.id)
            del card["other_id"]
        return jsonify({"collaborations": cards, "shareable": storage.list_owned_objects(g.user.id)})

    body = request.get_json(silent=True) or {}
    email = auth.normalize_email(body.get("email"))
    # The challenge is checked (and used up) first, so an unsolved one can't
    # be used to find out which emails are registered.
    if not auth.captcha_passed(str(body.get("captcha_id") or ""), str(body.get("captcha_answer") or "")):
        raise DocumentError("The characters you typed didn't match the image. Please try the new one.", 400)
    if not auth.EMAIL_RE.match(email) or len(email) > auth.EMAIL_MAX_CHARS:
        raise DocumentError("Please enter a valid email address.", 400)
    invitee = storage.get_user_by_email(email)
    if invitee is None:
        raise DocumentError("That email address is not currently associated with a user of this website.", 404)
    if invitee["id"] == g.user.id:
        raise DocumentError("You can't invite yourself to collaborate.", 400)
    existing = storage.find_collaboration(g.user.id, invitee["id"])
    if existing is not None:
        if existing["status"] == "confirmed":
            raise DocumentError("You are already collaborating with that user.", 409)
        if existing["inviter_id"] == g.user.id:
            raise DocumentError("You have already invited that user.", 409)
        raise DocumentError("That user has already invited you -- accept their invitation below.", 409)
    if storage.create_collaboration(g.user.id, invitee["id"]) is None:
        raise DocumentError("You have already invited that user.", 409)
    # Tell the invitee. Capped per address so deleting and re-sending
    # invitations can't be used to flood someone's inbox.
    if storage.record_signup_attempt(
        "mail:invite:" + invitee["email"].lower(), auth.MAIL_LIMIT_PER_ADDRESS, auth.MAIL_LIMIT_WINDOW_SECONDS,
    ):
        _, site_name, _ = mailer.site_for_host(request.host)
        mailer.send_action_email(
            request.host, invitee["email"], f"{g.user.email} invited you to collaborate on {site_name}",
            f"{g.user.email} has invited you to collaborate on {site_name}. "
            "Sign in to accept or decline the invitation.",
            "View the invitation", mailer.base_url(request.host) + url_for("collaborations_view"),
            "Nothing is shared with them unless you accept and choose what to share. "
            "If you don't know this person, you can ignore this email.",
        )
    return jsonify({"ok": True})


@app.route("/api/collaborations/seen", methods=["POST"])
def api_collaborations_seen():
    """The user has looked at the page: their accepted invitations stop
    counting as notifications."""
    storage.mark_acceptances_seen(g.user.id)
    return jsonify({"ok": True})


@app.route("/api/collaboration/<int:collaboration_id>", methods=["DELETE"])
def api_collaboration_item(collaboration_id):
    """Withdraws an invitation you sent, declines one you received, or ends
    a confirmed collaboration -- which also removes everything either of you
    had shared with the other."""
    row, other_id = own_collaboration(collaboration_id)
    if row["status"] == "confirmed":
        storage.end_collaboration(collaboration_id, g.user.id, other_id)
    else:
        storage.delete_pending_collaboration(collaboration_id, g.user.id)
    return jsonify({"ok": True})


@app.route("/api/collaboration/<int:collaboration_id>/accept", methods=["POST"])
def api_collaboration_accept(collaboration_id):
    row, _ = own_collaboration(collaboration_id)
    if row["invitee_id"] != g.user.id or not storage.accept_collaboration(collaboration_id, g.user.id):
        raise DocumentError("There is no invitation here for you to accept", 400)
    return jsonify({"ok": True})


@app.route("/api/collaboration/<int:collaboration_id>/access", methods=["PUT"])
def api_collaboration_access(collaboration_id):
    """Sets which of the user's own objects the collaborator can see/edit:
    {"cause": {"<id>": "viewer" | "editor", ...}, "case": {...}, ...}. Each
    kind given is replaced wholesale; a kind left out is untouched."""
    _, other_id = own_collaboration(collaboration_id, confirmed=True)
    body = request.get_json(silent=True) or {}
    grants = {}
    for kind in storage.SHARE_KINDS:
        if kind not in body:
            continue
        wanted = body[kind]
        if not isinstance(wanted, dict) or any(
            not isinstance(oid, str) or role not in storage.SHARE_ROLES for oid, role in wanted.items()
        ):
            raise DocumentError(f"Invalid access list for {kind}", 400)
        grants[kind] = wanted
    storage.set_shared_objects(g.user.id, other_id, grants)
    return jsonify({"shared_by_me": storage.list_shared_objects(g.user.id, other_id)})


# ---------------------------------------------------------------------------
# Secret keys: the owner of a cause, case, allegation, report or document
# shares it with anyone holding one of its keys (see
# db/migrations/018_secret_keys.sql and auth.unlock). Deleting a key
# unshares; switching it off suspends it.
# ---------------------------------------------------------------------------

# kind -> (page path, query parameter) of the URL a key opens
KEY_PAGE_URLS = {
    "cause": ("causes_view", "cause"),
    "case": ("cases_view", "case"),
    "allegation": ("allegations_view", "allegation"),
    "report": ("documents_view", "report"),
    "source": ("page_view", "doc"),
}


def key_kind(raw):
    if raw not in storage.SHARE_KINDS:
        raise DocumentError(f"Invalid kind: {raw!r}", 400)
    return raw


@app.route("/api/keys", methods=["GET", "POST"])
def api_keys():
    if request.method == "GET":
        keys = storage.list_keys(g.user.id)
        for kind, items in keys.items():
            endpoint, param = KEY_PAGE_URLS[kind]
            for item in items:
                item["url"] = url_for(endpoint, _external=True, **{param: item["object_id"]})
        return jsonify({"keys": keys, "shareable": storage.list_owned_objects(g.user.id)})

    body = request.get_json(silent=True) or {}
    kind = key_kind(body.get("kind"))
    object_id = body.get("object_id")
    if not isinstance(object_id, str):
        raise DocumentError("Choose what to share", 400)
    if body.get("permission") not in storage.KEY_PERMISSIONS:
        raise DocumentError("Permission must be viewer or editor", 400)
    require_role(kind, object_id, "owner")  # only an owner can share it
    storage.create_key(g.user.id, kind, object_id, body["permission"])
    return jsonify({"ok": True}), 201


@app.route("/api/key/<kind>/<int:key_id>", methods=["PATCH", "DELETE"])
def api_key_item(kind, key_id):
    kind = key_kind(kind)
    if request.method == "DELETE":
        done = storage.delete_key(g.user.id, kind, key_id)
    else:
        body = request.get_json(silent=True) or {}
        if "permission" in body:
            if body["permission"] not in storage.KEY_PERMISSIONS:
                raise DocumentError("Permission must be viewer or editor", 400)
            done = storage.set_key_permission(g.user.id, kind, key_id, body["permission"])
        else:
            active = body.get("active")
            if not isinstance(active, bool):
                raise DocumentError("active must be true or false", 400)
            done = storage.set_key_active(g.user.id, kind, key_id, active)
    if not done:
        raise DocumentError("No such key", 404)
    return jsonify({"ok": True})


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
        raise DocumentError(f"Unsupported file type {ext!r}. Upload a .pdf file.", 400)

    data = f.read()
    if not data:
        raise DocumentError("Uploaded file is empty", 400)

    try:
        with fitz.open(stream=data, filetype="pdf") as d:
            if d.page_count == 0:
                raise DocumentError("PDF has no pages", 400)
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"File is not a valid PDF: {exc}", 400) from exc

    stem = slugify_report_name(Path(f.filename).stem)
    doc_id = stem
    while storage.document_exists(doc_id):
        doc_id = f"{stem}-{uuid.uuid4().hex[:6]}"

    storage.create_document(doc_id, ext.lstrip("."), data, owner_id=g.user.id, link=default_cause_link())

    return jsonify({"id": doc_id, "type": norm_type, "filename": f"{doc_id}{ext}"})


# ---------------------------------------------------------------------------
# API: document info / rendering
# ---------------------------------------------------------------------------

@app.route("/api/doc/<doc_id>/info")
def api_doc_info(doc_id):
    pdf_bytes, _ = _get_pdf_bytes(doc_id, request.args.get("type", "pdf"))
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        pages = [{"width": p.rect.width, "height": p.rect.height} for p in d]
    return jsonify({"page_count": len(pages), "pages": pages})


@app.route("/api/doc/<doc_id>/title", methods=["POST"])
def api_doc_title(doc_id):
    check_doc_id(doc_id)
    require_role("source", doc_id, "editor")
    if storage.get_document_type(doc_id) is None:
        raise DocumentError(f"No document with id {doc_id!r}", 404)

    body = request.get_json(silent=True) or {}
    title = str(body.get("title") or "").strip()[:200] or doc_id
    storage.set_document_title(doc_id, title)
    return jsonify({"title": title})


@app.route("/api/doc/<doc_id>/description", methods=["POST"])
def api_doc_description(doc_id):
    check_doc_id(doc_id)
    require_role("source", doc_id, "editor")
    if storage.get_document_type(doc_id) is None:
        raise DocumentError(f"No document with id {doc_id!r}", 404)

    body = request.get_json(silent=True) or {}
    description = " ".join(str(body.get("description") or "").split())[:500]
    storage.set_document_description(doc_id, description)
    return jsonify({"description": description})


@app.route("/api/document/<doc_id>", methods=["DELETE"])
def api_delete_document(doc_id):
    check_doc_id(doc_id)
    require_role("source", doc_id, "owner")
    if storage.get_document_type(doc_id) is None:
        raise DocumentError(f"No document with id {doc_id!r}", 404)

    storage.delete_document(doc_id)
    return jsonify({"ok": True})


@app.route("/api/doc/<doc_id>/render/<int:page>")
def api_render(doc_id, page):
    pdf_bytes, _ = _get_pdf_bytes(doc_id, request.args.get("type", "pdf"))
    try:
        dpi = int(request.args.get("dpi", 150))
    except ValueError:
        dpi = 150
    dpi = max(50, min(dpi, 600))

    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        if not (1 <= page <= d.page_count):
            raise DocumentError("Page out of range", 404)
        zoom = dpi / 72
        p = d[page - 1]
        mode = {"1": "all"}.get(request.args.get("annotations"), request.args.get("annotations"))
        if mode in ("blackouts", "all"):
            draw_annotations_on_page(p, annexure_annotations_to_draw(storage.get_page_annotations(doc_id, page), mode), p.rect)
        pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
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
    normalize_type(request.args.get("type", "pdf"))
    require_role("source", doc_id)
    return jsonify(storage.get_all_annotations(doc_id))


@app.route("/api/doc/<doc_id>/annotations/<int:page>", methods=["GET", "POST"])
def api_annotations(doc_id, page):
    check_doc_id(doc_id)
    raw_type = request.args.get("type", "pdf")
    normalize_type(raw_type)
    require_role("source", doc_id, "viewer" if request.method == "GET" else "editor")

    if request.method == "GET":
        return jsonify(storage.get_page_annotations(doc_id, page))

    body = request.get_json(silent=True) or {}
    anns = body.get("annotations")
    if not isinstance(anns, list):
        raise DocumentError("Body must contain an 'annotations' list", 400)

    storage.set_page_annotations(doc_id, page, anns)
    refreshed = refresh_page_snippets(doc_id, page, raw_type, anns)
    return jsonify({"status": "ok", "count": len(anns), "snippets_refreshed": refreshed})


def refresh_page_snippets(doc_id, page, raw_type, raw_annotations):
    """Re-renders every snippet on this page against the page's current
    annotations, so the stored PNGs never show stale (edited or deleted)
    markup. Each keeps its crop rectangle and its existing resolution."""
    snippets = storage.list_snippets(doc_id, page=page)
    if not snippets:
        return 0
    pdf_bytes, _ = _get_pdf_bytes(doc_id, raw_type, min_role="editor")
    annotations = sanitize_annotations(raw_annotations)
    refreshed = 0
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        if not (1 <= page <= d.page_count):
            return 0
        p = d[page - 1]
        pr = p.rect
        draw_annotations_on_page(p, annotations, pr)
        for s in snippets:
            r = s["rect"]
            clip = fitz.Rect(
                pr.x0 + r["x"] * pr.width,
                pr.y0 + r["y"] * pr.height,
                pr.x0 + min(r["x"] + r["w"], 1.0) * pr.width,
                pr.y0 + min(r["y"] + r["h"], 1.0) * pr.height,
            ) & pr
            dpi = 300
            old = storage.read_snippet_bytes(doc_id, s["filename"])
            if old and clip.width > 0:
                # Keep the resolution the snippet was originally extracted at.
                dpi = max(72, min(round(fitz.Pixmap(old).width / clip.width * 72), 900))
            zoom = dpi / 72
            png_bytes = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip).tobytes("png")
            storage.replace_snippet_image(doc_id, s, bool(annotations), png_bytes)
            refreshed += 1
    return refreshed


# ---------------------------------------------------------------------------
# API: rectangular snippet extraction
# ---------------------------------------------------------------------------

@app.route("/api/doc/<doc_id>/snippet/<int:page>", methods=["POST"])
def api_create_snippet(doc_id, page):
    raw_type = request.args.get("type", "pdf")
    pdf_bytes, _ = _get_pdf_bytes(doc_id, raw_type, min_role="editor")

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

    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
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

    entry = storage.create_snippet(doc_id, page, {"x": x, "y": y, "w": w, "h": h}, bool(annotations), png_bytes,
                                  created_by=None if g.user.is_guest else g.user.id)

    result = dict(entry)
    result["url"] = url_for("api_snippet_file", doc_id=doc_id, filename=entry["filename"], type=raw_type)
    return jsonify(result)


def _annotated_pdf_response(doc_id, annotated_only):
    pdf_bytes, _ = _get_pdf_bytes(doc_id, request.args.get("type", "pdf"))

    data = storage.get_all_annotations(doc_id)

    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        annotated_pages = set()
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
            annotated_pages.add(page_num - 1)
        if annotated_only:
            # Pages a snippet was taken from count as annotated too.
            for snip in storage.list_snippets(doc_id):
                if 1 <= snip["page"] <= d.page_count:
                    annotated_pages.add(snip["page"] - 1)
            if not annotated_pages:
                raise DocumentError("This document has no annotated or snippet pages.", 400)
            d.select(sorted(annotated_pages))
        annotated_pdf_bytes = d.tobytes(deflate=True)

    suffix = "annotated-pages" if annotated_only else "annotated"
    resp = Response(annotated_pdf_bytes, mimetype="application/pdf")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Disposition"] = f'attachment; filename="{doc_id}-{suffix}.pdf"'
    return resp


@app.route("/api/doc/<doc_id>/download")
def api_download_annotated(doc_id):
    return _annotated_pdf_response(doc_id, annotated_only=False)


@app.route("/api/doc/<doc_id>/download-annotated-pages")
def api_download_annotated_pages(doc_id):
    return _annotated_pdf_response(doc_id, annotated_only=True)


@app.route("/api/doc/<doc_id>/download-original")
def api_download_original(doc_id):
    pdf_bytes, _ = _get_pdf_bytes(doc_id, request.args.get("type", "pdf"))
    resp = Response(pdf_bytes, mimetype="application/pdf")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Disposition"] = f'attachment; filename="{doc_id}.pdf"'
    return resp


@app.route("/api/doc/<doc_id>/snippets")
def api_list_snippets(doc_id):
    check_doc_id(doc_id)
    normalize_type(request.args.get("type", "pdf"))
    role = require_role("source", doc_id)
    page = request.args.get("page", type=int)

    # Who made a snippet is for the owner's side panel only.
    meta = storage.list_snippets(doc_id, page=page, include_creator=(role == "owner"))
    raw_type = request.args.get("type", "pdf")
    for m in meta:
        m["url"] = url_for("api_snippet_file", doc_id=doc_id, filename=m["filename"], type=raw_type)
    return jsonify(meta)


@app.route("/api/doc/<doc_id>/snippet/<snippet_id>", methods=["DELETE"])
def api_delete_snippet(doc_id, snippet_id):
    check_doc_id(doc_id)
    normalize_type(request.args.get("type", "pdf"))
    require_role("source", doc_id, "editor")
    if not storage.delete_snippet(doc_id, snippet_id):
        raise DocumentError(f"No snippet with id {snippet_id}", 404)
    return jsonify({"status": "ok"})


@app.route("/api/reports", methods=["GET", "POST"])
def api_reports():
    if request.method == "GET":
        items = storage.list_reports(g.user.id)
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return jsonify(add_cause_titles(items, default_cause_only()))

    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:200]
    if not name:
        raise DocumentError("A report name is required", 400)

    source_doc = str(body.get("source_doc") or "")
    source_type = "pdf"
    if source_doc:
        check_doc_id(source_doc)
        source_type = normalize_type(body.get("source_type", "pdf"))
        if not storage.document_exists(source_doc) or not has_role("source", source_doc, "viewer"):
            source_doc, source_type = "", "pdf"

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
    storage.save_report(report_id, data, owner_id=g.user.id, link=default_cause_link())
    return jsonify({"id": report_id, **data})


@app.route("/api/report/<report_id>", methods=["GET", "POST", "DELETE"])
def api_report(report_id):
    check_report_id(report_id)
    require_role("report", report_id, {"GET": "viewer", "DELETE": "owner"}.get(request.method, "editor"))
    existing = storage.get_report(report_id)
    if existing is None:
        raise DocumentError(f"No report with id {report_id!r}", 404)

    if request.method == "GET":
        resp = dict(existing)
        resp["margins"] = sanitize_margins(existing.get("margins"))
        resp["pageNumbers"] = sanitize_page_numbers(existing.get("pageNumbers"))
        return jsonify(resp)

    if request.method == "DELETE":
        storage.delete_report(report_id)
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
        linkable = source_doc == existing.get("source_doc") or has_role("source", source_doc, "viewer")
        if not linkable or not storage.document_exists(source_doc):
            source_doc, source_type = "", "pdf"

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
    storage.save_report(report_id, data)
    return jsonify(data)


@app.route("/api/report/<report_id>/copy", methods=["POST"])
def api_report_copy(report_id):
    """Copies a report (content, page setup, source document, annexure and
    cause/case associations) to a new one named "Copy n of <title>", with the
    lowest n not already used by a report this user can see."""
    check_report_id(report_id)
    require_role("report", report_id, "viewer")
    if not can_create_items():
        raise DocumentError("You don't have permission to create reports", 403)
    existing = storage.get_report(report_id)
    if existing is None:
        raise DocumentError(f"No report with id {report_id!r}", 404)

    listed = storage.list_reports(g.user.id)
    taken = {r["name"] for r in listed}
    n = 1
    while f"Copy {n} of {existing['name']}"[:200] in taken:
        n += 1
    name = f"Copy {n} of {existing['name']}"[:200]

    now = datetime.now(timezone.utc).isoformat()
    data = {
        "name": name,
        "doc": existing["doc"],
        "source_doc": existing.get("source_doc", ""),
        "source_type": existing.get("source_type", "pdf"),
        "margins": sanitize_margins(existing.get("margins")),
        "pageNumbers": sanitize_page_numbers(existing.get("pageNumbers")),
        "created_at": now,
        "updated_at": now,
    }
    new_id = f"{slugify_report_name(name)}-{uuid.uuid4().hex[:6]}"
    original = next((r for r in listed if r["id"] == report_id), None)
    links = [("cause", c) for c in (original or {}).get("cause_ids", [])]
    links += [("case", c) for c in (original or {}).get("case_ids", [])]
    storage.save_report(new_id, data, owner_id=g.user.id, link=links[0] if links else default_cause_link())
    for target_kind, target_id in links[1:]:
        storage.link_to("report", new_id, target_kind, target_id)
    annexure = storage.get_annexure(report_id)
    if annexure:
        storage.save_annexure(new_id, annexure)
    return jsonify({"id": new_id, "name": name, "original_name": existing["name"]})


PAGE_RANGE_RE = re.compile(r"^\s*\d+\s*(-\s*\d+\s*)?(,\s*\d+\s*(-\s*\d+\s*)?)*$")


def parse_page_range(text, page_count):
    """Pages (1-based, ascending, unique, clipped to the document) that a
    custom range like "1-3, 7" names; None if it isn't a valid range."""
    if not PAGE_RANGE_RE.match(text or ""):
        return None
    pages = set()
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        lo, hi = int(lo), int(hi or lo)
        if lo < 1 or hi < lo:
            return None
        pages.update(range(lo, min(hi, page_count) + 1))
    return sorted(pages)


ANNEXURE_ANNOTATION_MODES = ("none", "blackouts", "all")


def annexure_annotations_to_draw(raw, mode):
    """The sanitized annotations an annexure in `mode` shows."""
    anns = sanitize_annotations(raw)
    if mode == "blackouts":
        return [a for a in anns if a["kind"] == "blackout"]
    return anns if mode == "all" else []


# ---- Annexure "List of Documents" ----------------------------------------
# An optional unnumbered index placed before the annexed pages: the report's
# case's cause title (nine blank lines when there isn't one), a centred
# "List of Documents" heading, and a Sl.No. / Particulars / Pg.Nos. table.

LIST_OF_DOCUMENTS_BLANK_LINES = 9


def _cause_title_parties(case, side):
    names = [p["name"].strip() for p in case.get("parties", []) if p["side"] == side and p["name"].strip()]
    if case.get("cause_title_one_line_parties"):
        return f"{names[0]} and Ors." if len(names) > 1 else "".join(names)
    return "\n".join(f"{i + 1}. {n}" for i, n in enumerate(names)) if len(names) > 1 else "".join(names)


def _cause_title_template_doc(body):
    """A template body is a ProseMirror doc (JSON string); older ones are plain text, one paragraph per line."""
    if isinstance(body, str) and body.lstrip().startswith("{"):
        try:
            doc = json.loads(body)
            if isinstance(doc, dict) and doc.get("type") == "doc":
                return doc
        except ValueError:
            pass
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}] if line else []}
                                       for line in str(body).splitlines() or [""]]}


def generate_cause_title_doc(case, templates):
    """Mirror of static/cause-title.js generateCauseDoc: the case's template with its placeholders filled in."""
    template = next((t for t in templates if t["id"] == case.get("cause_title_template_id")), templates[0] if templates else None)
    if template is None:
        return {"type": "doc", "content": [{"type": "paragraph"}]}
    values = {
        "COURT_NAME": (case.get("court") or "").upper() or "[COURT_NAME]",
        "COURT_LOCATION": (case.get("court_location") or "").upper() or "[COURT_LOCATION]",
        "CASE_NUMBER": (case.get("case_number") or "").upper() or "[CASE_NUMBER]",
        "PLAINTIFFS": _cause_title_parties(case, "complainant") or "[PLAINTIFFS]",
        "RESPONDENTS": _cause_title_parties(case, "respondent") or "[RESPONDENTS]",
    }
    placeholder = re.compile(r"\[(COURT_NAME|COURT_LOCATION|CASE_NUMBER|PLAINTIFFS|RESPONDENTS)\]")

    def substitute(node):
        out = []
        for i, line in enumerate(placeholder.sub(lambda m: values[m.group(1)], node["text"]).split("\n")):
            if i:
                out.append({"type": "hardBreak"})
            if line:
                out.append({**node, "text": line})
        return out

    blocks = []
    for block in _cause_title_template_doc(template["body"]).get("content") or []:
        content = [x for n in block.get("content") or [] for x in (substitute(n) if n.get("type") == "text" else [n])]
        blocks.append({**block, "content": content})
    return {"type": "doc", "content": blocks}


def annexure_list_context(report, include_doc=False):
    """What the List of Documents shows besides the table: the cause title as
    HTML ("" when the report has no case with one), its font, and the court
    location. Taken from the report's first case the user can view.
    `include_doc` also returns the cause title's ProseMirror doc as "causeTitleDoc"."""
    case = next((c for c in (storage.get_case(cid) for cid in report.get("case_ids", []) if has_role("case", cid, "viewer")) if c), None)
    ctx = {"causeTitleHtml": "", "causeFont": "", "causeFontSize": 0, "location": "", "role": ""}
    if case is None:
        return ctx
    ctx["location"] = case.get("court_location") or ""
    ctx["role"] = case.get("case_role") or ""
    templates = storage.list_cause_title_templates()
    doc = case.get("cause_title_doc")
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError:
            doc = None
    if not doc:
        filled = case.get("court") or case.get("case_number") or case.get("court_location") or any(p["name"].strip() for p in case.get("parties", []))
        if not (filled and templates):
            return ctx
        doc = generate_cause_title_doc(case, templates)
    doc = sanitize_report_doc(doc, CAUSE_TITLE_TEMPLATE_MAX_CHARS)
    ctx["causeTitleHtml"] = _json_blocks_to_html(doc.get("content") or [], raw_tabs=True)  # the page lays tabs out on 40px stops, the PDF via _tabs_to_stops
    if include_doc:
        ctx["causeTitleDoc"] = doc
    ctx["causeFont"] = case.get("cause_title_font") or ""
    ctx["causeFontSize"] = case.get("cause_title_font_size") or 0
    return ctx


def annexure_page_range(start, end, pn):
    """The page numbers shown on annexure pages `start`..`end` (0-based, inclusive), as "a-b"; "-" if none is numbered."""
    skip, first = (pn["skip"], pn["first"]) if pn["position"] != "none" else (0, 1)
    lo = max(start, skip)
    if lo > end:
        return "-"
    a, b = lo - skip + first, end - skip + first
    return str(a) if a == b else f"{a}-{b}"


def annexure_selected_pages(d, page_count):
    """The 1-based source pages of annexed document `d` that the annexure includes."""
    if d["page_mode"] == "all":
        return range(1, page_count + 1)
    if d["page_mode"] == "snippets":
        return [p for p in d["snippet_pages"] if p <= page_count]
    return parse_page_range(d["page_range"], page_count) or []


def _tabs_as_spaces(nodes):
    """`nodes` with every tab in its text replaced by six non-breaking spaces, as the PDF does, so text laid out with tabs
    takes the same width in Word (whose own tab stops are much wider and make such lines wrap)."""
    return [{**n, "text": n["text"].replace("\t", "\u00a0" * 6)} if n.get("type") == "text" and "text" in n
            else {**n, "content": _tabs_as_spaces(n["content"])} if n.get("content") else n
            for n in nodes]


def list_of_documents_docx(ctx, rows):
    """The List of Documents as a Word file: `ctx` as from annexure_list_context(include_doc=True), `rows` as for list_of_documents_html."""
    doc = DocxDocument()
    doc.styles["Normal"].font.name = "Times New Roman"
    doc.styles["Normal"].font.size = Pt(12)
    section = doc.sections[0]
    section.page_width, section.page_height = Pt(595.28), Pt(841.89)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, Pt(64))
    max_width_emu = int(section.page_width - section.left_margin - section.right_margin)

    cause = (ctx.get("causeTitleDoc") or {}).get("content")
    if cause:
        _docx_render_blocks(_tabs_as_spaces(cause), doc, max_width_emu)
        font, size = _docx_clean_font_name(ctx.get("causeFont") or ""), ctx.get("causeFontSize")
        for p in doc.paragraphs:
            # As in the PDF, where the List of Documents CSS gives every paragraph no margin and a 1.4 line height.
            p.paragraph_format.space_before = p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.4
            for run in p.runs:
                if font and run.font.name is None:
                    run.font.name = font
                if size:
                    run.font.size = Pt(size)
    else:
        for _ in range(LIST_OF_DOCUMENTS_BLANK_LINES):
            doc.add_paragraph()
    heading = doc.add_paragraph()
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    heading.paragraph_format.space_before = Pt(16)
    heading.paragraph_format.space_after = Pt(12)
    run = heading.add_run("List of Documents")
    run.bold = run.underline = True

    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, ("Sl.No.", "Particulars", "Pg.Nos.")):
        cell.paragraphs[0].add_run(text).bold = True
    for i, (text, pages) in enumerate(rows, 1):
        for cell, value in zip(table.add_row().cells, (str(i), text, pages)):
            cell.paragraphs[0].add_run(value)
    # Word sizes columns from the table grid, so set it as well as each cell; Sl.No. 12% / Pg.Nos. 16% as in the PDF.
    table.autofit = False
    widths = [int(max_width_emu * 0.12), int(max_width_emu * 0.72), int(max_width_emu * 0.16)]
    for column, width in zip(table.columns, widths):
        column.width = width
    for row in table.rows:
        for cell, width in zip(row.cells, widths):
            cell.width = width
        for j in (0, 2):
            row.cells[j].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

    place = doc.add_paragraph(ctx.get("location") or "")
    place.paragraph_format.space_before = Pt(56)
    date_line = doc.add_paragraph("Date:")
    if ctx.get("role"):
        # Right-aligned so the user can sign above it.
        date_line.paragraph_format.tab_stops.add_tab_stop(Emu(int(max_width_emu)), WD_TAB_ALIGNMENT.RIGHT)
        date_line.add_run("\t" + ctx["role"])
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _tabs_to_stops(html, font, size):
    """`html` with each raw tab padded with non-breaking spaces up to the next 30pt (40px) tab stop, as the cause title
    editor lays them out; fitz.Story has no tab-size. Text is measured in the nearest built-in font, so stops are close, not exact."""
    face = "cour" if "Courier" in font else "helv" if any(f in font for f in ("Arial", "Helvetica", "Verdana", "Calibri")) else "tiro"
    width = lambda text: fitz.get_text_length(text, fontname=face, fontsize=size)
    space, stop = width(" "), 30.0
    x = 0.0
    out = []
    for token in re.split(r"(<[^>]*>)", html):
        if token.startswith("<"):
            if re.match(r"</?(p|div|h\d|li|ul|ol|blockquote)\b|<br", token):
                x = 0.0
            out.append(token)
            continue
        for piece in re.split(r"(\t)", token):
            if piece == "\t":
                pad = max(1, round((stop - x % stop) / space))
                out.append("&nbsp;" * pad)
                x += pad * space
            else:
                x += width(unescape(piece))
                out.append(piece)
    return "".join(out)


def list_of_documents_html(ctx, rows):
    """`rows`: [(particulars, page range)]."""
    cause_css = f"font-family: {ctx['causeFont']}, Times, serif;" if ctx["causeFont"] else ""
    if ctx["causeFontSize"]:
        cause_css += f" font-size: {ctx['causeFontSize']}pt;"
    cause_html = _tabs_to_stops(ctx["causeTitleHtml"], ctx["causeFont"], ctx["causeFontSize"] or 12)
    top = f'<div style="{html_escape(cause_css, quote=True)}">{cause_html}</div>' if ctx["causeTitleHtml"] \
        else "<p>&nbsp;</p>" * LIST_OF_DOCUMENTS_BLANK_LINES
    body = "".join(
        f"<tr><td style=\"text-align:center\">{i}</td><td>{html_escape(text)}</td><td style=\"text-align:center\">{html_escape(pages)}</td></tr>"
        for i, (text, pages) in enumerate(rows, 1))
    css = ("body { font-family: Times, serif; font-size: 12pt; line-height: 1.4; color: #000; }"
           " p { margin: 0; } table { border-collapse: collapse; width: 100%; }"
           " td, th { border: 1px solid #000; padding: 5pt; vertical-align: top; text-align: left; }")
    # Story ignores float and table widths, so push the role to the right margin with a run of non-breaking spaces.
    role = ctx.get("role", "")
    measure = lambda text: fitz.get_text_length(text, fontname="tiro", fontsize=12)
    line_width = fitz.paper_rect("a4").width - 2 * 64
    gap = "&nbsp;" * max(1, int((line_width - measure("Date:") - measure(role)) / measure(" ") * 0.9)) if role else ""
    return (f"<html><head><style>{css}</style></head><body>{top}"
            '<p style="text-align:center; margin: 16pt 0 12pt;"><b><u>List of Documents</u></b></p>'
            '<table><tr><th style="width:12%; text-align:center">Sl.No.</th><th>Particulars</th><th style="width:16%; text-align:center">Pg.Nos.</th></tr>'
            f'{body}</table><p style="margin-top: 56pt;">{html_escape(ctx["location"]) or "&nbsp;"}</p>'
            f'<p>Date:{gap}{html_escape(role)}</p></body></html>')


def list_of_documents_pdf(ctx, rows):
    """The List of Documents as an (unnumbered) A4 PDF, flowing onto more pages as needed."""
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (64, 64, -64, -64)
    story = fitz.Story(html=list_of_documents_html(ctx, rows))
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    more = 1
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    return buf.getvalue()



def _annexure_payload(report_id, saved, page_numbers=None, annotations=None, doc_numbers=None, list_of_documents=None):
    """The annexure as the page shows it: the saved [{"id", "page_mode", "page_range"}] order,
    with every document the report's snippets come from present (appended if
    missing -- they can't be left out) and each one's snippet pages; documents
    the user can no longer view are dropped. "snippets" mode only makes sense for
    a document with snippet pages; any other falls back to every page."""
    snippet_pages = storage.report_snippet_pages(report_id)
    docs = {d["id"]: d for d in list_source_docs()}
    order = [d for d in saved if d["id"] in docs]
    order += [{"id": d, "page_mode": "all", "page_range": ""} for d in snippet_pages
              if d in docs and d not in {o["id"] for o in order}]
    return {
        "annotations": storage.get_annexure_annotations(report_id) if annotations is None else annotations,
        "pageNumbers": sanitize_page_numbers(page_numbers if page_numbers is not None else storage.get_annexure_page_numbers(report_id),
                                             ANNEXURE_DEFAULT_PAGE_NUMBERS),
        "docNumbers": sanitize_doc_numbers(doc_numbers if doc_numbers is not None else storage.get_annexure_doc_numbers(report_id)),
        "listOfDocuments": {
            "enabled": storage.get_annexure_list_of_documents(report_id) if list_of_documents is None else list_of_documents,
            **annexure_list_context(storage.get_report(report_id) or {}),
        },
        "documents": [
            {"id": o["id"], "title": docs[o["id"]]["title"], "type": docs[o["id"]]["type"],
             "description": docs[o["id"]].get("description", ""),
             "snippet_pages": snippet_pages.get(o["id"], []), "locked": o["id"] in snippet_pages,
             "page_mode": "all" if o["page_mode"] == "snippets" and o["id"] not in snippet_pages else o["page_mode"],
             "page_range": o["page_range"]}
            for o in order
        ],
        "available": [
            {"id": d["id"], "title": d["title"], "type": d["type"], "description": d.get("description", "")}
            for d in docs.values() if d["id"] not in {o["id"] for o in order}
        ],
    }


@app.route("/api/report/<report_id>/annexure", methods=["GET", "POST"])
def api_report_annexure(report_id):
    check_report_id(report_id)
    require_role("report", report_id, "viewer" if request.method == "GET" else "editor")
    if not storage.report_exists(report_id):
        raise DocumentError(f"No report with id {report_id!r}", 404)
    saved = storage.get_annexure(report_id)
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        items = body.get("documents")
        if not isinstance(items, list):
            raise DocumentError("documents must be a list of {id, page_mode, page_range}", 400)
        saved = []
        for item in items:
            d = item.get("id") if isinstance(item, dict) else None
            check_doc_id(d)
            if all(d != x["id"] for x in saved) and has_role("source", d, "viewer") and storage.document_exists(d):
                mode = item.get("page_mode", "all")
                page_range = str(item.get("page_range") or "").strip()
                if mode not in ("all", "snippets", "custom"):
                    raise DocumentError(f"Invalid page mode: {mode!r}", 400)
                if mode == "custom" and parse_page_range(page_range, 1 << 30) is None:
                    raise DocumentError(f"Invalid page range: {page_range!r} (use e.g. 1-3, 7)", 400)
                saved.append({"id": d, "page_mode": mode, "page_range": page_range if mode == "custom" else ""})
        page_numbers = None
        if "pageNumbers" in body:
            page_numbers = sanitize_page_numbers(body["pageNumbers"], storage.get_annexure_page_numbers(report_id))
            storage.save_annexure_page_numbers(report_id, page_numbers)
        doc_numbers = None
        if "docNumbers" in body:
            doc_numbers = sanitize_doc_numbers(body["docNumbers"], storage.get_annexure_doc_numbers(report_id))
            storage.save_annexure_doc_numbers(report_id, doc_numbers)
        list_of_documents = None
        if "listOfDocuments" in body:
            list_of_documents = bool(body["listOfDocuments"])
            storage.save_annexure_list_of_documents(report_id, list_of_documents)
        annotations = None
        if "annotations" in body:
            annotations = body["annotations"]
            if annotations not in ANNEXURE_ANNOTATION_MODES:
                raise DocumentError("annotations must be 'none', 'blackouts' or 'all'", 400)
            storage.save_annexure_annotations(report_id, annotations)
        payload = _annexure_payload(report_id, saved, page_numbers, annotations, doc_numbers, list_of_documents)
        storage.save_annexure(report_id, [{k: d[k] for k in ("id", "page_mode", "page_range")} for d in payload["documents"]])
        return jsonify(payload)
    return jsonify(_annexure_payload(report_id, saved))


def _annexure_pdf_bytes(payload):
    """The annexure payload rendered as one PDF (bytes)."""
    out = fitz.open()
    first_pages = []  # index in `out` of each document's first page
    spans = []  # (document, first, last index in `out`) of each document with pages
    try:
        for d in payload["documents"]:
            pdf_bytes, _ = _get_pdf_bytes(d["id"], d["type"])
            with fitz.open(stream=pdf_bytes, filetype="pdf") as src:
                pages = annexure_selected_pages(d, src.page_count)
                if payload["annotations"] != "none":
                    for n, raw_anns in storage.get_all_annotations(d["id"]).items():
                        if n.isdigit() and 1 <= int(n) <= src.page_count:
                            draw_annotations_on_page(src[int(n) - 1], annexure_annotations_to_draw(raw_anns, payload["annotations"]), src[int(n) - 1].rect)
                if pages:
                    first_pages.append(out.page_count)
                    spans.append((d, out.page_count, out.page_count + len(pages) - 1))
                for p in pages:
                    out.insert_pdf(src, from_page=p - 1, to_page=p - 1)
        pn = payload["pageNumbers"]
        band = {"left": 36, "right": 36, "header": ANNEXURE_PAGE_NUMBER_BAND, "footer": ANNEXURE_PAGE_NUMBER_BAND}
        stamp_page_numbers(out, pn, band)
        stamp_doc_numbers(out, payload["docNumbers"], pn, band, first_pages)
        if payload["listOfDocuments"]["enabled"]:
            # Added after the stamping, so its pages stay unnumbered and the annexure's numbering is unchanged.
            dn = payload["docNumbers"]
            rows = []
            for n, (d, first, last) in enumerate(spans):
                text = (d["description"] or d["title"]).strip()
                rows.append((f"{doc_number_label(dn, n)} - {text}" if dn["enabled"] else text, annexure_page_range(first, last, pn)))
            with fitz.open(stream=list_of_documents_pdf(payload["listOfDocuments"], rows), filetype="pdf") as front:
                out.insert_pdf(front, start_at=0)
        data = out.tobytes()
    finally:
        out.close()
    return data


@app.route("/api/report/<report_id>/annexure/refs")
def api_report_annexure_refs(report_id):
    """The current text of each annexure reference the report editor can show, by document id and source page."""
    check_report_id(report_id)
    require_role("report", report_id, "viewer")
    return jsonify(annexure_ref_texts(report_id))


@app.route("/api/report/<report_id>/annexure/export")
def api_report_annexure_export(report_id):
    """The saved annexure as one PDF: the annexed documents' pages, in order."""
    check_report_id(report_id)
    require_role("report", report_id)
    report = storage.get_report(report_id)
    if report is None:
        raise DocumentError(f"No report with id {report_id!r}", 404)
    saved = storage.get_annexure(report_id)
    payload = _annexure_payload(report_id, saved)
    if not payload["documents"]:
        raise DocumentError("The annexure is empty — add a document before downloading", 400)
    data = _annexure_pdf_bytes(payload)

    resp = Response(data, mimetype="application/pdf")
    resp.headers["Cache-Control"] = "no-store"
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", report["name"] or report_id).strip("-") or report_id
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}-annexure.pdf"'
    return resp


@app.route("/api/report/<report_id>/annexure/list.docx")
def api_report_annexure_list_docx(report_id):
    """The annexure's List of Documents as a Word file (whether or not the PDF includes it)."""
    check_report_id(report_id)
    require_role("report", report_id)
    report = storage.get_report(report_id)
    if report is None:
        raise DocumentError(f"No report with id {report_id!r}", 404)
    payload = _annexure_payload(report_id, storage.get_annexure(report_id))
    if not payload["documents"]:
        raise DocumentError("The annexure is empty — add a document before downloading", 400)

    dn, pn = payload["docNumbers"], payload["pageNumbers"]
    rows = []
    shown = 0  # annexure pages laid out so far, across documents
    for d in payload["documents"]:
        pdf_bytes, _ = _get_pdf_bytes(d["id"], d["type"])
        with fitz.open(stream=pdf_bytes, filetype="pdf") as src:
            count = len(annexure_selected_pages(d, src.page_count))
        if count:
            text = (d["description"] or d["title"]).strip()
            rows.append((f"{doc_number_label(dn, len(rows))} - {text}" if dn["enabled"] else text,
                         annexure_page_range(shown, shown + count - 1, pn)))
            shown += count

    data = list_of_documents_docx(annexure_list_context(report, include_doc=True), rows)
    resp = Response(data, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    resp.headers["Cache-Control"] = "no-store"
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", report["name"] or report_id).strip("-") or report_id
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}-list-of-documents.docx"'
    return resp


@app.route("/api/report/<report_id>/export")
def api_report_export(report_id):
    check_report_id(report_id)
    require_role("report", report_id)
    data = storage.get_report(report_id)
    if data is None:
        raise DocumentError(f"No report with id {report_id!r}", 404)

    title = data.get("name") or report_id
    doc_json = data.get("doc")
    if doc_json is None:
        raise DocumentError("This report was saved by an older editor version — open and save it once to upgrade it before exporting", 400)
    if not doc_json.get("content"):
        raise DocumentError("Report is empty — add some content before exporting", 400)

    texts, positions, total = annexure_ref_data(report_id)
    payload = None
    if request.args.get("annexures") == "1":
        payload = _annexure_payload(report_id, storage.get_annexure(report_id))
    # Snippets link to their annexure page only when the annexure is part of the file.
    links = [] if payload and payload["documents"] else None
    pdf_bytes = render_report_pdf(title, inline_doc_images(resolve_snippet_refs(doc_json, texts, links is not None)),
                                  data.get("margins"), data.get("pageNumbers"), links)

    if links is not None:
        annex_bytes = _annexure_pdf_bytes(payload)
        with fitz.open(stream=pdf_bytes, filetype="pdf") as merged, fitz.open(stream=annex_bytes, filetype="pdf") as annex:
            report_pages = merged.page_count
            front = annex.page_count - total  # the List of Documents, ahead of the annexed pages
            merged.insert_pdf(annex)
            for pno, key, rect in links:
                doc_id, page = key.rsplit(":", 1)
                target = positions.get((doc_id, int(page)))
                if target is not None:
                    merged[pno].insert_link({"kind": fitz.LINK_GOTO, "from": rect, "page": report_pages + front + target,
                                             "to": fitz.Point(0, 0)})
            pdf_bytes = merged.tobytes()

    resp = Response(pdf_bytes, mimetype="application/pdf")
    resp.headers["Cache-Control"] = "no-store"
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-") or report_id
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}.pdf"'
    return resp


@app.route("/api/report/<report_id>/export.docx")
def api_report_export_docx(report_id):
    check_report_id(report_id)
    require_role("report", report_id)
    data = storage.get_report(report_id)
    if data is None:
        raise DocumentError(f"No report with id {report_id!r}", 404)

    title = data.get("name") or report_id
    doc_json = data.get("doc")
    if doc_json is None:
        raise DocumentError("This report was saved by an older editor version — open and save it once to upgrade it before exporting", 400)
    if not doc_json.get("content"):
        raise DocumentError("Report is empty — add some content before exporting", 400)

    docx_bytes = render_report_docx(title, inline_doc_images(resolve_snippet_refs(doc_json, annexure_ref_texts(report_id))), data.get("margins"), data.get("pageNumbers"))

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


@app.route("/api/allegation-cases", methods=["GET", "POST"])
def api_allegation_cases():
    if request.method == "GET":
        allegation_counts = storage.count_allegations_by_case()
        # ?default_cause=1: only the cases under the user's default cause
        # (the one picked in the title bar), which is what the cases and
        # allegations workspaces show.
        only_cause = resolve_default_cause_id(create=False) if request.args.get("default_cause") else None
        items = []
        for case in storage.list_cases(g.user.id):
            if only_cause and case["cause_id"] != only_cause:
                continue
            items.append({
                "id": case["id"],
                "name": case["name"] or case["id"],
                "cause_id": case["cause_id"],
                "court": case["court"],
                "case_number": case["case_number"],
                "summary": case["summary"],
                "allegation_count": allegation_counts.get(case["id"], 0),
                "hearing_count": len(case["hearings"]),
                "role": case["role"],
                "created_at": case["created_at"],
                "updated_at": case["updated_at"],
            })
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return jsonify(items)

    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:200]
    if not name:
        raise DocumentError("A case name is required", 400)

    # A case's cause is mandatory: an explicit cause_id wins (the user must
    # be able to edit that cause), else fall back to whichever cause they
    # most recently used -- see resolve_default_cause_id, which is
    # guaranteed to return a real, editable id.
    raw_cause_id = body.get("cause_id")
    cause_id = require_editable_cause(raw_cause_id) if raw_cause_id else resolve_default_cause_id()
    storage.set_default_cause(g.user.id, cause_id)

    case_id = f"{slugify_report_name(name)}-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "name": name,
        "cause_id": cause_id,
        "court": _sanitize_text(body.get("court"), CASE_MAX_COURT_CHARS),
        "case_number": _sanitize_text(body.get("case_number"), CASE_MAX_NUMBER_CHARS),
        "summary": _sanitize_text(body.get("summary"), ALLEGATION_MAX_TEXT_CHARS),
        **sanitize_cause_title_settings({}, {}),
        "hearings": [],
        "created_at": now,
        "updated_at": now,
    }
    storage.save_case(case_id, data, owner_id=g.user.id)
    return jsonify({"id": case_id, **data})


@app.route("/api/allegation-case/<case_id>", methods=["GET", "POST", "DELETE"])
def api_allegation_case(case_id):
    check_report_id(case_id)
    role = require_role("case", case_id, {"GET": "viewer", "DELETE": "owner"}.get(request.method, "editor"))
    existing = storage.get_case(case_id)
    if existing is None:
        raise DocumentError(f"No case with id {case_id!r}", 404)

    if request.method == "GET":
        return jsonify({**existing, "role": role})

    if request.method == "DELETE":
        storage.delete_case(case_id)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    name = str(body.get("name", existing.get("name", ""))).strip()[:200]
    if not name:
        raise DocumentError("A case name is required", 400)

    # A case's cause is mandatory. Keeping its current cause needs no
    # further check; moving it under a different one needs edit access to
    # that cause, just like creating a case there would.
    cause_id = body.get("cause_id", existing["cause_id"]) or existing["cause_id"]
    if cause_id != existing["cause_id"]:
        require_editable_cause(cause_id)
        storage.set_default_cause(g.user.id, cause_id)

    existing_doc_ids = {
        d["doc_id"] for h in existing.get("hearings", []) for d in h["submitted_docs"] + h["received_docs"]
    }

    # Each caller (the allegations editor, the cases workspace) only ever
    # sends the fields it owns -- falling back to the value already saved
    # for everything else (via dict.get's default, not truthiness) means one
    # page's save can never clobber the other's data.
    data = {
        "name": name,
        "cause_id": cause_id,
        "court": _sanitize_text(body.get("court", existing.get("court", "")), CASE_MAX_COURT_CHARS),
        "case_number": _sanitize_text(body.get("case_number", existing.get("case_number", "")), CASE_MAX_NUMBER_CHARS),
        "summary": _sanitize_text(body.get("summary", existing.get("summary", "")), ALLEGATION_MAX_TEXT_CHARS),
        **sanitize_cause_title_settings(body, existing),
        "parties": sanitize_parties(body.get("parties"), existing.get("parties", [])),
        "hearings": sanitize_hearings(body.get("hearings", existing.get("hearings", [])),
                                      linkable_ids("source", existing_doc_ids)),
        "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    storage.save_case(case_id, data)
    return jsonify(data)


# ---------------------------------------------------------------------------
# API: cause title templates. Anyone can read them (the case details page
# generates a case's cause title from one); only admins manage them.
# ---------------------------------------------------------------------------

@app.route("/api/cause-title-templates", methods=["GET", "POST"])
def api_cause_title_templates():
    if request.method == "GET":
        return jsonify(storage.list_cause_title_templates())
    require_admin()
    name, body = _cause_title_template_fields(request.get_json(silent=True) or {})
    template_id = f"{slugify_report_name(name)}-{uuid.uuid4().hex[:6]}"
    storage.save_cause_title_template(template_id, name, body)
    return jsonify({"id": template_id, "name": name, "body": body})


@app.route("/api/cause-title-template/<template_id>", methods=["PUT", "DELETE"])
def api_cause_title_template(template_id):
    require_admin()
    if template_id not in {t["id"] for t in storage.list_cause_title_templates()}:
        raise DocumentError("No such cause title template", 404)
    if request.method == "DELETE":
        storage.delete_cause_title_template(template_id)
        return jsonify({"ok": True})
    name, body = _cause_title_template_fields(request.get_json(silent=True) or {})
    storage.save_cause_title_template(template_id, name, body)
    return jsonify({"id": template_id, "name": name, "body": body})


def _cause_title_template_fields(data):
    name = _sanitize_text(data.get("name"), 200)
    # The body is a ProseMirror doc (as a JSON string, or an object) from the
    # admin page's rich-text editor.
    raw = data.get("body")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    doc = sanitize_report_doc(raw, CAUSE_TITLE_TEMPLATE_MAX_CHARS)
    if not name or not _doc_has_text(doc):
        raise DocumentError("A template needs a name and some text", 400)
    return name, json.dumps(doc, separators=(",", ":"))


def _doc_has_text(node):
    if node.get("type") == "text":
        return bool(node.get("text", "").strip())
    return any(_doc_has_text(c) for c in node.get("content", []))


# ---------------------------------------------------------------------------
# API: causes, each holding an ordered list of goals (see sanitize_goals
# above). A cause can only be deleted once it has no goals, and the UI only
# offers to delete a goal once it has no linked cases -- mirroring the
# case/hearing deletion rules just above.
# ---------------------------------------------------------------------------

@app.route("/api/causes", methods=["GET", "POST"])
def api_causes():
    if request.method == "GET":
        items = []
        for cause in storage.list_causes(g.user.id):
            items.append({
                "id": cause["id"],
                "title": cause["title"] or cause["id"],
                "description": cause["description"],
                "goal_count": len(cause["goals"]),
                "role": cause["role"],
                "created_at": cause["created_at"],
                "updated_at": cause["updated_at"],
            })
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return jsonify(items)

    body = request.get_json(silent=True) or {}
    title = str(body.get("title") or "").strip()[:ALLEGATION_MAX_TITLE_CHARS]
    if not title:
        raise DocumentError("A cause title is required", 400)

    cause_id = f"{slugify_report_name(title)}-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "title": title,
        "description": _sanitize_text(body.get("description"), ALLEGATION_MAX_TEXT_CHARS),
        "goals": [],
        "created_at": now,
        "updated_at": now,
    }
    storage.save_cause(cause_id, data, owner_id=g.user.id)
    return jsonify({"id": cause_id, **data})


@app.route("/api/cause/<cause_id>", methods=["GET", "POST", "DELETE"])
def api_cause(cause_id):
    check_report_id(cause_id)
    role = require_role("cause", cause_id, {"GET": "viewer", "DELETE": "owner"}.get(request.method, "editor"))
    existing = storage.get_cause(cause_id)
    if existing is None:
        raise DocumentError(f"No cause with id {cause_id!r}", 404)

    if request.method == "GET":
        return jsonify({**existing, "role": role})

    if request.method == "DELETE":
        # Another cause takes over as the default (if this was it) and as the
        # home of any report/source whose only association was this cause.
        fallback_cause_id = resolve_default_cause_id(create=False, exclude_cause_id=cause_id)
        if not fallback_cause_id:
            raise DocumentError("Cannot delete your last remaining cause", 400)
        try:
            storage.delete_cause(cause_id, fallback_cause_id, g.user.id)
        except psycopg2.errors.ForeignKeyViolation:
            raise DocumentError("The replacement cause was just deleted; try again", 409)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    title = str(body.get("title", existing.get("title", ""))).strip()[:ALLEGATION_MAX_TITLE_CHARS]
    if not title:
        raise DocumentError("A cause title is required", 400)

    data = {
        "title": title,
        "description": _sanitize_text(body.get("description", existing.get("description", "")), ALLEGATION_MAX_TEXT_CHARS),
        "goals": sanitize_goals(
            body.get("goals", existing.get("goals", [])),
            linkable_ids("case", (cid for goal in existing.get("goals", []) for cid in goal["case_ids"])),
        ),
        "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    storage.save_cause(cause_id, data)
    return jsonify(data)


# ---------------------------------------------------------------------------
# API: global allegations (each optionally linked to one or more cases)
# ---------------------------------------------------------------------------

@app.route("/api/allegations", methods=["GET", "POST"])
def api_allegations():
    if request.method == "GET":
        # ?default_cause=1: only the allegations under the user's default
        # cause (the one picked in the title bar).
        if request.args.get("default_cause"):
            only_cause = resolve_default_cause_id(create=False)
            return jsonify(with_allegation_roles(storage.list_allegations({only_cause})) if only_cause else [])
        return jsonify(with_allegation_roles(visible_allegations()))

    body = request.get_json(silent=True) or {}
    # An allegation's cause is mandatory: an explicit cause_id wins (the user
    # must be able to edit that cause), else their default cause.
    raw_cause_id = body.get("cause_id")
    cause_id = require_editable_cause(raw_cause_id) if raw_cause_id else resolve_default_cause_id()
    allegation_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    allowed_report_ids = linkable_ids("report")
    inculpatory = sanitize_evidence_list(body.get("inculpatory"), allowed_report_ids)
    exculpatory = sanitize_evidence_list(body.get("exculpatory"), allowed_report_ids)
    valid_evidence_ids = {e["id"] for e in inculpatory} | {e["id"] for e in exculpatory}
    data = {
        "cause_id": cause_id,
        "title": _sanitize_text(body.get("title"), ALLEGATION_MAX_TITLE_CHARS),
        "description": _sanitize_text(body.get("description"), ALLEGATION_MAX_TEXT_CHARS),
        "to_prove": sanitize_to_prove_list(body.get("to_prove"), valid_evidence_ids),
        "inculpatory": inculpatory,
        "exculpatory": exculpatory,
        "case_ids": sanitize_case_ids(body.get("case_ids"), linkable_ids("case")),
        "created_at": now,
        "updated_at": now,
    }
    storage.save_allegation(allegation_id, data)
    storage.grant_owner("allegation", allegation_id, g.user.id)
    return jsonify({"id": allegation_id, **data})


@app.route("/api/allegation/<allegation_id>", methods=["GET", "POST", "DELETE"])
def api_allegation_item(allegation_id):
    check_report_id(allegation_id)
    existing = storage.get_allegation(allegation_id)
    if existing is None:
        raise DocumentError(f"No allegation with id {allegation_id!r}", 404)
    require_allegation_role(existing, "viewer" if request.method == "GET" else "editor")

    if request.method == "GET":
        return jsonify(with_allegation_roles([existing])[0])

    if request.method == "DELETE":
        storage.delete_allegation(allegation_id)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    allowed_report_ids = linkable_ids(
        "report", (e["report_id"] for e in existing.get("inculpatory", []) + existing.get("exculpatory", []))
    )
    inculpatory = sanitize_evidence_list(body.get("inculpatory", existing.get("inculpatory", [])), allowed_report_ids)
    exculpatory = sanitize_evidence_list(body.get("exculpatory", existing.get("exculpatory", [])), allowed_report_ids)
    valid_evidence_ids = {e["id"] for e in inculpatory} | {e["id"] for e in exculpatory}
    # Keeping its current cause needs no check; moving it needs edit access
    # to the new one.
    cause_id = body.get("cause_id", existing["cause_id"]) or existing["cause_id"]
    if cause_id != existing["cause_id"]:
        require_editable_cause(cause_id)
    data = {
        "cause_id": cause_id,
        "title": _sanitize_text(body.get("title", existing.get("title", "")), ALLEGATION_MAX_TITLE_CHARS),
        "description": _sanitize_text(body.get("description", existing.get("description", "")), ALLEGATION_MAX_TEXT_CHARS),
        "to_prove": sanitize_to_prove_list(body.get("to_prove", existing.get("to_prove", [])), valid_evidence_ids),
        "inculpatory": inculpatory,
        "exculpatory": exculpatory,
        "case_ids": sanitize_case_ids(body.get("case_ids", existing.get("case_ids", [])),
                                      linkable_ids("case", existing.get("case_ids", []))),
        "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    storage.save_allegation(allegation_id, data)
    return jsonify({"id": allegation_id, **data})


@app.route("/api/allegations/order", methods=["POST"])
def api_allegations_order():
    body = request.get_json(silent=True) or {}
    raw_order = body.get("order")
    if not isinstance(raw_order, list):
        raise DocumentError("order must be a list of allegation ids", 400)
    candidates = [aid for aid in raw_order[:2000] if isinstance(aid, str) and DOC_ID_RE.match(aid)]
    visible = {x["id"] for x in visible_allegations()}
    full_order = storage.set_allegation_order([aid for aid in candidates if aid in visible])
    order = [aid for aid in full_order if aid in visible]  # never echo ids the user can't see
    return jsonify({"order": order})


@app.route("/api/me/default-cause", methods=["POST"])
def api_default_cause():
    """Picking a cause in the title-bar picker makes it the user's default
    -- the cause every report/source they create is associated with, so it
    has to be one they can edit."""
    body = request.get_json(silent=True) or {}
    cause_id = require_editable_cause(body.get("cause_id"))
    storage.set_default_cause(g.user.id, cause_id)
    return jsonify({"default_cause_id": cause_id})


# ---------------------------------------------------------------------------
# API: associating a report or source with further causes/cases (it gets
# its first association -- the creator's default cause -- when it's created). Adding or removing one needs
# edit access to both sides. The last association can't be removed, so a
# report/source always stays associated with at least one cause or case.
# ---------------------------------------------------------------------------

def _update_links(kind, object_id):
    check_report_id(object_id) if kind == "report" else check_doc_id(object_id)
    require_role(kind, object_id, "editor")
    body = request.get_json(silent=True) or {}
    target_kind, target_id = require_link_target(body, f"this {ACCESS_LABELS[kind]}")

    if request.method == "POST":
        storage.link_to(kind, object_id, target_kind, target_id)
    else:
        item = storage.get_report(object_id) if kind == "report" else next(
            (d for d in storage.list_documents(g.user.id) if d["id"] == object_id), None)
        links = item["cause_ids"] + item["case_ids"]
        if target_id not in item[f"{target_kind}_ids"]:
            raise DocumentError(f"This {ACCESS_LABELS[kind]} isn't associated with that {target_kind}", 404)
        if len(links) <= 1:
            raise DocumentError(f"A {ACCESS_LABELS[kind]} must stay associated with at least one cause or case", 400)
        storage.unlink_from(kind, object_id, target_kind, target_id)
    return jsonify({"ok": True})


@app.route("/api/report/<report_id>/links", methods=["POST", "DELETE"])
def api_report_links(report_id):
    return _update_links("report", report_id)


@app.route("/api/doc/<doc_id>/links", methods=["POST", "DELETE"])
def api_doc_links(doc_id):
    return _update_links("source", doc_id)


def require_admin():
    if not g.user.is_admin:
        raise DocumentError("You don't have permission to see this page", 403)


def require_content_creator():
    if not g.user.is_content_creator:
        raise DocumentError("You don't have permission to see this page", 403)


def require_admin_or_content_creator():
    if not (g.user.is_admin or g.user.is_content_creator):
        raise DocumentError("You don't have permission to see this page", 403)


# ---------------------------------------------------------------------------
# Admin: the Users tab (roles + per-user stats, admin-only) and the
# Websites tab (websites/sections, open to any content creator too, not
# just admins -- see db/migrations/022_content_platform.sql).
# ---------------------------------------------------------------------------

WEBSITE_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


@app.route("/admin")
def admin_view():
    require_admin_or_content_creator()
    users = storage.admin_list_users() if g.user.is_admin else None
    return render_template("admin.html", users=users, websites=storage.list_websites(), is_admin=g.user.is_admin,
                           title_templates=storage.list_cause_title_templates() if g.user.is_admin else None)


@app.route("/api/admin/user/<int:user_id>/roles", methods=["PUT"])
def api_admin_user_roles(user_id):
    require_admin()
    body = request.get_json(silent=True) or {}
    is_admin = bool(body.get("is_admin"))
    is_content_creator = bool(body.get("is_content_creator"))
    if not is_admin and user_id == g.user.id and storage.count_admins() <= 1:
        raise DocumentError("You can't remove your own administrator access while you're the only administrator", 400)
    storage.set_user_roles(user_id, is_admin, is_content_creator)
    return jsonify({"ok": True})


@app.route("/api/admin/websites", methods=["POST"])
def api_admin_websites():
    require_admin_or_content_creator()
    body = request.get_json(silent=True) or {}
    domain = str(body.get("domain") or "").strip().lower()
    name = str(body.get("name") or "").strip()[:100]
    tagline = str(body.get("tagline") or "").strip()[:300]
    if not WEBSITE_DOMAIN_RE.match(domain):
        raise DocumentError("Enter a valid domain, e.g. example.com", 400)
    if not name:
        raise DocumentError("A website name is required", 400)
    if storage.website_exists(domain):
        raise DocumentError("A website with that domain already exists", 409)
    storage.create_website(domain, domain, name, tagline)
    return jsonify({"id": domain, "domain": domain, "name": name, "tagline": tagline, "sections": []})


@app.route("/api/admin/website/<website_id>", methods=["POST", "DELETE"])
def api_admin_website(website_id):
    require_admin_or_content_creator()
    if not storage.website_exists(website_id):
        raise DocumentError(f"No website with id {website_id!r}", 404)
    if request.method == "DELETE":
        storage.delete_website(website_id)
        return jsonify({"ok": True})
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:100]
    tagline = str(body.get("tagline") or "").strip()[:300]
    if not name:
        raise DocumentError("A website name is required", 400)
    storage.update_website(website_id, name, tagline)
    return jsonify({"ok": True})


@app.route("/api/admin/website/<website_id>/sections", methods=["POST"])
def api_admin_sections(website_id):
    require_admin_or_content_creator()
    if not storage.website_exists(website_id):
        raise DocumentError(f"No website with id {website_id!r}", 404)
    body = request.get_json(silent=True) or {}
    title = str(body.get("title") or "").strip()[:100]
    description = str(body.get("description") or "").strip()[:500]
    if not title:
        raise DocumentError("A section title is required", 400)
    section_id = f"{slugify_report_name(title)}-{uuid.uuid4().hex[:6]}"
    website = next((w for w in storage.list_websites() if w["id"] == website_id), None)
    position = len(website["sections"]) if website else 0
    storage.create_section(section_id, website_id, title, description, position)
    return jsonify({"id": section_id, "title": title, "description": description, "position": position})


@app.route("/api/admin/section/<section_id>", methods=["POST", "DELETE"])
def api_admin_section(section_id):
    require_admin_or_content_creator()
    if not storage.section_exists(section_id):
        raise DocumentError(f"No section with id {section_id!r}", 404)
    if request.method == "DELETE":
        storage.delete_section(section_id)
        return jsonify({"ok": True})
    body = request.get_json(silent=True) or {}
    title = str(body.get("title") or "").strip()[:100]
    description = str(body.get("description") or "").strip()[:500]
    if not title:
        raise DocumentError("A section title is required", 400)
    position = body.get("position")
    storage.update_section(section_id, title, description, position if isinstance(position, int) else 0)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Articles workspace: a content creator's own webpages -- the same kind of
# Tiptap document a report is (sanitize_report_doc applies unchanged), just
# never paginated, and arranged into website_sections rather than causes.
# See templates/articles.html, static/src/article-editor.js.
# ---------------------------------------------------------------------------

ARTICLE_IMAGE_MAX_PIXELS = 1600
ARTICLE_IMAGE_MAX_BYTES = 200 * 1024  # a webpage's images should be quick to download
ARTICLE_THUMBNAIL_MAX_SIZE = (400, 300)  # width, height -- wherever an image is shown as a thumbnail
ARTICLE_SUMMARY_MAX_CHARS = 300
ARTICLE_MAX_SECTIONS = 50  # generous; just a sanity cap on the client's list


def require_article(article_id):
    """An article this content creator authored -- articles aren't shared,
    so there's no viewer/editor role to check, just ownership."""
    check_report_id(article_id)
    article = storage.get_article(article_id)
    if article is None or article["author_id"] != g.user.id:
        raise DocumentError(f"No webpage with id {article_id!r}", 404)
    return article


def valid_section_ids(raw):
    """The section ids from the client -- possibly across several
    websites, possibly none (an unpublished draft) -- de-duplicated;
    anything listed must name a section that actually exists."""
    if not isinstance(raw, list):
        return []
    section_ids = []
    for item in raw[:ARTICLE_MAX_SECTIONS]:
        section_id = str(item or "").strip()
        if not section_id or section_id in section_ids:
            continue
        if not storage.section_exists(section_id):
            raise DocumentError(f"No section with id {section_id!r}", 400)
        section_ids.append(section_id)
    return section_ids


def valid_thumbnail_image_id(raw, article_id):
    """An image id from the client, or None for "no thumbnail" -- anything
    else must be one of this article's own uploaded images."""
    image_id = str(raw or "").strip()
    if not image_id:
        return None
    image = storage.get_article_image(image_id)
    if image is None or image["article_id"] != article_id:
        raise DocumentError(f"No image with id {image_id!r} in this webpage", 400)
    return image_id


@app.route("/articles")
def articles_view():
    require_content_creator()
    article_id = request.args.get("article", "")
    if article_id:
        require_article(article_id)
    return render_template("articles.html", article_id=article_id, websites=storage.list_websites())


@app.route("/api/articles", methods=["GET", "POST"])
def api_articles():
    require_content_creator()
    if request.method == "GET":
        return jsonify(storage.list_articles(g.user.id))

    body = request.get_json(silent=True) or {}
    title = str(body.get("title") or "").strip()[:200]
    if not title:
        raise DocumentError("A title is required", 400)
    article_id = f"{slugify_report_name(title)}-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "title": title, "doc": {"type": "doc", "content": []}, "summary": "",
        "section_ids": [], "published": False, "thumbnail_image_id": None,
        "created_at": now, "updated_at": now,
    }
    storage.create_article(article_id, data, g.user.id)
    return jsonify({"id": article_id, **data})


@app.route("/api/article/<article_id>", methods=["GET", "POST", "DELETE"])
def api_article(article_id):
    require_content_creator()
    existing = require_article(article_id)

    if request.method == "GET":
        return jsonify(existing)
    if request.method == "DELETE":
        storage.delete_article(article_id)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    title = str(body.get("title", existing.get("title", ""))).strip()[:200]
    if not title:
        raise DocumentError("A title is required", 400)
    summary = str(body.get("summary", existing.get("summary", ""))).strip()[:ARTICLE_SUMMARY_MAX_CHARS]
    section_ids = valid_section_ids(body.get("section_ids", existing.get("section_ids")))
    published = bool(body.get("published")) and bool(section_ids)
    thumbnail_image_id = valid_thumbnail_image_id(
        body.get("thumbnail_image_id", existing.get("thumbnail_image_id")), article_id,
    )
    doc_json = sanitize_report_doc(body.get("doc", existing.get("doc")))
    data = {
        "title": title, "doc": doc_json, "summary": summary, "section_ids": section_ids,
        "published": published, "thumbnail_image_id": thumbnail_image_id,
        "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    storage.save_article(article_id, data)
    return jsonify(data)


@app.route("/api/article/<article_id>/images")
def api_article_images(article_id):
    require_content_creator()
    require_article(article_id)
    images = storage.list_article_images(article_id)
    for image in images:
        image["url"] = url_for("api_article_image", article_id=article_id, image_id=image["id"], thumb=1)
    return jsonify(images)


def _compress_jpeg_under(img, max_bytes):
    """Encodes `img` as JPEG, stepping quality down and, once that's not
    enough, shrinking it further, until the result is at or under
    `max_bytes` -- so an inserted image stays quick to download. Always
    terminates: dimensions shrink each round once quality bottoms out,
    which forces the loop's exit condition within a handful of rounds."""
    quality = 88
    while True:
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality)
        data = out.getvalue()
        if len(data) <= max_bytes or (quality <= 35 and max(img.size) <= 320):
            return data
        if quality > 35:
            quality -= 12
        else:
            img = img.resize((max(1, img.width * 4 // 5), max(1, img.height * 4 // 5)))
            quality = 70


def compress_uploaded_image():
    """The JPEG bytes (at most ARTICLE_IMAGE_MAX_PIXELS a side and
    ARTICLE_IMAGE_MAX_BYTES long) for the "image" file in this request --
    shared by the webpage and report editors' Image buttons."""
    upload = request.files.get("image")
    if upload is None:
        raise DocumentError("Please choose an image.", 400)
    try:
        img = Image.open(upload.stream)
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise DocumentError("That file isn't an image we can read. Please choose a JPEG, PNG or GIF image.", 400)
    img.thumbnail((ARTICLE_IMAGE_MAX_PIXELS, ARTICLE_IMAGE_MAX_PIXELS))
    return _compress_jpeg_under(img, ARTICLE_IMAGE_MAX_BYTES)  # re-encoding also drops any metadata or embedded payload


@app.route("/api/article/<article_id>/image", methods=["POST"])
def api_article_image_upload(article_id):
    require_content_creator()
    require_article(article_id)
    data = compress_uploaded_image()
    image_id = uuid.uuid4().hex[:24]
    storage.create_article_image(article_id, image_id, "image/jpeg", data)
    return jsonify({"id": image_id, "url": url_for("api_article_image", article_id=article_id, image_id=image_id)})


@app.route("/api/report/<report_id>/image", methods=["POST"])
def api_report_image_upload(report_id):
    check_report_id(report_id)
    require_role("report", report_id, "editor")
    if not storage.report_exists(report_id):
        raise DocumentError(f"No report with id {report_id!r}", 404)
    data = compress_uploaded_image()
    image_id = uuid.uuid4().hex[:24]
    storage.create_report_image(report_id, image_id, "image/jpeg", data)
    return jsonify({"id": image_id, "url": url_for("api_report_image", report_id=report_id, image_id=image_id)})


@app.route("/media/report-images/<report_id>/<image_id>")
def api_report_image(report_id, image_id):
    check_report_id(report_id)
    if not has_role("report", report_id, "viewer"):
        abort(404)
    image = storage.get_report_image(image_id) if DOC_ID_RE.match(image_id) else None
    if image is None or image["report_id"] != report_id:
        abort(404)
    data, mimetype = image["data"], image["content_type"]
    if request.args.get("thumb"):
        data, mimetype = _thumbnail_response_parts(data, mimetype)
    resp = Response(data, mimetype=mimetype)
    resp.headers["Cache-Control"] = "private, max-age=31536000"  # an image's bytes never change
    return resp


def _shrink_to_thumbnail(data):
    """Re-encodes `data` (a JPEG) so it's no larger than
    ARTICLE_THUMBNAIL_MAX_SIZE -- wherever an image is shown as a
    thumbnail (a card, the thumbnail picker) rather than in the article's
    own body, it doesn't need to ship at full article-image size. Falls
    back to the original bytes if they turn out not to be a real image."""
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError):
        return data
    if img.width <= ARTICLE_THUMBNAIL_MAX_SIZE[0] and img.height <= ARTICLE_THUMBNAIL_MAX_SIZE[1]:
        return data
    img.thumbnail(ARTICLE_THUMBNAIL_MAX_SIZE)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def _thumbnail_response_parts(data, mimetype):
    """(bytes, mimetype) for a ?thumb=1 request: _shrink_to_thumbnail's
    output is JPEG whenever it actually resized, else the original."""
    small = _shrink_to_thumbnail(data)
    return (small, mimetype) if small is data else (small, "image/jpeg")


@app.route("/media/article-images/<article_id>/<image_id>")
def api_article_image(article_id, image_id):
    """Serves one of an article's embedded images -- reachable by anyone
    once the article is published (see auth.PUBLIC_ENDPOINTS), else only its
    author, so a draft's images can still be previewed while writing it.
    With ?thumb=1, shrinks it to thumbnail size first (see
    _shrink_to_thumbnail) -- used everywhere the image is shown as a
    thumbnail rather than inline in the article's own body."""
    check_report_id(article_id)
    article = storage.get_article(article_id)
    if article is None:
        abort(404)
    viewer = g.user
    allowed = article["published"] or (viewer is not None and not viewer.is_guest and viewer.id == article["author_id"])
    if not allowed:
        abort(404)
    image = storage.get_article_image(image_id)
    if image is None or image["article_id"] != article_id:
        abort(404)
    data = _shrink_to_thumbnail(image["data"]) if request.args.get("thumb") else image["data"]
    resp = Response(data, mimetype=image["content_type"])
    resp.headers["Cache-Control"] = "public, max-age=31536000" if article["published"] else "private, no-cache"
    return resp


# ---------------------------------------------------------------------------
# The public content aggregation page: every website's sections and their
# published articles, tastefully laid out, reachable by anyone -- see
# auth.PUBLIC_ENDPOINTS.
# ---------------------------------------------------------------------------

def current_website():
    """The website matching the host this request came in on (the same
    lookup inject_site_name does for the site name), or a sane default if
    the host isn't recognized (e.g. localhost in development)."""
    host = request.host.split(":")[0].lower().removeprefix("www.")
    site = storage.get_website_by_domain(host)
    if site is not None:
        return site
    sites = storage.list_websites()
    return next((s for s in sites if s["name"] == DEFAULT_SITE_NAME), sites[0] if sites else None)


@app.route("/public")
def public_view():
    site = current_website()
    if site is None:
        raise DocumentError("No website is configured yet", 404)
    return render_template(
        "public.html", website=site,
        sections=storage.public_sections(site["id"]),
        legal_tools=storage.public_legal_tools_section(site["id"]),
    )


@app.route("/article/<article_id>")
def public_article(article_id):
    check_report_id(article_id)
    article = storage.get_published_article(article_id)
    if article is None:
        raise DocumentError(f"No published webpage with id {article_id!r}", 404)
    body_html = _json_blocks_to_html((article["doc"] or {}).get("content") or [])
    return render_template("article_view.html", article=article, body_html=body_html)


@app.route("/media/snippets/<doc_id>/<path:filename>")
def api_snippet_file(doc_id, filename):
    check_doc_id(doc_id)
    normalize_type(request.args.get("type", "pdf"))
    if "/" in filename or "\\" in filename:
        abort(400)
    if not storage.can_view_snippet_images(g.user.id, doc_id):
        abort(404)
    data = storage.read_snippet_bytes(doc_id, filename)
    if data is None:
        abort(404)
    mimetype = "image/png"
    if request.args.get("thumb"):
        data, mimetype = _thumbnail_response_parts(data, mimetype)
    resp = Response(data, mimetype=mimetype)
    # Snippet PNGs are rewritten in place when the page's annotations change.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 5050))
    app.run(host="127.0.0.1", port=port, debug=True)
