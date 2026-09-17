// Simulated A4 pagination: shows where the document will actually break
// across printed pages while editing, without mutating #editor's DOM
// directly (the old contenteditable-era approach -- inserting sibling
// spacer elements between top-level blocks via a MutationObserver -- would
// fight ProseMirror's own DOM reconciliation here). Page breaks and the
// blank filler that pads a short page out to full height are rendered as
// widget decorations instead: pure display, never part of the saved
// document, and never conflicting with ProseMirror's view of the DOM.
//
// A block that doesn't fit on the current page moves to the next page
// whole -- it's never split mid-paragraph, matching the old behavior. That
// atomicity only applies to actual text blocks (paragraphs/headings)
// though: a <listItem>'s own paragraph is atomic, but sibling list items
// (including nested sub-list items) are still separate break candidates --
// see collectBreakUnits below -- otherwise an entire multi-item list would
// count as a single unsplittable node, and a list that no longer quite fits
// on the current page would jump to the next page as one block, leaving
// the rest of the current page permanently blank.
import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";

const paginationKey = new PluginKey("pagination");

const PAGE_HEIGHT_PX = 1123; // A4 at 96dpi
const PT_TO_PX = 96 / 72;
const GAP_PX = 96 / 2.54; // fixed 1cm visual gap between simulated sheets

function marginsPx(margins) {
  return {
    left: margins.left * PT_TO_PX,
    right: margins.right * PT_TO_PX,
    header: margins.header * PT_TO_PX,
    footer: margins.footer * PT_TO_PX,
  };
}

// Flattens the doc into break candidates: top-level blocks as-is, but with
// orderedList/bulletList/listItem containers expanded into their children
// instead of counted as one node -- so a break can land between two
// sibling list items (at any nesting depth), or between an item's own
// paragraph and a nested sub-list inside it, without ever splitting a
// single paragraph/heading's own content. Mirrors the recursive walk
// buildMarkerIndex (listNumbering.js) uses to compute markers, for the
// same reason: list nesting is structural, not a unit of "atomic content".
function collectBreakUnits(doc) {
  const units = [];
  function walk(node, basePos) {
    node.forEach((child, offset) => {
      const absPos = basePos + offset;
      if (child.type.name === "orderedList" || child.type.name === "bulletList" || child.type.name === "listItem") {
        walk(child, absPos + 1);
      } else {
        units.push(absPos);
      }
    });
  }
  walk(doc, 0);
  return units;
}

// Measures each break unit's rendered position (via ProseMirror's own
// pos<->DOM mapping, not by trusting DOM child order) and decides where a
// page break needs to go. Because inserting a break changes the page's
// remaining content height for everything below it, and that change can
// only take effect once the resulting widget decorations actually render,
// this can under-count on the very first pass over a multi-page document --
// the view's `update()` hook (below) re-invokes this after every render
// until the computed breaks stop changing, so it settles within a couple of
// frames rather than needing to get it exactly right in one measurement.
//
// `oldBreaks` is the previously-rendered break set (positions kept in sync
// with doc edits by the plugin's `apply`, see below). Every getBoundingClientRect
// read below is against DOM that may already contain filler/break widgets
// from that previous pass, so raw rect.bottom values are inflated by
// whatever breaks were previously inserted above them. subtractInsertedFiller
// strips that out, remeasuring against the undecorated content flow, before
// this pass decides breaks fresh. Skipping that normalization makes a wrong
// break self-confirming: once its filler widget is real DOM space, later
// passes measure everything below it as already pushed down by a full page
// and never notice the break wasn't warranted -- and because that phantom
// page is now "full", the next unit can appear to overflow it too, cascading
// into extra spurious breaks (and, in the worst case, blank trailing pages).
function computeBreaks(view, margins, oldBreaks) {
  const m = marginsPx(margins);
  const pageContentHeight = PAGE_HEIGHT_PX - m.header - m.footer;
  // view.dom is the .tiptap-content child of #editor (see editor.js's
  // `element: editorEl` + editorProps.attributes), and #editor is the
  // element carrying the real top-padding (m.header) via CSS -- so
  // view.dom's own rect.top already sits just below that padding. Adding
  // m.header again here would double-count it, pushing the first page's
  // content-top (and every fillerBefore/break computed off it) down by a
  // full header's height.
  const editorRect = view.dom.getBoundingClientRect();
  let pageContentTop = editorRect.top;
  let prevBottom = null;
  const breaks = [];
  let pageNum = 1;

  // oldBreaks is sorted by pos (it's built by the same increasing-pos walk
  // below), so the cumulative filler height already rendered before a given
  // pos can be accumulated in one forward pass alongside it.
  let oldIdx = 0;
  let insertedBefore = 0;
  function subtractInsertedFiller(pos, bottom) {
    while (oldIdx < oldBreaks.length && oldBreaks[oldIdx].pos <= pos) {
      const b = oldBreaks[oldIdx];
      if (!b.trailing) insertedBefore += b.fillerBefore + GAP_PX + b.headerAfter;
      oldIdx++;
    }
    return bottom - insertedBefore;
  }

  for (const pos of collectBreakUnits(view.state.doc)) {
    const dom = view.nodeDOM(pos);
    if (!(dom instanceof HTMLElement)) continue;
    const bottom = subtractInsertedFiller(pos, dom.getBoundingClientRect().bottom);
    if (prevBottom !== null && bottom - pageContentTop > pageContentHeight) {
      const fillerBefore = Math.max(0, pageContentHeight - (prevBottom - pageContentTop) + m.footer);
      pageNum += 1;
      breaks.push({ pos, fillerBefore, headerAfter: m.header, pageNum });
      pageContentTop = prevBottom;
    }
    prevBottom = bottom;
  }

  // Pad the last page out to full page height too. Unlike the mid-document
  // fillerBefore above, this trailing filler doesn't need + m.footer: it
  // butts up against #editor's own real bottom padding (m.footer, applied
  // once via CSS), not a simulated one, so it only needs to fill the
  // remainder of the page's *content* area.
  if (prevBottom !== null) {
    const trailingFiller = Math.max(0, pageContentHeight - (prevBottom - pageContentTop));
    if (trailingFiller > 0.5) {
      breaks.push({ pos: view.state.doc.content.size, fillerBefore: trailingFiller, trailing: true });
    }
  }
  return breaks;
}

