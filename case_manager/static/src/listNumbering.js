// Multilevel numbering ("section numbers"), e.g. 1(2)(3) or 1(b)3(c).
//
// Port of the numbering engine previously hand-rolled against a raw
// contenteditable <ol>/<li> tree (see MULTILEVEL_SECTION_NUMBERING.md) onto
// Tiptap/ProseMirror's real list schema. The template and any restart/
// continue override are now genuine node attributes instead of parsed
// class/style strings:
//
//   - `numCascade` (bool) + `numLevels` ([{type, wrap}, ...] up to 6 entries)
//     live on the OUTERMOST <orderedList> node of a nesting chain only --
//     exactly like the old `cascade`/`lvl1-*..lvl6-*` classes, which also
//     only ever appeared on the top-level <ol>. A nested <orderedList>
//     created by Tab/sinkListItem carries neither attribute and inherits
//     its ancestor's template when rendering (see findRootOrderedList/
//     buildMarkerIndex below) -- so indenting never needs to touch them.
//   - `start` (ProseMirror's native ordered-list attribute) is reused as the
//     restart/continue/set-value mechanism: "restart this item's numbering"
//     splits the list into two sibling <orderedList> nodes at that item, the
//     second one carrying `start = n` and a copy of the first's template
//     attrs (see splitListForRestart below) -- a real document transform,
//     not a CSS counter-reset hack.
//
// Marker text (the actual "1(2)(3) " shown before an item) is never stored
// in the document -- like the old CSS-counter approach, it's always
// recomputed, but here directly from the ProseMirror doc via a decoration
// plugin (buildMarkerIndex/markerPlugin) rather than relying on the
// browser's CSS counter engine. `app.py`'s exporters mirror this same
// algorithm over the saved JSON (see _ListNumberingState there) -- the two
// must stay in sync, same as the old JS/Python pair did.
import { Node, Extension } from "@tiptap/core";
import { OrderedList as BaseOrderedList, ListItem as BaseListItem } from "@tiptap/extension-list";
import { Plugin, PluginKey, TextSelection } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { Fragment } from "@tiptap/pm/model";

export const LIST_MAX_LEVELS = 6;

const LIST_WRAPS = {
  none: ["", ""],
  period: ["", "."],
  paren: ["(", ")"],
  trail: ["", ")"],
};

const DEFAULT_LEVEL = { type: "decimal", wrap: "period" };
const DEFAULT_LEVELS = Array.from({ length: LIST_MAX_LEVELS }, () => ({ ...DEFAULT_LEVEL }));

export const LIST_PRESETS = {
  simple: { cascade: false, levels: DEFAULT_LEVELS.map((l) => ({ ...l })) },
  "legal-numeric": {
    cascade: true,
    levels: Array.from({ length: LIST_MAX_LEVELS }, (_, i) =>
      i === 0 ? { type: "decimal", wrap: "none" } : { type: "decimal", wrap: "paren" }
    ),
  },
  "legal-alpha": {
    cascade: true,
    levels: Array.from({ length: LIST_MAX_LEVELS }, (_, i) =>
      i % 2 === 0 ? { type: "decimal", wrap: "none" } : { type: "alpha", wrap: "paren" }
    ),
  },
  alpha: { cascade: false, levels: Array.from({ length: LIST_MAX_LEVELS }, () => ({ type: "alpha", wrap: "period" })) },
  roman: { cascade: false, levels: Array.from({ length: LIST_MAX_LEVELS }, () => ({ type: "roman", wrap: "period" })) },
  "decimal-dotted": {
    cascade: true,
    levels: Array.from({ length: LIST_MAX_LEVELS }, () => ({ type: "decimal", wrap: "period" })),
  },
  "decimal-alpha-dotted": {
    cascade: true,
    levels: Array.from({ length: LIST_MAX_LEVELS }, (_, i) =>
      i % 2 === 0 ? { type: "decimal", wrap: "period" } : { type: "alpha", wrap: "period" }
    ),
  },
};

