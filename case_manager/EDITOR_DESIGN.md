# Document editor — design notes

The document editor lets a user compose a free-text report (a "document"/"report") by
typing and by pulling in image snippets captured from source PDFs/Word docs via the
annotator (see `ANNOTATOR_DESIGN.md`), then export the result as a PDF. It lives at
`/document` and is implemented across:

- `templates/document.html` — page shell/toolbar markup
- `static/document.js` — all client-side behavior
- `static/style.css` — the "Google-Docs-ish" visual styling (`.docbar`, `.doc-toolbar`,
  `.editor-container`, `.editor`, `.page-break`, modals, landing grid)
- `app.py` — report CRUD endpoints, HTML sanitization, PDF export (`render_report_pdf`)

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/document` | GET | Renders `document.html`. With no `?report=` param, shows the landing/picker view. With `?report=<id>`, opens that report in the editor. Also accepts `?source=<id>&type=pdf\|docx` to pre-open the "new document" modal with a source pre-selected (used by the "Compile document »" link on the annotator page). |
| `/api/reports` | GET | List all saved reports (id, name, source doc/type, timestamps), newest-updated first. |
| `/api/reports` | POST | Create a new report. Body: `{name, source_doc, source_type}`. Server generates the id as `slugify(name)-<6 hex chars>`. |
| `/api/report/<id>` | GET | Fetch one report's full record (`name`, `html`, `source_doc`, `source_type`, timestamps). |
| `/api/report/<id>` | POST | Save/update a report. Body: `{name, html, source_doc, source_type}`. Server re-sanitizes `html` before persisting (§ Sanitization). |
| `/api/report/<id>/export` | GET | Renders the saved `html` to a PDF (`render_report_pdf`) and returns it as a download. |

Report records are stored one-per-file as `storage/reports/<id>.json`:

```json
{
  "name": "Case summary — Jane Doe",
  "html": "<h1>...</h1><p>...</p>",
  "source_doc": "some-doc-id",
  "source_type": "pdf",
  "created_at": "2026-08-24T03:25:00.374293+00:00",
  "updated_at": "2026-08-24T03:25:00.374293+00:00"
}
```

`html` is always the *sanitized* HTML — never trusted raw client input — and is what both
the browser (on reload) and the PDF exporter render from. There is no separate rich
document model (no JSON-based AST, no ProseMirror/Slate-style schema); the DOM *is* the
model, serialized as HTML.

## Page structure (`document.html`)

`#docApp` carries all the routing/URL context as `data-*` attributes so `document.js`
never has to construct API URLs itself beyond simple concatenation, and Jinja's
`url_for` stays the single source of truth for route paths.

- `.docbar` — top bar: back link, File menu (New/Open/Download as PDF), the document
  title `<input>`, an explicit **Save** button + autosave status text, and the source
  picker (`<select id="sourceSelect">` + "Annotate source »" link).
- `.doc-toolbar` (`#formatToolbar`) — a Google-Docs-style formatting toolbar: font
  family/size selects, paragraph style select (Normal/H1/H2/H3), bold/italic/underline,
  text color + highlight color pickers, alignment buttons (inline SVG icons), bullet/
  numbered list buttons. Hidden entirely (`.toolbar-hidden`) when no report is open.
- `.viewer-layout` → `.editor-container` → `#editor` — the single
  `contenteditable="true"` surface the user types into (see § Pagination below for how
  this became multi-page). When no report is open, this area instead shows a
  `.doc-landing` grid of existing documents.
- `.sidebar` (`#reportSnippetList`) — snippets/annotations belonging to whatever source
  document is currently selected in the source picker; each has an "Insert" button that
  drops it into the editor at the caret.
- Two modals: **New document** (name + optional source doc) and **Open** (a list of all
  existing reports).

## `document.js` architecture

The whole file is one IIFE. It branches early on whether a report is open
(`if (!reportId) { loadLanding(); return; }`), so everything below that point — toolbar
wiring, pagination, autosave — only exists when actually editing a document.

### Landing / New / Open

- Landing view (`loadLanding`) and the Open modal (`openDocBtn`) both call
  `GET /api/reports` and render `.report-card` tiles via `renderReportCard`, which just
  links to `/document?report=<id>`.
