# Annotator (page viewer) — design notes

The annotator is a per-page document viewer for source PDFs/Word docs, used to: mark up
a page with rectangles or freehand ink, extract rectangular "snippets" (cropped raster
images of a region, optionally with the markup baked in) for reuse elsewhere, and
download an annotated copy of the whole source PDF. Snippets extracted here are what the
document editor's sidebar (`EDITOR_DESIGN.md`) lets a user insert into a report. It lives
at `/annotations` and is implemented across:

- `templates/annotations.html` — page shell/toolbar markup
- `static/viewer.js` — all client-side behavior
- `static/style.css` — shared with the rest of the app (`.topbar`, `.toolbar`,
  `.viewer-layout`, `.page-wrap`, `.context-menu`, `.sidebar`/`.snippet-*`)
- `app.py` — document resolution, page rasterization, annotation storage, snippet
  extraction, annotated-PDF download
- `converters.py` — headless-LibreOffice `.docx`/`.doc` → PDF conversion

## Document model: everything becomes a PDF page raster

There's no native Word rendering path and no client-side PDF.js/vector rendering.
Instead, every source document — PDF or Word — is normalized to a PDF (via
`resolve_pdf_path`) and then every page the browser ever sees is a **server-rendered PNG
raster** of that PDF page (`GET /api/doc/<id>/render/<page>`, via PyMuPDF's
`get_pixmap`). This is a deliberate simplicity trade-off: it means the client only ever
deals with `<img>` + `<canvas>`, with zero PDF parsing/text-layer/font logic in the
browser, and PDFs and converted Word docs are visually indistinguishable to the rest of
the app once resolved.

- **PDF source** (`documents/<id>.pdf`): used directly.
- **Word source** (`documents/<id>.docx` or `.doc`): converted once via `convert_to_pdf`
  (`converters.py`, shells out to `soffice --headless --convert-to pdf`) into
  `storage/cache/<id>.pdf`. Re-converted automatically if the source file's mtime is
  newer than the cached PDF's, so editing the source Word doc and reloading picks up the
  change without any manual cache-busting. If LibreOffice (`soffice`/`libreoffice`) isn't
  on `PATH`, this raises a `ConversionError` with instructions (install LibreOffice, or
  drop a same-named `.pdf` into `documents/` yourself).
- `doc_id` is validated everywhere via `DOC_ID_RE` (`^[A-Za-z0-9_-]+$`) before touching
  the filesystem — the main defense against path traversal through a user-controllable
  id.
- `type` (`pdf`/`docx`/`doc`/`word`, normalized to `"pdf"`/`"docx"` by `normalize_type`)
  is threaded through nearly every route/URL because the same `doc_id` could
  *theoretically* exist as both a PDF and a Word source, and because storage paths keep
  the two universes separate (`<id>__pdf` vs `<id>__docx`).

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/annotations?doc=&type=&page=` | GET | Renders `annotations.html` for one page. Clamps `page` into `[1, page_count]`. |
| `/api/doc/<id>/info` | GET | Page count + each page's PDF-point dimensions. |
| `/api/doc/<id>/render/<page>?dpi=` | GET | Rasterizes that page to PNG (`dpi` clamped 50–600, default 150). No caching header (`Cache-Control: no-store`) since annotations aren't baked into this raster — see § Annotation rendering. |
| `/api/doc/<id>/annotations/<page>` | GET/POST | Load/save the raw annotation list for one page. |
| `/api/doc/<id>/snippet/<page>` | POST | Crop a rectangular region (fractional coords) out of the page at high DPI, optionally with the current annotation markup burned in, save it as a PNG + metadata entry. |
| `/api/doc/<id>/snippets?page=` | GET | List saved snippets (optionally filtered to one page). |
| `/api/doc/<id>/snippet/<snippet_id>` | DELETE | Delete one snippet (file + metadata entry). |
| `/api/doc/<id>/download` | GET | Whole source PDF, every page's saved annotations burned in, as an attachment. |
| `/media/snippets/<id>/<filename>` | GET | Serves a saved snippet PNG. |

## Page structure (`annotations.html`)

`#app` carries the same `data-*` URL-context pattern as the editor. Layout:

- `.topbar` — doc id/type, Prev/Next page links (plain `<a href>` navigation — see §
  "No SPA routing" below), a page-number `<input>` that jumps via Enter, and a "Compile
  document »" link to `/document?source=<id>&type=<type>` (opens the editor's New
  Document modal pre-filled with this source).
- `.toolbar` — mode buttons (Rectangle / Freehand / Snippet), a color `<input
  type=color>`, Undo, Clear (two-step confirm), Save annotations, Download annotated
  PDF, plus a live mode hint and a transient status message.
- `.viewer-layout` → `.viewer-container` → `.page-wrap` containing a stacked
  `<img id="pageImage">` (the raster) and `<canvas id="overlay">` (all markup, drawn in
  client JS) at identical CSS size, positioned via `position: absolute`.
- `.sidebar` — snippets belonging to *this page only* (`?page=` filter), each with an
  open-in-new-tab thumbnail link, a Download link, and a two-step-confirm Delete.

## `viewer.js` architecture

### Coordinate system: fractional (0–1), not pixels

Every annotation and every snippet rectangle is stored as fractions of the page's
width/height (`x, y, w, h ∈ [0,1]`), not absolute pixels. This is what lets the same
annotation data drive three different pixel scales without any conversion math living in
the data itself: the on-screen `<canvas>` (sized to fit the container, `canvas.width` =
`img.naturalWidth` at 150dpi), a snippet crop rendered at a much higher DPI (300 default,
up to 900), and the full-resolution annotated-PDF download (drawn directly in PDF point
space via `fitz`). `clientToFrac()` converts a raw mouse/pointer client position into
this fractional space using the canvas's *rendered* bounding rect, so it stays correct
regardless of zoom/responsive scaling.

### Layout / responsiveness

`layout()` sizes `.page-wrap`/`canvas` (CSS size only — `canvas.width`/`height`, the
actual bitmap resolution, are fixed to `img.naturalWidth/Height` once at image load) to
fill the available container width minus a 40px margin, preserving the image's aspect
ratio. Re-run via `requestAnimationFrame` (not a fixed debounce) on `window.resize` and
via `ResizeObserver` on the container, so it tracks a live window-drag every frame
instead of snapping only once the resize settles.

### Modes

`mode` is one of `null` (select/idle), `"rect"`, `"freehand"`, `"snippet"`. Clicking an
active mode button toggles it back to `null`. Switching modes always cancels any
in-progress shape (`liveRect`/`currentStroke` cleared) so a half-drawn shape can't get
orphaned mid-mode-switch.

- **`null` (select)**: click hit-tests existing annotations (see below) and selects the
  topmost hit; the cursor becomes a pointer over a hit annotation.
- **`rect`**: drag defines a rectangle in the current color picker color. On release, the
  finished rectangle is pushed into `annotations` *and* immediately triggers an
  auto-generated snippet crop of that same region (padded 5px) — see § Auto-snippets.
- **`freehand`**: drag accumulates points (throttled — a new point is only appended once
  the pointer has moved ≥0.0015 of the page's fractional size, to avoid pathological
  point counts from a slow, jittery drag), then smoothed on release (`smoothPoints`, a
  simple centered moving-average over a window of 2) before being pushed to
  `annotations` and also auto-snippeted, using its fractional bounding box.
- **`snippet`**: drag defines a rectangle exactly like `rect` mode, but it is *not* added
  to `annotations` — it only extracts a crop. Rendered live in a fixed blue (`#2266dd`)
  rather than the color picker's color, visually distinguishing "this is a one-off crop"
  from "this is a persistent markup annotation".

### Drawing & hit-testing

All rendering is immediate-mode canvas (`redraw()` clears and redraws everything from
the `annotations` array plus whatever's mid-draw — no retained shape objects, no
incremental patching). Freehand strokes are rendered as a smoothed path using
`quadraticCurveTo` between successive midpoints (classic canvas line-smoothing idiom).

Hit-testing (`hitTestAnnotations`) is edge-only, not fill-based — a click must land near
a rectangle's border or near a freehand stroke's polyline, within a tolerance of
`max(10, canvas.width * 0.008)` px, computed via point-to-segment distance
(`distToSegment`). This means clicking *inside* a big rectangle without touching its
border does not select it — a deliberate (if perhaps surprising) choice that keeps large
annotations from blocking selection of things layered underneath/inside them.

