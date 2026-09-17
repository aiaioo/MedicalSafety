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
// whole -- it's never split mid-paragraph, matching the old behavior.
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

// Measures each top-level node's rendered position (via ProseMirror's own
// pos<->DOM mapping, not by trusting DOM child order) and decides where a
// page break needs to go. Because inserting a break changes the page's
// remaining content height for everything below it, and that change can
// only take effect once the resulting widget decorations actually render,
// this can under-count on the very first pass over a multi-page document --
// the view's `update()` hook (below) re-invokes this after every render
// until the computed breaks stop changing, so it settles within a couple of
// frames rather than needing to get it exactly right in one measurement.
function computeBreaks(view, margins) {
  const m = marginsPx(margins);
  const pageContentHeight = PAGE_HEIGHT_PX - m.header - m.footer;
  const editorRect = view.dom.getBoundingClientRect();
  let pageContentTop = editorRect.top + m.header;
  let prevBottom = null;
  const breaks = [];
  let pageNum = 1;

  view.state.doc.forEach((_node, offset) => {
    const dom = view.nodeDOM(offset);
    if (!(dom instanceof HTMLElement)) return;
    const rect = dom.getBoundingClientRect();
    if (prevBottom !== null && rect.bottom - pageContentTop > pageContentHeight) {
      const fillerBefore = Math.max(0, pageContentHeight - (prevBottom - pageContentTop) + m.footer);
      pageNum += 1;
      breaks.push({ pos: offset, fillerBefore, headerAfter: m.header, pageNum });
      pageContentTop += pageContentHeight + m.footer + GAP_PX + m.header;
    }
    prevBottom = rect.bottom;
  });
  return breaks;
}

function breaksEqual(a, b) {
  if (a.length !== b.length) return false;
  return a.every((x, i) => x.pos === b[i].pos && Math.abs(x.fillerBefore - b[i].fillerBefore) < 0.5);
}

function renderBreakWidget(b) {
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
            return meta || value;
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
            const newBreaks = computeBreaks(editorView, options.getMargins());
            const current = paginationKey.getState(editorView.state).breaks;
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