- New document (`newDocBtn` → modal → `createDocument`) posts to `/api/reports`, then
  navigates to the newly created report's URL. If `?source=` was present in the current
  URL (arrived via the annotator's "Compile document »" link), the modal opens
  automatically pre-filled with that source.

### Save / autosave

State machine: `dirty`, `saving`, `saveAgainAfter`, `autosaveTimer`.

- Any edit (`editor` `input` event, title `input`, source picker `change`, or a
  formatting command) calls `markDirty()`, which sets a 1200ms debounce timer that
  calls `saveReport()`.
- The explicit **Save** button also calls `saveReport()` directly (bypassing the
  debounce) — added so a user isn't left wondering whether recent edits actually
  persisted, given autosave's default state text shows "Saving…" immediately but the
  real save only fires after the debounce.
- `saveReport()` guards against overlapping requests: if a save is already in flight,
  it doesn't fire a second one — it just sets `saveAgainAfter = true` so one more save
  runs immediately after the in-flight one resolves, guaranteeing the *latest* state
  eventually reaches the server without ever having two concurrent POSTs racing.
- `window.beforeunload` does a best-effort final flush via `navigator.sendBeacon` (fire-
  and-forget, since browsers no longer reliably support a blocking confirmation dialog
  tied to an async save).
- The payload's `html` is **not** `editor.innerHTML` directly — see § Pagination for why
  it goes through `getCleanEditorHtml()` first.

### Formatting toolbar

Built entirely on `document.execCommand(...)` (bold/italic/underline/foreColor/
hiliteColor/justify*/insert*List/formatBlock/fontName), which is deprecated but still
functional in every mainstream browser and avoids needing a real rich-text editor
framework for a fairly small feature surface.

Two setup calls matter a lot here:
- `execCommand("styleWithCSS", false, true)` — makes the browser express color/etc. as
  `<span style="...">` rather than legacy `<font color=...>` tags, because the server
  sanitizer's tag whitelist doesn't include `<font>` and would otherwise silently strip
  color formatting back out on save.
- `execCommand("defaultParagraphSeparator", false, "p")` — makes pressing Enter create
  `<p>` elements instead of Chrome's default bare `<div>`, so the DOM (and hence the
  pagination pass, which walks `#editor`'s direct children) has a predictable, semantic
  block structure.

**Selection save/restore**: clicking a toolbar button moves focus away from the editor,
which normally collapses/loses the text selection the command should apply to.
`saveSelection()`/`restoreSelection()` snapshot the current `Range` on every
`keyup`/`mouseup`/`blur` inside the editor, and every toolbar handler calls
`restoreSelection()` immediately before its `execCommand` call.

**Font size** is a special case: `execCommand("fontSize", ...)` only accepts the legacy
1–7 scale, not arbitrary point sizes. The workaround: apply size "7" (the max legacy
value, chosen because it's unambiguous to find afterward), then walk the DOM for
whatever the browser produced for it (`<font size="7">` in some engines, `<span
style="font-size:xxx-large">` in Chrome with `styleWithCSS` on), unwrap/normalize each
into a plain `<span style="font-size:<pt>pt">`, and — since replacing nodes invalidates
the live selection — reselect the new span(s) so a color change applied right after
still lands on the resized text rather than wherever the collapsed caret happens to be.

**Highlight** tries `hiliteColor` first and falls back to `backColor` (older
Firefox/Safari support one but not the other); "clear highlight" is just
`hiliteColor("transparent")`.

### Source picker & snippet sidebar

`sourceSelect` determines which source document's snippets/annotations show in the
sidebar — independent from (but often initialized from) the report's own
`source_doc`/`source_type`. Changing it: updates the "Annotate source »" link, reloads
the sidebar via `GET /api/doc/<id>/snippets?type=...`, and marks the report dirty (since
`source_doc`/`source_type` are themselves persisted fields on the report).

Inserting a snippet (`.insert-snippet` button) wraps its `<img>` in a
`<figure class="doc-figure"><img>...<figcaption>...</figcaption></figure>` and inserts
it at the caret via `execCommand("insertHTML", ...)`, followed by an empty paragraph so
the caret has somewhere to continue typing below the figure.

### Pagination — simulated A4 pages

Added so the editor visually shows page breaks as you type, rather than being one
infinitely tall scrolling surface, and so those breaks roughly line up with where the
PDF export will actually paginate.

**Sizing.** `.editor` is sized to A4 at 96dpi (794×1123px) with padding
(61.33px/48px) chosen to match `render_report_pdf`'s fitz mediabox inset of
`(36, 46, -36, -46)` pt, converted to px at `96/72`. `.editor`'s `line-height` was
tightened from an earlier `1.6` to `1.5` to match `REPORT_PDF_CSS`'s `line-height: 1.5`
— any mismatch there directly skews how many editor-pages a paragraph run takes versus
how many PDF-pages it takes.

**Algorithm** (`paginate()` in `document.js`): treats every direct element child of
`#editor` as an opaque "block" (whatever tag it is — `p`, `h1`, `figure`, `ul`, …).
Blocks are **never split** — this is a deliberate simplicity/robustness trade-off (see
below), not a limitation ProseMirror-style editors have; a block that doesn't fit on the
current page moves to the next page whole. On each pass:

1. Remove any existing `.page-break` spacer elements (so heights are measured against
   real content only).
2. Walk the remaining element children in order, tracking `used` height for the current
   simulated page.
3. For each block, measure `getBoundingClientRect().height` plus its computed
   top/bottom margins. If adding it would push `used` past `PAGE_CONTENT_HEIGHT` (≈1000px
   — A4 height minus top+bottom margins) *and* the current page isn't still empty (`used
   > 0`, so an oversized single block doesn't recurse into an infinite string of empty
   pages), insert a non-editable `<div class="page-break" contenteditable="false"
   data-page="N">` immediately before that block and reset `used` to 0.

**Why spacers are safe to insert/remove around live typing**: because they only ever go
*between* top-level blocks, never inside the block containing the caret, inserting or
removing them never invalidates the browser's current `Range`/selection — no manual
save/restore is needed the way the toolbar needs it. This is the key property that makes
the whole approach tractable without a virtual-DOM diffing layer.

**Trigger**: a `MutationObserver` on `#editor` (`childList + subtree + characterData`)
catches every source of change — typing, toolbar commands, undo/redo, snippet insertion,
initial `innerHTML` load — and schedules `paginate()` via `requestAnimationFrame`
(coalescing bursts of mutations, e.g. a fast typing run, into one measurement pass per
frame). `paginate()` disconnects the observer for the duration of its own DOM writes and
reconnects afterward, so its own spacer insert/remove calls don't re-trigger themselves.
Two extra triggers: an `img` `load` listener (image height is unknown/zero until it
loads, so a block containing one is re-measured once it does) and `window.resize` (the
editor can shrink below 794px on narrow viewports via `max-width:100%`, which reflows
line-wrapping and thus block heights).

**Keeping spacers out of the saved document**: `.page-break` elements are a pure view
artifact recomputed from scratch every pass — they must never reach the server. Both
places that used to read `editor.innerHTML` directly (the save payload and the
`beforeunload` sendBeacon) now call `getCleanEditorHtml()` instead, which clones
`#editor`, strips every `.page-break` from the clone, and serializes that. (Even if a
stray `.page-break` did leak into a save, the server's HTML sanitizer would strip its
`contenteditable` attribute — not in the attribute whitelist — but the `div.page-break`
element itself would survive, since `div` and `class` are both allowed; client-side
stripping is the real safety net here, not the sanitizer.)

**Known limitation — page breaks are an approximation, not exact.** The editor's
pagination only ever moves whole blocks; the PDF exporter (`render_report_pdf`, via
PyMuPDF's `Story`) does real flow layout and *can* split a paragraph mid-line to use
leftover space at the bottom of a page. On a long, paragraph-heavy document the editor
can therefore show one more page than the exported PDF actually has. Closing that gap
completely would require either reimplementing Story's line-breaking in the browser, or
switching the export path itself to render page-by-page from the browser's own layout
(e.g. headless Chrome print-to-PDF) — both explicitly out of scope; block-level
pagination was chosen as the right complexity/robustness trade-off for a contenteditable
surface (see chat history 2026-08-24: user explicitly chose "block-level" fidelity over
"line-level" and "A4" over "Letter" as the shared page size standard).

**Other known nuance**: pressing Backspace/Delete right at a page boundary can "consume"
one keypress on the non-editable `.page-break` div itself before it goes on to merge the
adjacent paragraphs, since a `contenteditable="false"` island is deleted as an atomic
unit. Because the whole spacer set is discarded and recomputed on every pass anyway, this
self-corrects immediately and never leaves stale/duplicate spacers behind.

## Server-side: sanitization, image inlining, PDF export

### HTML sanitization (`sanitize_report_html`)

A hand-rolled whitelist sanitizer (`_ReportHTMLSanitizer`, subclassing
`html.parser.HTMLParser`) — not a maintained library like `bleach` — runs on every
`POST /api/report/<id>`, over both new and previously-saved content (i.e. even the
"unchanged" parts of `existing.get("html")` get re-sanitized on the next save, so the
whitelist can be tightened later without needing a migration).

- `REPORT_ALLOWED_TAGS`: a fixed set of structural/text tags (`p`, headings, `strong`/
  `em`/`u`/`s`, lists, `table*`, `figure`/`figcaption`, `a`, `img`, `blockquote`, `hr`,
  `div`, `span`, `pre`, `code`). Anything else is dropped (tag stripped, but its inner
  content re-parsed and kept unless it's in `REPORT_STRIP_CONTENT_TAGS`).
- `REPORT_STRIP_CONTENT_TAGS` (`script`, `style`, `iframe`, `object`, `embed`, `form`,
  `input`, `button`, `svg`) drop the tag **and** everything inside it — these are
  actively dangerous or irrelevant to a document body, not just "not in the whitelist".
- Per-tag attribute whitelist (`_clean_report_attrs`): only `class` and `style`
  (style-string regex-validated to be alphanumeric/`:;#%.,-()` only, plus an explicit
  `"expression"`/`"javascript"` substring block against old IE `expression()` and
  `javascript:` tricks) are allowed generically; `href` (on `<a>`) and `src` (on `<img>`)
  must match `REPORT_SAFE_URL_RE` (`https?://`, `mailto:`, `/media/`, or a `data:image/…
  ;base64,` URI); `alt`/`width`/`height` on `<img>`; `colspan`/`rowspan` (digits only) on
  `<td>`/`<th>`.
- All text content is `html.escape`d on the way back out, so nothing round-trips as raw
  markup even if it slipped past tag filtering.

### Image inlining for export (`inline_report_images`)

Snippets inserted from the sidebar reference `/media/snippets/<doc>/<file>?type=...` —
a live server route. `fitz.Story` (used for PDF rendering) has no network/filesystem
access to fetch that at render time, so before export, every such `src` is rewritten to
a `data:image/png;base64,...` URI read directly off disk (`snippets_dir(...)`), making
the export's HTML fully self-contained. The regex-matched doc id is re-validated
(`DOC_ID_RE`) and the filename checked for path separators before touching the
filesystem, since this URL segment originated from stored (if sanitized) user content.

### PDF rendering (`render_report_pdf`)

Uses PyMuPDF's `Story`/`DocumentWriter` — a real (if fairly basic) CSS flow-layout
engine, *not* a browser or wkhtmltopdf-style renderer. `REPORT_PDF_CSS` sets base
typography (11pt Helvetica/Arial, 1.5 line-height, heading sizes, table borders,
figure/figcaption styling) since none of the editor's own inline styles are assumed to
cover every case. Page geometry: `fitz.paper_rect("a4")` mediabox with content inset
`(36, 46, -36, -46)` pt (left/top/right/bottom margins of 36/46/36/46pt) — this exact
geometry is what the editor's `.editor`/pagination CSS was sized to match (§
Pagination). Pages are written in a loop: `story.place(where)` lays out as much content
as fits the current page and reports whether more remains; `story.draw(device)` paints
it; repeat until `more` is falsy.

### Report id / naming

Report ids are generated once at creation (`slugify_report_name(name) +
"-" + uuid4().hex[:6]`) and never change even if the document is later renamed — so URLs
stay stable across renames. `check_report_id`/`DOC_ID_RE` (`^[A-Za-z0-9_-]+$`) guards
every path built from a report id against traversal, matching the same pattern used for
source-document ids elsewhere in the app.

## Data flow summary

```
type/format → editor DOM (contenteditable, source of truth in-browser)
            → MutationObserver → paginate() → visual .page-break spacers (view-only)
            → markDirty() → debounced saveReport()
            → getCleanEditorHtml() (strips .page-break) → POST /api/report/<id>
            → sanitize_report_html() → storage/reports/<id>.json ["html"]

Download as PDF → saveReport() (flush first) → GET /api/report/<id>/export
                → inline_report_images() → render_report_pdf() (fitz Story, A4)
                → application/pdf response
```
