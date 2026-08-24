# Document editor — design notes

The document editor lets a user compose a free-text report (a "document"/"report") by
typing and by pulling in image snippets captured from source PDFs/Word docs via the
annotator (see `ANNOTATOR_DESIGN.md`), then export the result as a PDF. It lives at
`/document` and is implemented across:

- `templates/document.html` — page shell/toolbar markup
- `static/document.js` — all client-side behavior
- `static/style.css` — the "Google-Docs-ish" visual styling (`.docbar`, `.doc-toolbar`,
  `.editor-container`, `.editor`, `.page-break`, `.page-filler`, `.margin-guides`, modals,
  landing grid)
- `app.py` — report CRUD endpoints, HTML sanitization, PDF export (`render_report_pdf`)

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/document` | GET | Renders `document.html`. With no `?report=` param, shows the landing/picker view. With `?report=<id>`, opens that report in the editor. Also accepts `?source=<id>&type=pdf\|docx` to pre-open the "new document" modal with a source pre-selected (used by the "Compile document »" link on the annotator page). |
| `/api/reports` | GET | List all saved reports (id, name, source doc/type, timestamps), newest-updated first. |
| `/api/reports` | POST | Create a new report. Body: `{name, source_doc, source_type}`. Server generates the id as `slugify(name)-<6 hex chars>`. |
| `/api/report/<id>` | GET | Fetch one report's full record (`name`, `html`, `source_doc`, `source_type`, `margins`, timestamps). |
| `/api/report/<id>` | POST | Save/update a report. Body: `{name, html, source_doc, source_type, margins}`. Server re-sanitizes `html` and re-validates `margins` before persisting (§ Sanitization, § Page margins). |
| `/api/report/<id>/export` | GET | Renders the saved `html` to a PDF (`render_report_pdf`) using the report's saved `margins`, and returns it as a download. |
| `/api/report/<id>/export.docx` | GET | Renders the saved `html` to a `.docx` (`render_report_docx`) and returns it as a download. |
| `/api/documents/upload` | POST | Multipart `file` field (`.pdf`/`.docx`/`.doc`). Validates the file actually opens (PyMuPDF for PDF, python-docx for Word), saves it into `documents/` under a slugified doc id, and returns `{id, type, filename}`. See § Uploading a source document. |

Report records are stored one-per-file as `storage/reports/<id>.json`:

```json
{
  "name": "Case summary — Jane Doe",
  "html": "<h1>...</h1><p>...</p>",
  "source_doc": "some-doc-id",
  "source_type": "pdf",
  "margins": {"left": 36, "right": 36, "header": 46, "footer": 46},
  "created_at": "2026-08-24T03:25:00.374293+00:00",
  "updated_at": "2026-08-24T03:25:00.374293+00:00"
}
```

`margins` (left/right/header/footer, in points) defaults to `REPORT_DEFAULT_MARGINS` — the
geometry `render_report_pdf` always used before margins became configurable — so reports
saved before this feature existed render unchanged. Set/edited via File → Page setup (§
Page margins).

`html` is always the *sanitized* HTML — never trusted raw client input — and is what both
the browser (on reload) and the PDF exporter render from. There is no separate rich
document model (no JSON-based AST, no ProseMirror/Slate-style schema); the DOM *is* the
model, serialized as HTML.

## Page structure (`document.html`)

`#docApp` carries all the routing/URL context as `data-*` attributes so `document.js`
never has to construct API URLs itself beyond simple concatenation, and Jinja's
`url_for` stays the single source of truth for route paths.

- `.docbar` — top bar: back link, File menu (New document / Open, then — in its own
  group — Upload source, then Page setup, then Download as PDF / Download as Word), the
  document title `<input>`, an explicit **Save** button + autosave status text, an
  `#uploadSourceStatus` status span, and the source picker (`<select id="sourceSelect">`
  + "Annotate source »" link).
- `.doc-toolbar` (`#formatToolbar`) — a Google-Docs-style formatting toolbar: font
  family/size selects, paragraph style select (Normal/H1/H2/H3), bold/italic/underline,
  text color + highlight color pickers, alignment buttons (inline SVG icons), bullet/
  numbered list buttons. Hidden entirely (`.toolbar-hidden`) when no report is open.