function toAlpha(n, upper) {
  let s = "";
  while (n > 0) {
    n -= 1;
    s = String.fromCharCode(97 + (n % 26)) + s;
    n = Math.floor(n / 26);
  }
  return upper ? s.toUpperCase() : s;
}

const ROMAN_TABLE = [
  [1000, "m"], [900, "cm"], [500, "d"], [400, "cd"], [100, "c"], [90, "xc"],
  [50, "l"], [40, "xl"], [10, "x"], [9, "ix"], [5, "v"], [4, "iv"], [1, "i"],
];
function toRoman(n, upper) {
  let s = "";
  for (const [value, sym] of ROMAN_TABLE) {
    while (n >= value) {
      s += sym;
      n -= value;
    }
  }
  return upper ? s.toUpperCase() : s;
}

function formatCounterValue(n, type) {
  if (type === "alpha") return toAlpha(n, false);
  if (type === "upalpha") return toAlpha(n, true);
  if (type === "roman") return toRoman(n, false);
  if (type === "uproman") return toRoman(n, true);
  return String(n);
}

function normalizeSpec(spec) {
  const levels = [];
  for (let i = 0; i < LIST_MAX_LEVELS; i++) levels.push(spec?.levels?.[i] || { ...DEFAULT_LEVEL });
  return { cascade: !!spec?.cascade, levels };
}

// One of these per top-level <orderedList>, threaded through the recursive
// doc walk below -- mirrors _ListNumberingState in app.py exactly.
class ListNumberingState {
  constructor(spec) {
    this.spec = normalizeSpec(spec);
    this.counters = new Array(LIST_MAX_LEVELS).fill(0);
  }
  enterList(depth, start) {
    if (typeof start === "number") this.counters[depth - 1] = start - 1;
  }
  nextValue(depth) {
    this.counters[depth - 1] += 1;
    return this.counters[depth - 1];
  }
  markerText(depth) {
    const from = this.spec.cascade ? 1 : depth;
    let out = "";
    for (let i = from; i <= depth; i++) {
      const lvl = this.spec.levels[i - 1] || DEFAULT_LEVEL;
      const [pre, suf] = LIST_WRAPS[lvl.wrap] || LIST_WRAPS.period;
      out += pre + formatCounterValue(this.counters[i - 1], lvl.type) + suf;
    }
    return out + " ";
  }
}

// Walks the whole document once, computing every ordered-list item's marker
// text and value. Returns { markers: [{pos, depth, value, text}], byLiPos }
// where `pos` is the position of the <listItem> node itself (so callers can
// look a specific item up by its own position) and `text` is what should be
// displayed before that item's content.
function buildMarkerIndex(doc) {
  const markers = [];
  const byLiPos = new Map();

  function walk(node, basePos, state, depth) {
    node.forEach((child, offset) => {
      const absPos = basePos + offset;
      if (child.type.name === "orderedList") {
        const childDepth = depth + 1;
        const childState = state || new ListNumberingState(child.attrs.numLevels ? { cascade: child.attrs.numCascade, levels: child.attrs.numLevels } : null);
        childState.enterList(childDepth, child.attrs.start);
        walk(child, absPos + 1, childState, childDepth);
      } else if (child.type.name === "bulletList") {
        walk(child, absPos + 1, state, depth + 1);
      } else if (child.type.name === "listItem") {
        if (state) {
          const value = state.nextValue(depth);
          const text = state.markerText(depth);
          markers.push({ pos: absPos, depth, value, text });
          byLiPos.set(absPos, { depth, value, text });
        }
        walk(child, absPos + 1, state, depth);
      } else {
        walk(child, absPos + 1, state, depth);
      }
    });
  }

  walk(doc, 0, null, 0);
  return { markers, byLiPos };
}