Selection is visualized as a padded dashed blue outline (`drawSelectionHighlight`)
around the selected annotation's (or freehand stroke's bounding box's) edges.

### Deletion

Three paths, all funneling into `deleteSelectedAnnotation()`:
- **Delete/Backspace key** — only acts if something is selected and focus isn't inside
  a text input/textarea/contenteditable (so it doesn't hijack normal text editing
  elsewhere on the page).
- **Right-click context menu** — right-click always hit-tests and selects under the
  cursor first (independent of the current mode — right-click bypasses draw modes
  entirely), then shows a small custom `.context-menu` positioned at the click.
- Deleting an annotation also deletes its linked auto-snippet, if any (§ Auto-snippets).

**Undo** just pops the most recent annotation (and its linked snippet, if any) — a flat
stack, not true multi-action undo (no redo, no undo of a delete). **Clear** removes every
annotation on the page (and all their linked snippets) behind a two-step "Clear" →
"Confirm clear?" button (3s auto-revert) to guard against an accidental full wipe.

### Save flow & unsaved-changes guard

`dirty` is set on every mutation (add/delete/undo/clear). The **Save annotations**
button POSTs the entire current `annotations` array for the page to
`/api/doc/<id>/annotations/<page>`, which the server stores wholesale (§ Storage) — no
incremental diffing. `beforeunload` shows the browser's native "leave site?" prompt while
`dirty` is true (this one *can* use the blocking dialog, unlike the document editor's
`sendBeacon` approach, because annotation saves are cheap/instant and there's no
autosave to race against — see `EDITOR_DESIGN.md` for why the editor went the beacon
route instead).

### Auto-snippets (annotation ↔ snippet linkage)

Every time a `rect` or `freehand` annotation is finished, the code doesn't just save the
markup — it also immediately calls `createSnippet()` for that same region (padded 5px
via `boundsToPaddedRect`), so a raster crop of "the area you just annotated" shows up in
the sidebar automatically, without a separate manual snippet-extraction step. The link
is tracked client-side only, via `annotationSnippetIds` — an array kept strictly parallel
to `annotations` (same index ↔ same annotation), storing the resulting snippet's id (or
`null` if the snippet call failed, or hadn't resolved yet). This is why delete/undo/clear
all also delete the linked snippet: from the server's point of view a rect annotation and
its auto-crop are two unrelated records (one in `annotations/<id>__<type>.json`, one in
`snippets/<id>__<type>.json` + a PNG file) with no persisted foreign key between them —
the *only* place that link is known is this parallel array in the live page's JS state.
Reloading the page loses the linkage (loaded annotations all get `null` snippet ids in
`loadAnnotations()`), so deleting an old annotation after a reload will **not** clean up
its earlier auto-snippet; that snippet just becomes an ordinary, independently-managed
sidebar entry from then on.

Plain "Snippet" mode crops do **not** get added to `annotations` at all, so they have no
linkage to track or clean up — deleting one only ever removes the snippet itself.

### No SPA routing

Prev/Next/page-jump are plain full-page navigations (`<a href="?doc=...&page=...">`, or
`window.location.href = ...` from the page-number input's Enter handler) — not
client-side page swapping. Simpler, and it means `dirty`/unsaved-annotation state is
scoped to exactly one page's lifetime by construction (no risk of stale in-memory state
from a previously-viewed page bleeding into the next).

## Server-side

### Annotation validation (`sanitize_annotations`)

Every annotation list — whether coming from `POST /api/doc/<id>/annotations/<page>` or
embedded in a `POST /api/doc/<id>/snippet/<page>` body — is re-validated server-side
before ever being drawn or persisted, via `sanitize_annotations`:
- Hard caps: at most 500 annotations, at most 2000 points per freehand stroke (crude
  but effective bound against a pathological/malicious payload).
- Every coordinate is clamped into `[0, 1]` (`_clamp01`), never trusted as-is.
- `color` must match `HEX_COLOR_RE` (`#rrggbb`) or falls back to
  `DEFAULT_ANNOTATION_COLOR` (`#e02424`) — no way to inject arbitrary CSS/SVG paint
  values.
- A `rect` with non-numeric or zero/negative `w`/`h` is dropped; a `freehand` with fewer
  than 2 valid points after cleaning is dropped.
