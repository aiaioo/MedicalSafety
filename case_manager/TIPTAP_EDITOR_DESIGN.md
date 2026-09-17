# Document editor — Tiptap architecture

Supersedes `EDITOR_DESIGN.md` and `MULTILEVEL_SECTION_NUMBERING.md`, which describe the
original hand-rolled `contenteditable` + `document.execCommand()` editor. That editor had no
real document model — the DOM *was* the model, serialized as sanitized HTML — so every
list-editing edge case (Enter/Tab around list items, multilevel section numbers, restart/
continue) had to be hand-built against raw DOM mutations, one bug at a time. This is a
rewrite onto [Tiptap](https://tiptap.dev)/ProseMirror, which brings a real schema (nodes,
attributes, a documented transform API) so those edge cases are either solved by the
library outright or become small, structurally-obvious extensions instead of DOM archaeology.

The two superseded docs are still worth reading for *why* certain UX behaviors are required
(e.g. "Enter on an empty list item exits the list outright, regardless of depth" — still
true here, just implemented differently) — they're a record of product decisions, not just
implementation detail. This doc covers how those requirements are met now.

## File layout

- `templates/document.html` — page shell/toolbar markup (mostly unchanged from before —
  the toolbar's element IDs are still what the JS below wires up to)
- `static/src/editor.js` — the editor "app shell": file menu, modals, page setup, save/
  autosave/export, toolbar wiring, snippet sidebar. Entry point for the build.
- `static/src/listNumbering.js` — the multilevel numbering engine (§ below)
- `static/src/pagination.js` — simulated A4 pagination (§ below)
- `static/src/resizableImage.js` — resizable/alignable snippet images (§ below)
- `static/dist/editor.bundle.js` — the esbuild output actually loaded by
  `document.html`; **generated, not source** (gitignored — see README's Setup section for
  the `npm install && npm run build` step)
- `static/style.css` — visual styling, shared with the rest of the app
- `app.py` — report CRUD endpoints, JSON sanitization, PDF/DOCX export

## Data model: the document is Tiptap/ProseMirror JSON, not HTML

A report's content (`doc` field in `storage/reports/<id>.json`) is a ProseMirror document —
a JSON tree of typed nodes (`paragraph`, `heading`, `orderedList`, `listItem`, `image`, ...)
with typed attributes, and marks (`bold`, `textStyle`, `link`, ...) on text nodes — rather
than a string of HTML. This is a real schema Tiptap enforces client-side (unknown node/mark
types or malformed content simply can't be constructed through the editor's own commands),
and `sanitize_report_doc` in `app.py` re-validates it server-side against the same shape
before every save: a fixed whitelist of node/mark types, and per-attribute checks (a hex
color regex, `REPORT_SAFE_URL_RE` for image/link URLs, enum checks for alignment/wrap/type
values, numeric ranges for indent/level counts). This is structurally safer than the old
HTML-tag sanitizer: there's no HTML-parsing edge case to get wrong, because there's no HTML
to parse — just a tree of dicts with known shapes.

`GET /api/report/<id>` still returns whatever the record happens to have: `doc` for
anything saved by this editor, or a legacy `html` field for a report saved before this
migration. `static/src/editor.js`'s `loadReport()` prefers `doc` and falls back to handing
`html` straight to `editor.commands.setContent()`, which parses it through Tiptap's own
HTML→schema importer — a one-time, best-effort migration. It recovers ordinary content and
plain numbered lists, but **not** a custom numbering template or a restart/continue override
on an old list (those were encoded as CSS classes/`style` the new schema doesn't parse back
in) — such a list comes back as a plain sequential list, and the user would need to reapply
a preset. `POST /api/report/<id>/export(.docx)` refuses outright (with a message asking the
user to open-and-save first) if the record still has no `doc` at all, rather than silently
exporting a blank or wrong document.

## Multilevel numbering (`listNumbering.js`)

Where the old editor encoded a numbering template as CSS classes on the `<ol>`
(`cascade`, `lvl1-decimal-none`, ...) and a restart/continue override as
`style="counter-reset: cN n"`, this is now genuine schema:

- `numCascade` (bool) and `numLevels` (`[{type, wrap}, ...]`, up to 6 entries) are real
  attributes on a custom `OrderedList` extension (`BaseOrderedList.extend(...)`) — set on
  the **outermost** `<orderedList>` node of a nesting chain only, exactly like the old
  classes only ever lived on the top-level `<ol>`. A nested list created by Tab/
  `sinkListItem` carries neither attribute; rendering (see below) walks up to find the
  nearest ancestor that has them.
- Restart/set-value/continue reuse ProseMirror's *native* `start` attribute on
  `orderedList`. Restarting the list's own first item is just updating `start` in place.
  Restarting a *later* item splits the list into two sibling `orderedList` nodes at that
  point — a real document transform (`tr.replaceWith`) — with the second one carrying
  `start` and a copy of the first's `numCascade`/`numLevels` (so the split-off tail keeps
  looking like part of the same templated list, not a fresh untemplated one).

Marker text ("1.", "1(2)(3)", ...) is **never stored** — same principle as the old CSS-
counter approach (always computed from state, never baked into content), but computed
directly from the ProseMirror document by a plugin (`ListMarkers`) instead of relying on the
browser's CSS counter/`::before` engine. On every transaction, it walks the doc (mirroring
`_ListNumberingState` in `app.py` — the two must stay in sync), and renders each numbered
item's marker as a **widget decoration**: a `<span class="list-marker">` inserted at the
start of the item's first paragraph. Decorations are display-only and never serialize into
`getJSON()`/`getHTML()`, so this can't leak into saved content the way the old
`#dynamicListStyles` `<style>` tag (deliberately kept outside `#editor`) had to be careful
not to.