// Outermost <orderedList> ancestor of the position `pos` resolves into,
// within its own continuous list-nesting chain (stops at the first non-
// list/list-item ancestor) -- mirrors topLevelListRoot() in the old
// document.js.
export function findRootOrderedList(doc, pos) {
  const $pos = doc.resolve(pos);
  // First find the nearest enclosing <orderedList> at all -- `pos` usually
  // resolves inside a paragraph/heading (the actual text the caret is in),
  // which isn't itself part of the list-nesting chain, so this can't start
  // by walking the chain from `pos` directly.
  let depth = null;
  for (let d = $pos.depth; d > 0; d--) {
    if ($pos.node(d).type.name === "orderedList") {
      depth = d;
      break;
    }
  }
  if (depth === null) return null;
  let result = { pos: $pos.before(depth), node: $pos.node(depth), depth };
  // Then keep climbing outward through listItem/bulletList/orderedList
  // links to the outermost list in the same continuous nesting chain.
  for (let d = depth - 1; d > 0; d--) {
    const name = $pos.node(d).type.name;
    if (name === "orderedList") {
      result = { pos: $pos.before(d), node: $pos.node(d), depth: d };
    } else if (name !== "listItem" && name !== "bulletList") {
      break;
    }
  }
  return result;
}

// Nearest enclosing <listItem>/<orderedList> pair around `pos`, plus the
// item's index within that list -- used by the restart/continue commands.
function findListItemContext(doc, pos) {
  const $pos = doc.resolve(pos);
  for (let d = $pos.depth; d > 0; d--) {
    if ($pos.node(d).type.name === "listItem" && $pos.node(d - 1)?.type.name === "orderedList") {
      const liPos = $pos.before(d);
      const olPos = $pos.before(d - 1);
      const olNode = $pos.node(d - 1);
      const index = $pos.index(d - 1);
      return { liPos, olPos, olNode, index };
    }
  }
  return null;
}

const markerPluginKey = new PluginKey("listMarkers");

export const ListMarkers = Extension.create({
  name: "listMarkers",

  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: markerPluginKey,
        props: {
          decorations(state) {
            const { markers } = buildMarkerIndex(state.doc);
            const decos = [];
            for (const m of markers) {
              const liNode = state.doc.nodeAt(m.pos);
              if (!liNode) continue;
              const first = liNode.firstChild;
              const insertPos = first && first.isTextblock ? m.pos + 2 : m.pos + 1;
              decos.push(
                Decoration.widget(
                  insertPos,
                  () => {
                    const span = document.createElement("span");
                    span.className = "list-marker";
                    span.contentEditable = "false";
                    span.textContent = m.text;
                    return span;
                  },
                  { side: -1, key: `lm-${m.pos}-${m.text}` }
                )
              );
            }
            return DecorationSet.create(state.doc, decos);
          },
        },
      }),
    ];
  },
});

// Extends Tiptap's stock OrderedList with the template attributes above.
export const OrderedList = BaseOrderedList.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      numCascade: {
        default: false,
        keepOnSplit: false,
        parseHTML: () => false,
        renderHTML: () => ({}),
      },
      numLevels: {
        default: null,
        keepOnSplit: false,
        parseHTML: () => null,
        renderHTML: () => ({}),
      },
    };
  },
});

// Stock ListItem requires its first child to be literally a <paragraph>
// ("paragraph block*"), so applying a block-format change (e.g. the
// toolbar's Heading 1/2/3) to a list item's text fails Tiptap's setNode
// schema check and falls back to clearNodes(), which lifts the item clean
// out of the list. Widening the leading slot to accept a heading too keeps
// the item inside the list -- everything else in this file (marker
// placement, empty-item detection, restart/continue) already works off
// `firstChild.isTextblock`, not a hardcoded "paragraph", so this doesn't
// disturb any of it.
export const ListItem = BaseListItem.extend({
  content: "(paragraph | heading) block*",
});

// Applies a preset (by key) or a custom {cascade, levels} spec to the
// top-level list the selection is (or will be) inside of -- creating an
// ordered list first if the caret isn't in one yet.
export function applyListTemplate(editor, presetKeyOrSpec) {
  const spec = typeof presetKeyOrSpec === "string" ? LIST_PRESETS[presetKeyOrSpec] : presetKeyOrSpec;
  if (!spec) return;
  let chain = editor.chain().focus();
  if (!editor.isActive("orderedList")) chain = chain.toggleOrderedList();
  chain
    .command(({ tr, state }) => {
      const root = findRootOrderedList(state.doc, state.selection.from);
      if (!root) return false;
      tr.setNodeMarkup(root.pos, undefined, { ...root.node.attrs, numCascade: !!spec.cascade, numLevels: spec.levels });
      return true;
    })
    .run();
}

