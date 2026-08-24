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

Inserting a snippet (`.insert-snippet` button) inserts a plain, fully-editable
`<img class="doc-snippet">` — no wrapper element — at the caret via
`execCommand("insertHTML", ...)`, followed by an empty paragraph so the caret has
somewhere to continue typing below it. No caption is added, and the image is explicitly
sized to match the source page — see § Snippet figures: caption, size, alignment, resize,
which also explains why there's no wrapper.

### Snippet figures: caption, size, alignment, resize

**No caption.** Snippets used to carry a `<figcaption>Snippet — page N</figcaption>`
under the image. It's gone, for a concrete reason: a plain click-drag on an image inside
`contenteditable` only ever picks up the `<img>` itself, leaving a `<figcaption>` sibling
behind — that's what "the text doesn't follow the snippet when dragged" meant in
practice. Dropping the caption removes the thing that gets left behind, without needing
anything cleverer.

**No wrapper element — every property lives on the `<img>` itself.** Two earlier versions
wrapped the image in `<figure class="doc-figure">` and hung state off the figure instead:
first `contenteditable="false"` + `draggable="true"` on the figure (the same
nested-editable-island pattern `.page-break`/`.page-filler` use for pagination spacers) to
force it to move as one atomic unit — reverted because native HTML5 drag-and-drop over a
`contenteditable` region doesn't reliably remove the drag source, so dragging a snippet
could leave duplicate copies scattered through the document. Then a plain, fully-editable
`<figure>` with a grab cursor, hover-to-resize handles, and alignment all keyed off
`.doc-figure`/its nearest-block-ancestor `text-align` (set by the main toolbar's
`execCommand("justify*")`, same as centering a heading). That fixed the duplicate-copy bug
but broke as soon as a snippet was actually dragged: native contenteditable image drag
only ever relocates the bare `<img>` node, so the figure — and the grab cursor, hover
handles, and alignment hanging off it — got left behind at the old location. The dragged
image landed at its new spot wrapper-less, cursor-less, and left-aligned regardless of
what it was set to before.

So every per-snippet property now lives directly on the `<img>` that actually gets
dragged, not on anything around it:
- **Identity**: `class="doc-snippet"`, set at insert time and by `markAsSnippet()` when
  migrating an older document (below). The grab/grabbing cursor
  (`.editor img.doc-snippet`/`.dragging` in style.css), the hover-triggered resize handles
  (`img.doc-snippet` in the `mouseover`/`mouseout` listeners in document.js), and the
  `dragstart` cursor toggle all key off this class instead of a `.doc-figure` ancestor.
- **Alignment**: written straight onto the image's own inline `margin-left`/
  `margin-right` by `setSnippetAlign(img, align)` in document.js, not
  `execCommand("justify*")`. The main toolbar's Align left/center/right buttons detect
  when the current selection is exactly one `img.doc-snippet` (`getSelectedSnippetImage()`)
  and call `setSnippetAlign()` instead of `execCommand` in that case; for ordinary text
  selections the buttons behave exactly as before. `class` and `style` are both in the
  server's HTML-sanitizer attribute whitelist (`_clean_report_attrs` in app.py), which is
  also why this is inline style rather than a `data-align` attribute — the sanitizer has no
  data-* whitelist, so one would get silently stripped on save.
- **Size**: unchanged from before — `width`/`height` as inline `style` on the `<img>`
  (see below). This one already lived on the image itself, which is exactly why it was
  never affected by the drag-strands-the-wrapper bug the way cursor/handles/alignment were.

Because alignment no longer depends on `text-align` positioning an inline-level box inside
a block ancestor, the image can be `display: block` (see style.css) with plain
margin-based left/center/right — simpler, and it's what makes alignment survive a drag
that a purely CSS/execCommand-based scheme can't.

**Sized to match the source page, not the export PNG's raw pixels.** A snippet's rect is
captured as page-relative fractions (`x`/`y`/`w`/`h`, 0–1) and exported to PNG at a fixed
300dpi (`api_create_snippet` in app.py) — deliberately higher than the ~150dpi the source
page itself is rendered at for on-screen viewing, so the export stays crisp. Dropping the
PNG in at its native pixel size would render it roughly 3x too large on the page (the
300/96 dpi ratio) — not "the same size it was in the source" at all. `loadSnippets()`
fixes this at the source: alongside `GET .../snippets`, it fetches
`GET /api/doc/<id>/info` for each page's true dimensions in points, and computes the
insert size as `rect.w * page.width * (96/72)` (and the equivalent for height) — the same
pt→px convention used for page margins elsewhere in this file (`PT_TO_PX`). That's
independent of whatever DPI the PNG or the annotator's on-screen view happen to use; it's
the snippet's real physical size on the page, expressed in the same 96dpi convention this
editor's own A4 page emulation already uses. The computed `width`/`height` are written as
inline `style` directly on the inserted `<img>` (no `max-width` cap in `.editor
img.doc-snippet`, so this size is exactly what renders, not silently clamped to fit the
column). A snippet's on-screen size can also be changed directly by dragging one of the
four corner handles that appear on hover (`startFigureResize()` in document.js), which
likewise just writes new `width`/`height` onto the image's inline style.

**Cleaning up a document that passed through an earlier version.**
`cleanLegacyDocFigures()` runs once on `loadReport()` and unwraps any `.doc-figure`-wrapped
`<img>` an already-saved document still has: it removes legacy `<figcaption>`s,
`.resize-handle`/`.figure-align-toolbar` elements, and the figure's own
`contenteditable`/`draggable` attributes, then reads whatever alignment the figure carried
— inline `text-align`, or the older `align-center`/`align-right` classes — and translates
it into `setSnippetAlign()` on the unwrapped image before dropping the figure entirely. It
also strips a stray inline `max-width` the (long-removed) first resize attempt could have
left on the `<img>`. It deliberately does *not* touch the `<img>`'s own `width`/`height` —
those are the correct-size values `loadSnippets()` computed at insert time and need to
survive every reload untouched. `render_report_pdf`'s CSS (`REPORT_PDF_CSS` in app.py)
keeps a `figure { margin: 12pt 0; }`/`figure img { margin: 0; }` pair alongside its
`img { margin: 12pt 0; }` rule for the same reason, on the server side: a report exported
to PDF without ever being reopened in the editor may still have the old wrapped markup on
disk, and both forms need to render with the same spacing.

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
the real safety net here, not the sanitizer.) Snippets no longer need any special handling
here — an `img.doc-snippet` is plain editable content with no view-only additions of its
own; see § Snippet figures: caption, size, alignment, resize.

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

Insert snippet → loadSnippets() fetches page dims (/info) alongside /snippets
              → execCommand("insertHTML", ...) (no wrapper, no caption,
                <img class="doc-snippet" style="width/height"> sized to match the source
                page; alignment set later via setSnippetAlign(), not execCommand)
              → markDirty() → (same save path as below)

            → markDirty() → debounced saveReport()
            → getCleanEditorHtml() (strips .page-break/.page-filler)
              → POST /api/report/<id> (html + margins)
            → sanitize_report_html() / sanitize_margins() → storage/reports/<id>.json

Page setup → margins (in-memory) → applyMarginsToCss() (--m-* CSS vars) → schedulePaginate()
          → markDirty() → (same save path as above)

Download as PDF → saveReport() (flush first) → GET /api/report/<id>/export
                → inline_report_images() → render_report_pdf(html, margins) (fitz Story, A4)
                → application/pdf response
```