function breaksEqual(a, b) {
  if (a.length !== b.length) return false;
  return a.every(
    (x, i) => x.pos === b[i].pos && Math.abs(x.fillerBefore - b[i].fillerBefore) < 0.5 && !x.trailing === !b[i].trailing
  );
}

function renderBreakWidget(b) {
  // The trailing filler (after the last node) just pads the last page out
  // to full height -- there's no next page to break into, so it's a bare
  // blank div, not the filler/break-bar/filler triplet used mid-document.
  if (b.trailing) {
    const filler = document.createElement("div");
    filler.className = "page-filler";
    filler.contentEditable = "false";
    filler.style.height = b.fillerBefore + "px";
    return filler;
  }

  const wrap = document.createElement("div");
  wrap.className = "page-break-group";

  const fillerBefore = document.createElement("div");
  fillerBefore.className = "page-filler";
  fillerBefore.contentEditable = "false";
  fillerBefore.style.height = b.fillerBefore + "px";

  const brk = document.createElement("div");
  brk.className = "page-break";
  brk.contentEditable = "false";
  brk.dataset.page = String(b.pageNum);
  brk.style.height = GAP_PX + "px";

  const fillerAfter = document.createElement("div");
  fillerAfter.className = "page-filler";
  fillerAfter.contentEditable = "false";
  fillerAfter.style.height = b.headerAfter + "px";

  wrap.append(fillerBefore, brk, fillerAfter);
  return wrap;
}

export const Pagination = Extension.create({
  name: "pagination",

  addOptions() {
    return {
      getMargins: () => ({ left: 36, right: 36, header: 46, footer: 46 }),
      onPaginate: () => {},
    };
  },

  addProseMirrorPlugins() {
    const options = this.options;
    return [
      new Plugin({
        key: paginationKey,
        state: {
          init: () => ({ breaks: [] }),
          apply(tr, value) {
            const meta = tr.getMeta(paginationKey);
            if (meta) return meta;
            // Keep break positions valid across doc-changing transactions
            // that happen between recomputes (e.g. remote/collab updates,
            // or another plugin's own dispatch) -- computeBreaks relies on
            // these positions to line up with the *current* doc when it
            // subtracts already-rendered filler height, and a stale pos
            // would attribute a break's filler to the wrong node.
            if (!tr.docChanged) return value;
            return { breaks: value.breaks.map((b) => ({ ...b, pos: tr.mapping.map(b.pos) })) };
          },
        },
        props: {
          decorations(state) {
            const { breaks } = paginationKey.getState(state);
            if (!breaks.length) return null;
            return DecorationSet.create(
              state.doc,
              breaks.map((b) => Decoration.widget(b.pos, () => renderBreakWidget(b), { side: -1, key: `pb-${b.pos}` }))
            );
          },
        },
        view(editorView) {
          let raf = null;
          const recompute = () => {
            raf = null;
            const current = paginationKey.getState(editorView.state).breaks;
            const newBreaks = computeBreaks(editorView, options.getMargins(), current);
            if (!breaksEqual(newBreaks, current)) {
              editorView.dispatch(editorView.state.tr.setMeta(paginationKey, { breaks: newBreaks }));
            }
            options.onPaginate();
          };
          const schedule = () => {
            if (raf) return;
            raf = requestAnimationFrame(recompute);
          };
          schedule();
          return {
            update: () => schedule(),
            destroy() {
              if (raf) cancelAnimationFrame(raf);
            },
          };
        },
      }),
    ];
  },
});

// Forces the pagination plugin to re-measure even when nothing in the
// document changed -- needed after a window resize, an async image load,
// or a margin-only change (Page Setup), none of which produce a
// ProseMirror transaction on their own.
export function repaginate(editor) {
  editor.view.dispatch(editor.state.tr);
}