- Anything that isn't a `dict` with `kind` = `"rect"` or `"freehand"` is silently
  skipped, not errored on — malformed individual entries don't fail the whole batch.

### Annotation rendering — never mutates the source file

`draw_annotations_on_page` burns sanitized annotations into a `fitz.Page`'s content
stream **entirely in memory** using `page.new_shape()` / `draw_rect` /
`draw_polyline` / `shape.commit()` — this never touches the on-disk source PDF. It's
called in exactly two places, both of which open their own `fitz.open(...)` handle and
either discard it or write to a fresh in-memory buffer:
- `/api/doc/<id>/snippet/<page>` — draws the *currently-being-saved* annotation batch
  (not necessarily what's persisted yet) onto an open page, then `get_pixmap(...,
  clip=...)` crops just the requested rectangle at the requested DPI. This is why a
  snippet extracted right when you draw a rectangle shows that rectangle's outline baked
  into the image, while a **plain "Snippet" mode** crop — where `annotations` passed
  along still contains whatever was already on the page — shows any *existing* markup
  but not a rectangle for the crop region itself (there is no annotation added for a
  pure snippet-mode drag).
- `/api/doc/<id>/download` — walks every page key in the stored annotations JSON,
  re-validates each page's list through `sanitize_annotations` again (defense in depth —
  never trust that what's on disk still satisfies today's validation rules), draws it
  onto that page, and serializes the *whole modified-in-memory* document via
  `d.tobytes(deflate=True)`. The file on disk under `documents/` is untouched; only the
  bytes returned to the client include the markup.
- `/api/doc/<id>/render/<page>` (the plain page raster used for on-screen viewing)
  deliberately does **not** call `draw_annotations_on_page` — annotation markup is drawn
  purely client-side on the `<canvas>` overlay for the live-editing view, and only
  server-baked into pixels for snippet crops and the final download.

### Storage layout

```
storage/
  cache/<id>.pdf                       — LibreOffice-converted Word source, keyed by mtime
  annotations/<id>__<type>.json        — {"<page>": [ {kind:"rect",color,x,y,w,h} | {kind:"freehand",color,points:[[x,y],...]} ]}
  snippets/<id>__<type>.json           — [ {id, page, filename, rect:{x,y,w,h}, annotated, created_at} ]
  snippets/<id>__<type>/<filename>.png — the actual cropped raster, filename = p<page>_<12-hex-id>.png
  reports/<report_id>.json             — see EDITOR_DESIGN.md
```

`annotations/<id>__<type>.json` is one JSON object per (doc, type), keyed by
page-number-as-string, holding that page's full annotation list — saving a page
overwrites just its own key (`data[str(page)] = anns`) via read-modify-write, not a
per-page file. `save_json` writes to a random `.tmp` sibling and `Path.replace()`s it
into place, so a crash mid-write can't leave a torn/partial JSON file behind.

Snippet metadata's `annotated` flag records whether the snippet was created *with* a
non-empty annotation list burned in (`bool(annotations)` at creation time) — this is
purely descriptive after the fact (e.g. so the editor sidebar could label it
"Annotation" vs "Snippet" — see `EDITOR_DESIGN.md`); it doesn't affect how the snippet
is served or stored otherwise.

## Data flow summary

```
GET /annotations?doc=&type=&page=  → resolve_pdf_path() (convert Word→PDF if needed, cached by mtime)
                                    → annotations.html + viewer.js

viewer.js on load:
  img.src = /api/doc/<id>/render/<page>?dpi=150   (raw page raster, no markup baked in)
  GET /api/doc/<id>/annotations/<page>            → draw onto <canvas> overlay client-side
  GET /api/doc/<id>/snippets?page=<page>          → sidebar thumbnails

draw rect/freehand → annotations[] (client) → auto POST /api/doc/<id>/snippet/<page>
                                                 (sanitize_annotations → draw_annotations_on_page (in-memory)
                                                  → get_pixmap(clip=..., dpi=300) → PNG saved to disk + metadata)

Save annotations  → POST /api/doc/<id>/annotations/<page> → sanitize_annotations → annotations/<id>__<type>.json

Download annotated PDF → GET /api/doc/<id>/download
                        → for each page: sanitize_annotations(stored) → draw_annotations_on_page (in-memory)
                        → d.tobytes() → application/pdf response (source file on disk never modified)
```