- `.viewer-layout` → `.editor-container` → `#editor-page-wrap` (`#editor` +
  `#marginGuides`) — `#editor` is the single `contenteditable="true"` surface the user
  types into (see § Pagination below for how this became multi-page); `#marginGuides` is
  a sibling overlay, not a child of `#editor`, that draws the dashed margin lines (§ Page
  margins). When no report is open, this area instead shows a `.doc-landing` grid of
  existing documents.
- `.sidebar` (`#reportSnippetList`) — snippets/annotations belonging to whatever source
  document is currently selected in the source picker; each has an "Insert" button that
  drops it into the editor at the caret.
- Three modals: **New document** (name + optional source doc), **Open** (a list of all
  existing reports), and **Page setup** (left/right/header/footer margin inputs, in
  points).

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

### Uploading a source document

`File → Upload source…` (`uploadSourceMenuBtn`) is a plain menu `<button>` that
programmatically clicks a hidden `<input type="file" id="uploadSourceInput">` sitting
right next to it in the dropdown — the standard pattern for a custom-styled file picker
trigger. It's deliberately in its own group (separated by `<hr>`s from New/Open above and
Page setup below) since it's a different kind of action: it mutates the shared
`documents/` library rather than the current report.

On the input's `change` event, `document.js` posts the file as `multipart/form-data` to
`POST /api/documents/upload`, which is the only way to add a source document short of
dropping a file into `documents/` on the server directly (§ Document model in
`ANNOTATOR_DESIGN.md`). On success it:

- appends a new `<option>` (value `"<id>|<type>"`) to **both** `sourceSelect` and the New
  Document modal's `newDocSource`, so the freshly uploaded doc is immediately choosable
  in either place without a page reload;
- if a report is currently open, selects it in `sourceSelect` and re-runs the same
  `change` handling as picking it manually (`updateAnnotateLink()`, `loadSnippets()`,
  `markDirty()`) — so uploading while editing a report both attaches the new source to
  that report and immediately shows its (empty) snippet list;
- writes a short status message into `#uploadSourceStatus` (next to the File menu, since
  the menu itself has already closed by the time the async upload/response completes).

Failure (wrong extension, corrupt/unparseable file, empty body) surfaces the server's
`{"error": "..."}` message in the same status span with an `.error` class, and never
touches `sourceSelect`/`newDocSource` or the report.

Doc-id collisions are resolved server-side, not client-side: `api_upload_document`
slugifies the uploaded filename's stem (reusing `slugify_report_name`) and, if a doc with
that id already exists (as `.pdf`, `.docx`, or `.doc`), appends `-<6 hex chars>` until it
finds a free id — so re-uploading a same-named file never silently overwrites an existing
source document.

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

Inserting a snippet (`.insert-snippet` button) wraps its `<img>` in
`<figure class="doc-figure" contenteditable="false" draggable="true"><img>...</figure>`
and inserts it at the caret via `execCommand("insertHTML", ...)`, followed by an empty
paragraph so the caret has somewhere to continue typing below the figure. No caption is
added — see § Snippet figures: caption, size, resize, alignment.

### Snippet figures: caption, size, resize, alignment

**No caption.** Snippets used to carry a `<figcaption>Snippet — page N</figcaption>`
under the image. It's gone: the browser's native contenteditable drag only ever picked
up the `<img>`, leaving the `<figcaption>` sibling behind, so the fix was to drop the
caption rather than try to keep the two in sync. `enhanceDocFigures()` (below) also
strips any `<figcaption>` still sitting inside a `.doc-figure` from a document saved
before this change, the next time that document loads.

**Atomic, draggable figure.** `.doc-figure` is `contenteditable="false"` inside the
`contenteditable="true"` editor — the same nested-editable-island pattern `.page-break`/
`.page-filler` use for pagination spacers, except here it's a real content node the user
interacts with. Making it non-editable is what actually fixes the drag bug: a
`contenteditable="false"` island is always dragged as a single atomic unit (there's no
longer a sub-part like a caption that could be left behind), and `draggable="true"` makes
that native drag-to-reposition explicit rather than relying on default per-browser
behavior.

**Original size, not shrunk to fit.** `.editor .doc-figure img` carries no `max-width`
constraint, so a freshly inserted snippet renders at the same pixel size it has in the
source document, even if that's wider than the editor column (it'll simply overflow
horizontally — `.editor-container`'s `overflow: auto` handles that). This is a deliberate
trade-off: previously `max-width: 100%` silently shrank oversized snippets, which is the
opposite of what "keep the original size" means. The resize handles below are how a user
brings an oversized snippet back down.