Enter-on-an-empty-list-item (`exitEmptyListItemOnEnter`) and Tab/Shift+Tab
(`sinkListItem`/`liftListItem`, falling back to a `BlockIndent` margin-nudge outside a list)
are both intercepted in `editor.js`'s own `keydown` handler ahead of Tiptap's defaults, for
the same "exit outright regardless of depth" / "never touch execCommand's indent" reasons
the old doc explains — just implemented as ProseMirror transforms instead of raw DOM splices.

`app.py`'s exporters mirror this algorithm in Python (`_ListNumberingState`,
`_json_blocks_to_html` for PDF, `_docx_render_blocks` for DOCX) reading the same
`numCascade`/`numLevels`/`start` attributes directly off the JSON — no class-string parsing
needed, unlike the old `_parse_list_template` regex.

## Pagination (`pagination.js`)

Shows where the document will actually break across printed A4 pages while editing. The old
implementation mutated `#editor`'s DOM directly (a `MutationObserver` inserting `.page-break`/
`.page-filler` sibling `<div>`s between top-level blocks) — which would fight ProseMirror's
own DOM reconciliation here. Instead, breaks are rendered as widget decorations, computed by
a ProseMirror plugin's `view()` component:

1. On every view update, `computeBreaks()` walks the document's top-level nodes via
   `view.nodeDOM(pos)` (ProseMirror's own pos↔DOM mapping, not raw `element.children`),
   measuring each one's rendered height and deciding where a page boundary falls — a block
   that doesn't fit moves to the next page whole, never split mid-paragraph.
2. If the computed breaks differ from what's currently rendered, it dispatches a transaction
   carrying the new breaks as plugin state, which the `decorations()` prop turns into
   `.page-break`/`.page-filler` widgets.
3. That dispatch triggers another `view()` update, so `computeBreaks()` runs again — now
   measuring a layout that includes the *previous* pass's inserted gaps. This settles within
   a couple of animation frames rather than needing to get page-break math exactly right
   against not-yet-rendered content in one pass (the old DOM-mutation version could do this
   synchronously in one pass; decorations can't insert-and-immediately-remeasure, so this
   trades that for a self-correcting loop instead).

A window resize, an async image load, or a margin-only change (Page Setup) all change layout
without producing a ProseMirror transaction on their own, so each dispatches a no-op
transaction (`repaginate()`) purely to nudge the plugin's `view.update()` hook.

Margin guides (`renderMarginGuides()` in `editor.js`, an overlay `<div>` sibling of `#editor`
so it can never leak into saved content) are unchanged in spirit from before — measured from
the actual rendered `.page-break` elements — just re-triggered via the plugin's `onPaginate`
callback instead of a `MutationObserver`.

## Resizable/alignable images (`resizableImage.js`)

Extends Tiptap's stock `Image` node with `width`/`height`/`align` attributes — persisted,
unlike the old editor's approach of writing straight to the `<img>`'s own inline style with
nothing backing it up in the saved document (a resize survived a session only because the
whole DOM was serialized as the saved HTML; here the DOM is rendered *from* attrs, so the
attrs are the source of truth).

Resize handles are a ProseMirror **NodeView** — a `<span class="img-wrap">` wrapping the
`<img>` plus four corner handles, shown on hover/selection. The old editor kept handles in a
separate overlay layer positioned by hand from the hovered element's live
`getBoundingClientRect()), specifically because native `contenteditable` image drag detaches
only the bare `<img>` node, stranding any wrapper/handles at the old location. That doesn't
apply here: dragging a ProseMirror `NodeSelection` moves the *whole node* — wrapper and all —
via a real document transform, so the handles can safely live inside it.

Dragging a handle writes width/height straight to the live `<img>` element for immediate
visual feedback, then on release dispatches `tr.setNodeMarkup` to persist it as real node
attrs. Alignment (left/center/right) is a toolbar-button check: if the current selection is
a `NodeSelection` of an image (`isImageSelected`), the align buttons set the image's own
`align` attr instead of calling `setTextAlign` (an image has no block ancestor of its own for
text-align to center).

Export mirrors this: `_docx_add_image` in `app.py` prefers the image's `width` attr
(converted CSS-px→EMU) over the source file's native pixel size, and a paragraph's alignment/
`text-align` is set from the same `align` attr — both exporters previously had no way to
reflect an on-screen resize at all.

## Known gaps

- **Tables.** There's no Table extension yet, and neither exporter has a table-rendering
  path (the old ones did, via a hand-rolled `_docx_render_table`/`REPORT_ALLOWED_TAGS`
  entry — kept for pasted-in tables, since the old toolbar never exposed a table button
  either). Pasting a table today gets flattened by Tiptap's own paste handling. Adding
  `@tiptap/extension-table` plus corresponding exporter support is future work if needed.
- **Legacy document migration** is best-effort, not lossless — see the "Data model"
  section above.

## Build

Tiptap ships as npm/ESM packages; this project has no other JS build step, so
`package.json` + [esbuild](https://esbuild.github.io/) is the whole toolchain:

```
npm install
npm run build     # bundles static/src/editor.js -> static/dist/editor.bundle.js
npm run watch     # same, rebuilding on change, for local development
```

Flask serves the resulting bundle as an ordinary static file — no Node process at runtime.
