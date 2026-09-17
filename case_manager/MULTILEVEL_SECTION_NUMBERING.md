# Multilevel section numbering — design notes

> **Superseded.** The CSS-counter/class-based numbering engine described here has been
> replaced by real ProseMirror node attributes plus a decoration plugin — see
> `TIPTAP_EDITOR_DESIGN.md`'s "Multilevel numbering" section for the current
> implementation. This doc is kept because the *feature requirements* it documents (the
> preset/cascade/restart-continue semantics, the Enter/Tab behaviors) are unchanged; only
> the mechanism is.

Added so numbered lists in the document editor (see `EDITOR_DESIGN.md`) can be used for
legal/medical section numbers — formats like `1(2)(3)` or the alternating `1(b)3(c)` — with
per-item restart/continue control, on top of fixing two pre-existing bugs in how Enter and
Tab behaved around list items. Implemented across the same four files `EDITOR_DESIGN.md`
already documents:

- `templates/document.html` — the numbering-style split button/dropdown, the Custom levels
  modal, and an empty `<style id="dynamicListStyles">` tag
- `static/document.js` — the numbering engine, the Enter/Tab fixes, the right-click menu
- `static/style.css` — the default (untemplated) list-marker CSS
- `app.py` — a Python port of the same numbering algorithm, used by both exporters

## Why this needed a real design pass

The editor has no rich-document model — no ProseMirror/Slate-style schema, no node attrs
(§ `EDITOR_DESIGN.md` "Data flow summary"). Numbered lists were, until now, 100% native:
clicking "1. List" just called `execCommand("insertOrderedList")` and the browser numbered
plain `<ol>/<li>` itself. No numbering *value* was ever stored anywhere, so there was
nothing to hang a custom format, a restart point, or a "continue from" relationship off of.
Building section numbers meant building that missing layer from scratch — but doing it in a
way that doesn't require a real schema, since introducing one here would be a much bigger
change than the feature warrants.

## Design decisions

These were confirmed up front rather than assumed, since each has a materially different
implementation cost:

- **Numbering style is a fixed template per level**, Word-multilevel-list style — e.g.
  level 1 is always decimal, level 2 is always `(b)`, etc. — not a per-item override at a
  given level. Depth is controlled by the existing Tab/Shift+Tab indent mechanism
  unchanged.
- **Restart/continue/set-value is a right-click action** on a list item, not a toolbar
  control.
- **Enter on an empty list item exits the list outright** (converts that line to a plain
  paragraph), regardless of nesting depth — not Word's two-step "outdent one level, then
  exit on the next Enter" behavior. Every other Enter case (splitting a non-empty item) is
  untouched, since it wasn't reported as broken.
- **PDF and DOCX export must reproduce the same numbers as the editor**, not just the live
  browser view — since the exported document is often the actual deliverable.

## Data model: everything lives in `class`/`style`, not a new schema

A template and its restart state are encoded entirely as attributes on the existing
`<ol>`/`<li>` markup:

- **Template** — classes on the *top-level* `<ol>` (the list root; an `<ol>` not itself
  nested inside an `<li>`):
  - `cascade` — a presence flag. Level *N*'s marker concatenates levels 1..*N* (needed for
    `1(2)(3)` and `1(b)3(c)`). Absent: each level counts independently — today's native
    look, and the default for the plain "1. List" button, so pre-existing documents and
    workflows render unchanged.
  - `lvl1-<type>-<wrap>` through `lvl6-<type>-<wrap>` — one class per level, always emitted
    together when a non-default template is applied (levels beyond what's actually nested
    are simply unused). `type` ∈ `decimal | alpha | upalpha | roman | uproman`, `wrap` ∈
    `none | period | paren | trail` (`1` / `1.` / `(1)` / `1)`).
  - No classes at all ⇒ the default template (decimal/period, no cascade) — so untouched or
    pasted-in `<ol>`s behave exactly as before.
  - Nested `<ol>`s created by `listIndentItem()`/`listOutdentRun()` (§ `EDITOR_DESIGN.md`
    "Tab / Shift+Tab indent") need **no classes of their own** — depth is pure DOM nesting,
    and the generated CSS (below) keys off `ol ol ol...` descendant chains from the classed
    root. This is why Tab/Shift+Tab didn't need to change at all for this feature.