**Resize handles.** `enhanceDocFigures()` (called after every snippet insertion and after
`loadReport()` sets `editor.innerHTML`) adds four `.resize-handle` corner spans to each
`.doc-figure` that doesn't already have them, each wired to `startFigureResize()` via
`pointerdown`. Dragging a corner resizes the `<img>` with its aspect ratio locked
(computed once from the image's rendered box at drag-start) and a 24px floor
(`MIN_SIZE`), writing the new size to `img.style.width`/`height` (plus
`max-width: none`, overriding nothing since the img rule already has none, but keeping
the intent explicit). Handles are idempotent to add — gated on
`figure.querySelector(":scope > .resize-handle")` — so re-running `enhanceDocFigures()`
on an already-enhanced figure (e.g. a second insert elsewhere in the document) is a
no-op for it.

**Alignment.** A separate small `.figure-align-toolbar` (three buttons, reusing the exact
SVG icons from the main toolbar's Align left/center/right buttons) sits above each
figure, visible on hover. This is deliberately *not* wired through the main toolbar's
`execCommand("justifyLeft"/"justifyCenter"/"justifyRight", ...)` buttons — browsers
exclude non-editable content from `execCommand`'s reach, so those silently no-op on a
`contenteditable="false"` figure. Clicking a button instead toggles an `align-center`/
`align-right` class on the figure (default/no class = left), which CSS turns into
`margin-left`/`margin-right: auto` as appropriate. That only moves the figure if it has a
*definite* width — a bare block defaults to `width: auto` (100% of the editor column),
which leaves no spare width for `margin: auto` to distribute — so `syncFigureWidth()`
pins `figure.style.width` to `img.style.width` whenever a figure is (re-)enhanced, and
`startFigureResize()`'s drag handler keeps the two in sync live during a resize.

**Keeping the controls out of the saved document.** `.resize-handle` spans and the
`.figure-align-toolbar` are pure UI, recomputed by `enhanceDocFigures()` on every load —
they must never reach the server. `getCleanEditorHtml()` strips both, alongside
`.page-break`/`.page-filler`, and also removes the `contenteditable`/`draggable`
attributes it added to `.doc-figure` (harmless either way, since neither is in the
sanitizer's attribute whitelist and would be stripped server-side regardless — see
§ HTML sanitization). `enhanceDocFigures()` re-derives all of this from the live DOM on
next load rather than trusting anything persisted, which is also why a `<button>` inside
`.figure-align-toolbar` is safe even though `button` isn't an allowed tag: it's stripped
client-side before the HTML the server ever sees is generated.

### Pagination — simulated A4 pages

Added so the editor visually shows page breaks as you type, rather than being one
infinitely tall scrolling surface, and so those breaks roughly line up with where the
PDF export will actually paginate.

**Sizing.** `.editor` is A4 at 96dpi (794×1123px, `PAGE_HEIGHT_PX` in `document.js`).
Unlike the original version of this feature, top/right/bottom/left padding is **not**
fixed — it's driven by CSS custom properties (`--m-header`, `--m-right`, `--m-footer`,
`--m-left`) that `applyMarginsToCss()` sets from the report's `margins` (§ Page margins),
converted pt→px at `96/72`. The CSS fallback values if those vars are unset (61.33px/
48px) match the old hardcoded defaults, i.e. `render_report_pdf`'s original fitz inset of
`(36, 46, -36, -46)` pt. `.editor`'s `line-height: 1.5` matches `REPORT_PDF_CSS`'s
`line-height: 1.5` — any mismatch there directly skews how many editor-pages a paragraph
run takes versus how many PDF-pages it takes.

**Algorithm** (`paginate()` in `document.js`): treats every direct element child of
`#editor` (other than the pagination's own `.page-break`/`.page-filler` spacers) as an
opaque "block" (whatever tag it is — `p`, `h1`, `figure`, `ul`, …). Blocks are **never
split** — a deliberate simplicity/robustness trade-off (see the known limitation below);
a block that doesn't fit on the current page moves to the next page whole. On each pass:

1. Remove any existing `.page-break`/`.page-filler` elements (so measurements are against
   real content only).
2. Walk the remaining element children in order, tracking `pageContentTop` — the *actual
   measured* top of the current simulated page's content region (`editor`'s own padding
   edge for page 1; `previous break's bottom + header margin` for later pages) — and
   `prevBottom`, the bottom of the last block placed on the page.
3. For each block, if `rect.bottom - pageContentTop > pageContentHeight` (the page's
   content-region capacity, `PAGE_HEIGHT_PX - header - footer`) *and* this isn't the
   page's first block (`prevBottom !== null`, so an oversized single block doesn't
   recurse into an infinite string of empty pages), close out the page: insert a
   `.page-filler` sized to the leftover content space *plus* that page's footer margin
   (its own white space, not the PDF's), then a fixed-height `.page-break` gap, then
   another `.page-filler` sized to the new page's header margin, before advancing to this
   block.
4. After the loop, if there was any content, insert one more `.page-filler` on the final
   page sized to its leftover content space only — its footer comes for free from
   `.editor`'s own bottom padding, same as page 1's header comes from its top padding.

Blocks are measured via `getBoundingClientRect()` diffs (`rect.bottom - pageContentTop`),
**not** by summing each block's own `margin-top`/`margin-bottom` — adjacent block margins
*collapse* in the browser's real layout, so summing them independently overcounts true
rendered height and made both the overflow check and the filler sizing badly wrong (found
and fixed 2026-08-24, see chat history).

**Why spacers are safe to insert/remove around live typing**: `.page-break` and
`.page-filler` only ever go *between* top-level blocks, never inside the block containing
the caret, so inserting or removing them never invalidates the browser's current
`Range`/selection — no manual save/restore is needed the way the toolbar needs it. This
is the key property that makes the whole approach tractable without a virtual-DOM
diffing layer.

**Trigger**: a `MutationObserver` on `#editor` (`childList + subtree + characterData`)
catches every source of change — typing, toolbar commands, undo/redo, snippet insertion,
initial `innerHTML` load — and schedules `paginate()` via `requestAnimationFrame`
(coalescing bursts of mutations, e.g. a fast typing run, into one measurement pass per
frame). `paginate()` disconnects the observer for the duration of its own DOM writes and
reconnects afterward, so its own spacer insert/remove calls don't re-trigger themselves.
Two extra triggers: an `img` `load` listener (image height is unknown/zero until it
loads, so a block containing one is re-measured once it does) and `window.resize` (the
editor can shrink below 794px on narrow viewports via `max-width:100%`, which reflows
line-wrapping and thus block heights). Changing margins via Page setup doesn't itself
mutate `#editor`'s DOM, so `applyMarginsToCss()` calls `schedulePaginate()` explicitly.

**Keeping spacers out of the saved document**: `.page-break`/`.page-filler` elements are
a pure view artifact recomputed from scratch every pass — they must never reach the
server. Both places that used to read `editor.innerHTML` directly (the save payload and
the `beforeunload` sendBeacon) now call `getCleanEditorHtml()` instead, which clones
`#editor`, strips every `.page-break`/`.page-filler` from the clone, and serializes that.
(Even if either stray element did leak into a save, the server's HTML sanitizer would
strip its `contenteditable` attribute — not in the attribute whitelist — but the `div`
itself would survive, since `div` and `class` are both allowed; client-side stripping is
the real safety net here, not the sanitizer.) `getCleanEditorHtml()` does the same job for
snippet figures' own view-only additions (`.resize-handle`, `.figure-align-toolbar`,
`contenteditable`/`draggable`) — see § Snippet figures: caption, size, resize, alignment.

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
"line-level" and "A4" over "Letter" as the shared page size standard). A related, smaller
approximation: `.page-filler` sizing measures up to `prevBottom` (a block's own border-box
bottom, which excludes that block's `margin-bottom`), so a page can end up a handful of
pixels taller than exactly A4 — always *at least* A4-tall, per an explicit later
requirement, never shorter.

**Other known nuance**: pressing Backspace/Delete right at a page boundary can "consume"
one keypress on a non-editable `.page-break`/`.page-filler` div itself before it goes on
to merge the adjacent paragraphs, since a `contenteditable="false"` island is deleted as
an atomic unit. Because the whole spacer set is discarded and recomputed on every pass
anyway, this self-corrects immediately and never leaves stale/duplicate spacers behind.

### Page margins & margin guides

**Configuring margins.** File → Page setup opens a modal (`#pageSetupModal`) with four
number inputs (left/right/header/footer, in points, 0–200 each — `clampMargin()`).
Applying it updates the in-memory `margins` object, calls `applyMarginsToCss()` (§
Pagination), and `markDirty()` so it's persisted like any other edit. `loadReport()`
normalizes whatever the server returns (`normalizeMargins()`) so a pre-margins report
(no `margins` key) falls back to the same defaults `REPORT_DEFAULT_MARGINS` uses
server-side.

**Two different kinds of "space" that look similar but aren't:**
- **Header/footer margins** are a *page's own white space* — rendered as `.page-filler`
  elements inside `#editor` (§ Pagination), sized in px from the pt margin values. Real
  editable-adjacent blank space: `.page-filler` is `contentEditable="false"` but
  `pointer-events: none`, so a click there falls through to `#editor` and lands the caret
  at the nearest real text, same as clicking below the last line of a normal document.
- **The gap between pages** (`.page-break`) is a fixed 1cm (`PAGE_GAP_PX = 96/2.54`),
  **independent of the margin values** — purely a visual separator between simulated
  sheets, the same idea as the constant gap Google Docs/Word show regardless of your
  margins. It used to be sized to `header + footer` combined, which conflated the two
  concepts and made the gap balloon with larger margins (fixed 2026-08-24). Unlike
  `.page-filler`, `.page-break` keeps `pointer-events: auto` and `cursor: default`
  deliberately — it needs to actually capture hover/clicks (rather than let them fall
  through to editable text underneath, which made the gap look like typable space) — and
  `document.js` `preventDefault()`s its `mousedown` so a click there is fully inert.

**Margin guides** (`#marginGuides`, in `document.html`): a dashed-outline overlay showing
each simulated page's printable area, one `.page-rect` per page. It's a *sibling* of
`#editor` (inside `#editorPageWrap`), not a child — so it can never leak into saved HTML
and doesn't interfere with `#editor`'s `:empty` placeholder (an earlier version put guides
inside `#editor`, which broke the `:empty` CSS selector the placeholder text depends on).
`renderMarginGuides()` (called at the end of every `paginate()` pass) measures the actual
`.page-break` elements' positions rather than assuming uniform `PAGE_HEIGHT_PX` slices —
an earlier version did the latter and, because pages don't literally pad to exactly
`PAGE_HEIGHT_PX` before a break (see the filler-sizing approximation above), the guide
rectangle occasionally overshot the real page and bled dashed lines into the gray gap
(fixed 2026-08-24).

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
`(left, header, -right, -footer)` pt, taken from the report's own `margins` (§ Page
margins) via `sanitize_margins()` — defaulting to `REPORT_DEFAULT_MARGINS`
(36/36/46/46pt) when a report predates the feature or a value is out of range
(`REPORT_MARGIN_MIN`/`MAX`, 0–200pt — chosen so left+right and header+footer both stay
well under A4's 595×842pt, keeping the content rect non-degenerate). This geometry is
what the editor's `.editor`/pagination CSS is sized to match (§ Pagination) via the same
`margins` value. Pages are written in a loop: `story.place(where)` lays out as much
content as fits the current page and reports whether more remains; `story.draw(device)`
paints it; repeat until `more` is falsy.

### Report id / naming

Report ids are generated once at creation (`slugify_report_name(name) +
"-" + uuid4().hex[:6]`) and never change even if the document is later renamed — so URLs
stay stable across renames. `check_report_id`/`DOC_ID_RE` (`^[A-Za-z0-9_-]+$`) guards
every path built from a report id against traversal, matching the same pattern used for
source-document ids elsewhere in the app.

## Data flow summary

```
type/format → editor DOM (contenteditable, source of truth in-browser)
            → MutationObserver → paginate() → visual .page-break/.page-filler spacers
              (view-only) + renderMarginGuides() (#marginGuides overlay, view-only)

Insert snippet → execCommand("insertHTML", ...) (.doc-figure, no caption)
              → enhanceDocFigures() → .resize-handle / .figure-align-toolbar (view-only)
              → markDirty() → (same save path as below)

            → markDirty() → debounced saveReport()
            → getCleanEditorHtml() (strips .page-break/.page-filler/.resize-handle/
              .figure-align-toolbar, and .doc-figure's contenteditable/draggable)
              → POST /api/report/<id> (html + margins)
            → sanitize_report_html() / sanitize_margins() → storage/reports/<id>.json

Page setup → margins (in-memory) → applyMarginsToCss() (--m-* CSS vars) → schedulePaginate()
          → markDirty() → (same save path as above)

Download as PDF → saveReport() (flush first) → GET /api/report/<id>/export
                → inline_report_images() → render_report_pdf(html, margins) (fitz Story, A4)
                → application/pdf response
```