// Reads the template currently applied to the top-level list the selection
// is in (for populating the "Custom levels..." modal), or null.
export function currentListTemplate(editor) {
  const root = findRootOrderedList(editor.state.doc, editor.state.selection.from);
  if (!root) return null;
  return normalizeSpec(root.node.attrs.numLevels ? { cascade: root.node.attrs.numCascade, levels: root.node.attrs.numLevels } : null);
}

// The number the item at `pos` (a <listItem> position, as returned by
// findListItemContext) currently displays -- for the "Set numbering
// value..." prompt's default and "Continue from previous list".
function computedValueAt(doc, liPos) {
  const { byLiPos } = buildMarkerIndex(doc);
  return byLiPos.get(liPos)?.value ?? null;
}

// Shared by restart / set-value / continue: makes the list item at `liPos`
// (inside orderedList `ctx`) start counting from `n` at its own level.
// If it's the list's first item, this is just a `start` update; otherwise
// the list is split in two around it, the tail copy carrying `start = n`
// and the same template as the original (see module comment above).
function setListStartValue({ tr, state, dispatch }, ctx, n) {
  const { olPos, olNode, index } = ctx;
  if (index === 0) {
    if (dispatch) tr.setNodeMarkup(olPos, undefined, { ...olNode.attrs, start: n });
    return true;
  }
  const items = [];
  olNode.forEach((child) => items.push(child));
  const before = items.slice(0, index);
  const after = items.slice(index);
  const sharedAttrs = { numCascade: olNode.attrs.numCascade, numLevels: olNode.attrs.numLevels };
  const olBefore = olNode.type.create({ ...olNode.attrs, ...sharedAttrs }, Fragment.from(before));
  const olAfter = olNode.type.create({ ...olNode.attrs, ...sharedAttrs, start: n }, Fragment.from(after));
  if (dispatch) {
    tr.replaceWith(olPos, olPos + olNode.nodeSize, Fragment.from([olBefore, olAfter]));
  }
  return true;
}

export function restartNumberingAt(editor, pos) {
  const ctx = findListItemContext(editor.state.doc, pos);
  if (!ctx) return;
  editor.commands.command((props) => setListStartValue(props, ctx, 1));
}

export function currentNumberAt(editor, pos) {
  const ctx = findListItemContext(editor.state.doc, pos);
  if (!ctx) return null;
  return computedValueAt(editor.state.doc, ctx.liPos);
}

export function setNumberingValueAt(editor, pos, n) {
  const ctx = findListItemContext(editor.state.doc, pos);
  if (!ctx) return;
  editor.commands.command((props) => setListStartValue(props, ctx, n));
}

export function continueFromPreviousListAt(editor, pos) {
  const ctx = findListItemContext(editor.state.doc, pos);
  if (!ctx) return;
  const { doc } = editor.state;
  const $ol = doc.resolve(ctx.olPos);
  const parent = $ol.parent;
  const indexInParent = $ol.index();

  // Walk backward from ctx.olPos (a known-good position) subtracting each
  // preceding sibling's own size, rather than summing forward from some
  // computed "start of parent" position -- simpler to get right, since
  // ctx.olPos is already exactly "the position right before this list".
  let prevIndex = indexInParent - 1;
  let prevNode = null;
  let prevOlPos = ctx.olPos;
  while (prevIndex >= 0) {
    const n = parent.child(prevIndex);
    prevOlPos -= n.nodeSize;
    if (n.type.name === ctx.olNode.type.name) {
      prevNode = n;
      break;
    }
    prevIndex -= 1;
  }
  if (!prevNode) return;

  let lastLiPos = null;
  prevNode.forEach((child, offset) => {
    lastLiPos = prevOlPos + 1 + offset;
  });
  const prevValue = lastLiPos !== null ? computedValueAt(doc, lastLiPos) : 0;
  editor.commands.command((props) => setListStartValue(props, ctx, (prevValue || 0) + 1));
}