- **Restart / continue / set-value** — `style="counter-reset: c<level> <n>"` on any `<li>`
  (restarts *that item's* level from `n+1`) or on the top-level `<ol>` itself (sets the
  whole list's starting value). One mechanism serves all three right-click actions —
  "restart" is just `n = 0`, "set value" is `n = target - 1`, "continue" computes `n` from
  wherever the previous list left off.

Both `class` and `style` are already passed through the sanitizer unrestricted (`class`
entirely; `style` via a regex that already permits `counter-reset: c2 4`-shaped values —
`_clean_report_attrs` in `app.py`), so **no backend attribute-whitelist change was needed**
for any of this to round-trip through save/reload.

## Live rendering: CSS counters, not JS-computed labels

Given a fixed template per top-level list, marker text is generated as real CSS
(`counter-reset`/`counter-increment`/`::before { content: ... }`) rather than computed and
written into the DOM by JavaScript on every keystroke. This matters a lot for a hand-rolled
`contenteditable` editor with no virtual-DOM/observer-driven re-render: the browser's own
counter engine updates live, for free, no matter how the list is edited (typed into, pasted
into, reordered by drag) — there's no "renumber the whole document" pass to keep in sync.

Two layers of CSS:

- **Static default** (`static/style.css`, right after the `.editor h1/h2/h3` rules) — six
  levels of plain independent decimal counters (`c1`..`c6`), unconditionally targeting bare
  `.editor ol`/`.editor ol ol`/etc. This is what every list looks like until a template is
  applied, and it's also what makes restart/continue work on an *untemplated* list — the
  counters exist regardless of template.
- **Generated per-template** (`#dynamicListStyles`, a `<style>` tag living outside `#editor`
  so it's never part of saved content) — `regenerateListStyles()` in `document.js` scans the
  document for distinct `lvl*`/`cascade` class combinations actually in use and emits the
  matching nested-counter CSS for each, e.g. for `legal-numeric` at depth 3:
  ```css
  .editor ol.lvl1-decimal-none.lvl2-decimal-paren.lvl3-decimal-paren ol ol {
    list-style: none; counter-reset: c3;
  }
  .editor ol.lvl1-decimal-none.lvl2-decimal-paren.lvl3-decimal-paren ol ol > li {
    counter-increment: c3;
  }
  .editor ol.lvl1-decimal-none.lvl2-decimal-paren.lvl3-decimal-paren ol ol > li::before {
    content: counter(c1) "(" counter(c2) ")" "(" counter(c3) ")" " ";
  }
  ```
  `counter(c1)` inside the level-3 rule still resolves to the nearest ancestor's `c1` value
  even though it isn't reset at that selector — the standard nested-counter technique, and
  the reason cascading concatenation falls out naturally instead of needing per-item
  bookkeeping. Templated selectors always out-specify the plain-`ol` default rules (a class
  selector beats a bare type selector), so applying a template cleanly overrides the default
  without `!important` or careful rule ordering.
  This CSS is regenerated on load and whenever a template is applied — never generated
  per-keystroke, and never persisted (it's rebuilt from the classes on the lists themselves,
  which *are* persisted).

Why runtime generation at all, rather than hand-authoring every combination in
`style.css`: with 5 types × 4 wraps per level × up to 6 levels, the combination space is
far too large to pre-author, and a custom per-level spec (§ Custom levels below) is
genuinely open-ended. Generating only the combinations actually present in the document
keeps the emitted CSS small regardless.

`computeCounterValueAtLevel(li)` (`document.js`) is the one place the *value* a counter
would display gets computed in JS rather than left to the browser — needed for showing
"currently N" in the "Set numbering value" prompt and for the Enter-key auto-continue logic
below. It mirrors CSS counter semantics exactly (walk preceding siblings, apply any
`counter-reset` override encountered, then increment), so it always agrees with what's
actually on screen.

## Toolbar: presets + custom levels

The "1. List" button (`document.html`) became a split control: the button itself still does
today's `insertOrderedList` (now additionally tagging the new `<ol>` with the last-used
template, defaulting to "Simple" — today's look), plus a chevron opening a dropdown
(reusing the existing `.menu-dropdown` pattern already used by the File menu) with five
presets and a "Custom levels…" option:

| Preset | cascade | levels |
|---|---|---|
| Simple (default) | no | decimal/period at every level |
| Legal numeric | yes | `1` then `(2)`/`(3)`/... at every deeper level |
| Legal alpha | yes | alternating `decimal/none` and `alpha/paren` per level — `1`, `(b)`, `3`, `(c)`, ... |
| Alpha | no | alpha/period at every level |
| Roman | no | roman/period at every level |

"Custom levels…" opens a modal (`#listLevelsModal`, modeled on the existing Page Setup
modal) with six rows (Level 1–6), each a Type select and a Wrap select, plus a cascade
checkbox — i.e. the same `{cascade, levels}` shape every preset already is, just
user-specified instead of named. Applying either a preset or a custom spec calls
`applyListTemplate()`, which resolves the *top-level* root of whatever list the caret is
in (or creates one via `execCommand` if the caret isn't in a list yet), replaces its
template classes, and calls `regenerateListStyles()`.

## Right-click: restart / set value / continue

Modeled directly on the existing annotation-delete context menu in `static/viewer.js`
(same `.context-menu` CSS, same show/hide-on-outside-click pattern) — `document.js` adds a
`contextmenu` listener on `#editor` that, when the target is inside an `<ol>` item,
`preventDefault()`s the native menu and shows three actions, all built on
`setListStartValue(el, depth, n)`:

- **Restart numbering (start at 1)** — `setListStartValue(li, depth, 1)`.
- **Set numbering value…** — a `prompt()` pre-filled with the item's current computed
  value, then `setListStartValue(li, depth, n)`.
- **Continue from previous list** — finds the nearest earlier sibling list
  (`previousSiblingList()`, a plain DOM sibling search — works for top-level lists and
  equally for nested ones sharing a parent), computes its last item's value via
  `computeCounterValueAtLevel()`, and continues from `value + 1`.

## Enter on an empty item: exits the list, doesn't split it awkwardly

Previously, Enter inside a list item was left entirely to native `contentEditable`
behavior. Pressing Enter on an *empty* item (particularly one with items following it in
the same list) could leave the DOM in a state where a stray/malformed line sat between two
list fragments — the reported "an empty line is created, and the list seems to break at
that point" bug.

The fix (`document.js`) only intercepts Enter when the collapsed caret is in a genuinely
empty `<li>` (no text, no nested sublist — `isListItemEmpty()`); every other Enter case
still falls through to native handling unchanged. On an empty item:

1. `splitListAt(li)` removes the `<li>`. If it had following siblings, they're spliced into
   a **new** list of the same tag/class (the same "splice a run of siblings into a fresh
   list" shape as `listOutdentRun()` already uses for Shift+Tab — see `EDITOR_DESIGN.md`).
   A plain `<p>` is inserted at the removal point.
2. If the removed item was in an `<ol>` and there *is* a following split-off list, that new
   list's start is set (`setListStartValue`) to the value the removed item itself would
   have displayed (`computeCounterValueAtLevel(li)`, read *before* the removal). This is
   what makes numbering continue seamlessly across the exit point by default — the item
   "used up" that slot, so the next real item picks it back up — without the user needing
   to reach for the right-click menu. "Restart" is still one right-click away if they
   actually want a fresh 1.
3. The caret moves into the new `<p>`.

A defensive one-time pass on document load (`repairOrphanedListItems()`) converts any
`<li>` found sitting directly under `#editor` (not inside an `<ol>/<ul>`) into a plain
`<p>` — covering documents saved before this fix shipped, whose markup might already
contain that malformed shape from the old native behavior.

## The Tab bug was really a downstream symptom

The reported Tab bug ("pressing Tab from that empty line indents both the list above and
below") turned out not to need its own fix. `applyTabIndent()`'s existing dispatch
(`closestListItemOrTopBlock()`) already does the right thing for a plain `<p>` — it's
treated as an ordinary top-level block and just gets its own `margin-left` nudged
(`blockIndentEl()`), never touching any list. The actual bug was upstream: native Enter
could leave that "empty line" as something other than a clean `<p>` (e.g. still
list-adjacent in a way `closestListItemOrTopBlock()` misidentified). Once Enter is fully
under our own control (previous section), the line is always a genuine `<p>`, so Tab's
existing behavior is correct with no changes of its own — verified directly (see
Verification below) rather than assumed.

## Export fidelity: one Python port, shared by both exporters

Neither exporter can rely on the live browser's CSS counter engine. DOCX obviously can't
(it's assembled directly via `python-docx`, not rendered from HTML/CSS at all — see
`EDITOR_DESIGN.md` "Word (.docx) export"). PDF *might* have been able to, but PyMuPDF's
`Story` is a lightweight HTML/CSS engine of unverified `::before`/counter support, and
depending on that unverified behavior for something as visible as section numbers wasn't
worth the risk. Instead, both exporters compute the numbers themselves, in Python, using
one shared implementation (`app.py`, just above `_docx_render_blocks`):

- `_parse_list_template(class_attr)` — mirrors `parseListTemplateFromClassList()` in
  `document.js` exactly (same class-name scheme, same default fallback).
- `_ListNumberingState` — one instance per top-level `<ol>`, threaded through the
  recursive tree walk both exporters already do. `enter_list()`/`next_value()` mirror the
  CSS reset-then-increment semantics (`document.js`'s `computeCounterValueAtLevel()`), so a
  restart/continue `counter-reset` found in the parsed HTML's `style` attribute is honored
  identically. `marker_text()` mirrors `listMarkerContent()`'s cascade/wrap logic.
- `_to_alpha()`/`_to_roman()` — Python equivalents of what the *browser's* `counter()`
  function does natively for `lower-alpha`/`lower-roman` styles, since Python has no
  built-in for either.

**DOCX** (`_docx_render_blocks`): the `ol`/`li` recursion now carries `num_state`/`depth`
alongside the existing `list_ctx`. Each numbered `<li>` gets the computed marker text
prepended as a literal run, with a manual hanging indent (`paragraph_format.left_indent`/
`first_line_indent`, scaled by depth) — deliberately using the generic `"List Paragraph"`
style rather than Word's built-in `"List Number"` auto-numbering field, since Word's own
numbering can't represent an arbitrary custom format like `1(b)3(c)`. This trades away a
"live" Word list (you can't right-click it in Word and see numbering options) for correct
visual/text content, which is what a medical-record deliverable actually needs. `ul`
(bullet lists) are untouched — still Word's native `"List Bullet"` style.

**PDF** (`render_report_pdf`): rather than feed sanitized HTML straight to `fitz.Story` as
before, it now parses the HTML into a tree (reusing `_HTMLTreeBuilder`, the same parser the
DOCX path already uses), walks it with `_inject_list_markers()` — structurally identical to
the DOCX walk, just prepending marker text into each `<li>`'s children instead of building
docx paragraphs — then re-serializes back to an HTML string (`_serialize_html_tree()`,
the parser's inverse) before handing it to `Story`. `REPORT_PDF_CSS` gained
`list-style: none` (so `Story`'s own native markers don't double up with the injected text)
and a simple `li ol, li ul { margin-left: 18pt }` rule for nesting indentation — ordinary
box-model margin, which compounds correctly through normal nested layout without needing
per-depth selectors.

## A bug found along the way: a comment that broke its own CSS

While verifying the implementation in a browser, numbered items rendered with a doubled
marker (`1. 1. Section one`). The cause was a code comment, not the numbering logic: the
comment introducing the static default CSS in `style.css` said `<ol> carrying lvl1-*/
cascade classes`, and that literal `*/` closed the CSS comment early. Every following
character up through the *next* `*/` (twelve lines later) was then fed to the CSS parser as
one enormous invalid selector, which — per normal CSS error recovery — discarded the first
rule it terminated at (`.editor ol { list-style: none; ... }`) as garbage before parsing
resumed cleanly at the next rule. The net effect: `list-style: none` silently never applied,
so the browser's own native decimal marker rendered *alongside* the custom `::before` one.
Fixed by rewording the comment to avoid the sequence; re-verified with the exact repro
(§ Verification) showing a single marker afterward.

## Known limitations

- Capped at 6 levels (`LIST_MAX_LEVELS`, both `document.js` and `app.py`) — matches typical
  legal/technical section-numbering depth; raising it is a one-constant change plus more
  rows in the Custom levels modal.
- "Continue from previous list" looks at the nearest earlier *sibling* list only, not an
  arbitrary earlier list anywhere in the document — sufficient for the common case (an
  Enter-exit split, or two lists separated by a paragraph) without a document-wide search.
- Bullet lists (`<ul>`) are entirely unaffected by this feature — no templates, no
  restart/continue, no export-side marker computation. Nothing in the request asked for it.
- DOCX numbering is literal text, not a native Word numbering field (see § Export fidelity)
  — the tradeoff is intentional, not an oversight.

## Verification

Exercised end-to-end against a live instance (Flask dev server + Playwright), not just
read through:

- Built a 3-level "Legal numeric" list via Tab/Shift+Tab and confirmed markers
  `1` / `1(1)` / `1(1)(1)`.
- Built a "Legal alpha" list and confirmed the alternating `1` / `1(a)` / `1(a)1` pattern.
- Pressed Enter on an empty top-level item at the end of a list → confirmed a plain `<p>`,
  not a stray list fragment.
- Pressed Enter on an empty item in the *middle* of a list (items both before and after) →
  confirmed the list split into two `<ol>`s around a `<p>`, with the second `<ol>` carrying
  `style="counter-reset: c1 1"` so it displayed `2.` — i.e. numbering continued exactly as
  if the deleted item had never existed.
- Pressed Tab immediately on the resulting empty paragraph → confirmed only that
  paragraph's own `margin-left` changed, with the surrounding list(s) untouched.
- Right-clicked a list item and used "Set numbering value…" → confirmed the item's
  `style="counter-reset: c3 4"` and its re-rendered marker.
- Saved and reloaded a document with a custom template and a restart override → confirmed
  both round-tripped through the sanitizer unchanged.
- Downloaded the same document as PDF and DOCX → confirmed both showed the identical
  `1`, `1(1)`, `1(1)(5)` markers as the live editor.