// ---------------------------------------------------------------------
// Enter on an empty list item exits the list outright (converts that line
// to a plain paragraph), regardless of nesting depth, and continues the
// tail list's numbering from where the removed item would have been --
// see the "Enter on an empty item" section of MULTILEVEL_SECTION_NUMBERING.md
// for the contenteditable-era version of this same behavior.
// ---------------------------------------------------------------------
function isEmptyListItem(state, $from) {
  for (let d = $from.depth; d > 0; d--) {
    if ($from.node(d).type.name === "listItem") {
      const li = $from.node(d);
      if (li.childCount > 1) return null; // has a nested list -> not "empty"
      const first = li.firstChild;
      if (!first || !first.isTextblock || first.content.size > 0) return null;
      return { liPos: $from.before(d), liNode: li };
    }
  }
  return null;
}

export function exitEmptyListItemOnEnter(editor) {
  const { state } = editor;
  const { $from } = state.selection;
  if (!state.selection.empty) return false;
  const hit = isEmptyListItem(state, $from);
  if (!hit) return false;
  const ctx = findListItemContext(state.doc, hit.liPos + 1);
  if (!ctx) return false;

  const resumeValue = computedValueAt(state.doc, hit.liPos);
  const items = [];
  ctx.olNode.forEach((child) => items.push(child));
  const before = items.slice(0, ctx.index);
  const after = items.slice(ctx.index + 1);
  const sharedAttrs = { numCascade: ctx.olNode.attrs.numCascade, numLevels: ctx.olNode.attrs.numLevels };

  const paragraphType = editor.schema.nodes.paragraph;
  const replacement = [];
  if (before.length) replacement.push(ctx.olNode.type.create({ ...ctx.olNode.attrs, ...sharedAttrs }, Fragment.from(before)));
  replacement.push(paragraphType.create());
  if (after.length) {
    replacement.push(
      ctx.olNode.type.create({ ...ctx.olNode.attrs, ...sharedAttrs, start: (resumeValue || 0) + 1 }, Fragment.from(after))
    );
  }

  const tr = state.tr;
  tr.replaceWith(ctx.olPos, ctx.olPos + ctx.olNode.nodeSize, Fragment.from(replacement));
  const paragraphPos = ctx.olPos + (before.length ? Fragment.from(before).size + 2 : 0) + 1;
  tr.setSelection(TextSelection.create(tr.doc, paragraphPos));
  editor.view.dispatch(tr);
  return true;
}

// ---------------------------------------------------------------------
// Plain-block indent (Tab/Shift+Tab on a paragraph/heading that isn't in a
// list): a purely visual left-margin nudge, same idea as blockIndentEl() in
// the old document.js.
// ---------------------------------------------------------------------
export const BlockIndent = Extension.create({
  name: "blockIndent",
  addOptions() {
    return { types: ["paragraph", "heading"], step: 40, max: 10 };
  },
  addGlobalAttributes() {
    return [
      {
        types: this.options.types,
        attributes: {
          indent: {
            default: 0,
            parseHTML: (el) => {
              const n = parseInt(el.style.marginLeft || "0", 10);
              return Number.isFinite(n) ? Math.round(n / this.options.step) : 0;
            },
            renderHTML: (attrs) => (attrs.indent ? { style: `margin-left: ${attrs.indent * this.options.step}px` } : {}),
          },
        },
      },
    ];
  },
  addCommands() {
    return {
      indentBlock:
        (direction) =>
        ({ state, tr, dispatch }) => {
          const { $from } = state.selection;
          const node = $from.parent;
          if (!this.options.types.includes(node.type.name)) return false;
          const cur = node.attrs.indent || 0;
          const next = Math.max(0, Math.min(this.options.max, cur + direction));
          if (next === cur) return false;
          if (dispatch) tr.setNodeMarkup($from.before($from.depth), undefined, { ...node.attrs, indent: next });
          return true;
        },
    };
  },
});
