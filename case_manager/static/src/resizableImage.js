// Resizable, alignable snippet images. Extends Tiptap's stock Image node
// with `width`/`height`/`align` attributes (persisted, unlike the old
// contenteditable editor's approach of writing straight to the <img>'s own
// inline style with nothing backing it up in the saved document -- these
// are now real node attrs that round-trip through save/load and export).
//
// Resize handles are a NodeView wrapping the <img>, shown on hover/select,
// rather than the old editor's separate #figureResizeOverlay positioned by
// hand from the hovered element's live getBoundingClientRect(). That
// approach existed only because native contenteditable image drag detaches
// just the bare <img> node, stranding any sibling wrapper/handles at the
// old location (see the old static/document.js comment above
// cleanLegacyDocFigures). ProseMirror doesn't have that problem: dragging a
// NodeSelection moves the whole node -- attrs included -- via a real
// document transform, so the handles can safely live inside the image's
// own wrapper this time.
import { Node } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { Image as BaseImage } from "@tiptap/extension-image";

// Snippet images show, per their `refStyle` attr, the image alone ("image"),
// the image with its annexure reference below it ("both"), or the reference
// alone ("reference"). The reference text -- "Annexure P-1, page 3" -- is
// looked up from `refs` ({docId: {page: text}}, see setSnippetRefs) so it
// follows the annexure as it changes; the server resolves it the same way
// on export.
const MISSING_REF = "[not in annexure]";
let refs = {};
let refMeta = {};
let annexureUrl = "";
const views = new Set();
const refListeners = new Set();

// The source document id and page a snippet image's src points at, or null.
export function snippetSource(src) {
  const m = /\/media\/snippets\/([^/]+)\/p(\d+)_[^/]*$/.exec((src || "").split("?")[0]);
  return m ? { docId: m[1], page: parseInt(m[2], 10) } : null;
}

// `next` is {texts: {docId: {page: text}}, meta: {docId: {label, order, pages: {page: {pos, number}}}}}.
export function setSnippetRefs(next, annexureHref) {
  refs = (next && next.texts) || {};
  refMeta = (next && next.meta) || {};
  if (annexureHref) annexureUrl = annexureHref;
  views.forEach((render) => render());
  refListeners.forEach((fn) => fn());
}

// Pieces of the merged text for consecutive references, mirroring the server's
// group_ref_pieces: [{text, key?: {docId, page}}] -- a key marks a clickable piece.
function joinAnd(n, i) {
  return i === 0 ? "" : i === n - 1 ? " and " : ", ";
}

function groupRefPieces(sources) {
  const byDoc = new Map();
  sources.forEach(({ docId, page }) => {
    if (!byDoc.has(docId)) byDoc.set(docId, new Map());
    byDoc.get(docId).set(page, refMeta[docId].pages[page]);
  });
  const docIds = [...byDoc.keys()].sort((a, b) => refMeta[a].order - refMeta[b].order);
  const pieces = [];
  docIds.forEach((docId, di) => {
    if (di) pieces.push({ text: joinAnd(docIds.length, di) });
    const label = refMeta[docId].label;
    const pages = [...byDoc.get(docId).entries()].sort((a, b) => a[1].pos - b[1].pos);
    const ranges = [];
    pages.forEach(([page, info]) => {
      if (info.number === "-") return;
      const last = ranges[ranges.length - 1];
      if (last && info.pos === last.pos + 1) Object.assign(last, { last: info.number, pos: info.pos });
      else ranges.push({ page, first: info.number, last: info.number, pos: info.pos });
    });
    if (!ranges.length) {
      pieces.push({ text: label, key: { docId, page: pages[0][0] } });
      return;
    }
    const plural = ranges.length > 1 || ranges[0].first !== ranges[0].last;
    pieces.push({ text: `${label}, page${plural ? "s" : ""} ` });
    ranges.forEach((r, i) => {
      if (i) pieces.push({ text: joinAnd(ranges.length, i) });
      pieces.push({ text: r.first === r.last ? r.first : `${r.first}-${r.last}`, key: { docId, page: r.page } });
    });
  });
  return pieces;
}

const TEXTBLOCKS = new Set(["paragraph", "heading"]);

// Runs of two or more snippetRef nodes with only whitespace, line breaks or
// blank paragraphs between them (mirrors the server's group_adjacent_refs).
// Each run: {refs: [{pos, node, source}], hide: [{from, to}] (inline ranges),
// blocks: [{pos, node}] (textblocks the run spans, the first one holds the merged reference)}.
function findRefRuns(container, base, runs) {
  let cur = null;
  let pending = [];
  let seen = [];
  const end = () => {
    if (cur && cur.refs.length > 1) runs.push(cur);
    cur = null;
  };
  container.forEach((child, offset) => {
    const pos = base + offset;
    if (!TEXTBLOCKS.has(child.type.name)) {
      end();
      if (!child.isLeaf && !child.isTextblock) findRefRuns(child, pos + 1, runs);
      return;
    }
    const block = { pos, node: child };
    if (cur) seen.push(block);
    child.forEach((n, off) => {
      const npos = pos + 1 + off;
      const source = n.type.name === "snippetRef" ? snippetSource(n.attrs.src) : null;
      if (source && refMeta[source.docId]?.pages[source.page]) {
        if (!cur) {
          cur = { refs: [{ pos: npos, node: n, source }], hide: [], blocks: [block] };
          pending = [];
          seen = [block];
        } else {
          cur.hide.push(...pending);
          pending = [];
          cur.refs.push({ pos: npos, node: n, source });
          cur.blocks = seen.slice();
        }
      } else if (n.type.name === "hardBreak" || (n.isText && !n.text.trim())) {
        if (cur) pending.push({ from: npos, to: npos + n.nodeSize });
      } else {
        end();
      }
    });
  });
  end();
}

function mergedRefWidget(run) {
  return () => {
    const dom = document.createElement("span");
    dom.className = "snippet-ref-inline snippet-ref-group";
    groupRefPieces(run.refs.map((r) => r.source)).forEach(({ text, key }) => {
      if (!key) {
        dom.appendChild(document.createTextNode(text));
        return;
      }
      const a = document.createElement("a");
      a.textContent = text;
      a.className = "snippet-ref-link";
      a.href = annexureUrl || "#";
      a.target = "_blank";
      a.rel = "noopener";
      a.title = "Open the annexure";
      dom.appendChild(a);
    });
    return dom;
  };
}

// Shows each run of consecutive references as one merged reference, leaving
// the underlying nodes untouched (the server merges the same runs on export).
const groupKey = new PluginKey("snippetRefGroups");
export const SnippetRefGroups = Node.create({
  name: "snippetRefGroups",
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: groupKey,
        view(view) {
          const refresh = () => view.dispatch(view.state.tr.setMeta(groupKey, true));
          refListeners.add(refresh);
          return { destroy: () => refListeners.delete(refresh) };
        },
        state: {
          init: (_, state) => build(state.doc),
          apply: (tr, old) => (tr.docChanged || tr.getMeta(groupKey) ? build(tr.doc) : old),
        },
        props: { decorations: (state) => groupKey.getState(state) },
      }),
    ];
  },
});

function build(doc) {
  const runs = [];
  findRefRuns(doc, 0, runs);
  const decos = [];
  runs.forEach((run) => {
    const first = run.refs[0];
    decos.push(Decoration.widget(first.pos, mergedRefWidget(run), { side: -1, key: `ref-group-${first.pos}-${run.refs.length}` }));
    run.refs.forEach((r) => decos.push(Decoration.node(r.pos, r.pos + r.node.nodeSize, { class: "snippet-ref-hidden" })));
    run.hide.forEach(({ from, to }) => decos.push(Decoration.inline(from, to, { class: "snippet-ref-hidden" })));
    // Blocks after the first disappear once nothing else is left in them.
    run.blocks.slice(1).forEach(({ pos, node }) => {
      let rest = false;
      node.forEach((n, off) => {
        const p = pos + 1 + off;
        const inRun = run.refs.some((r) => r.pos === p) || run.hide.some((h) => h.from === p);
        if (!inRun) rest = true;
      });
      if (!rest) decos.push(Decoration.node(pos, pos + node.nodeSize, { class: "snippet-ref-hidden" }));
    });
  });
  return DecorationSet.create(doc, decos);
}

function applyWrapperAttrs(wrapper, img, attrs) {
  img.src = attrs.src || "";
  if (attrs.alt) img.alt = attrs.alt;
  else img.removeAttribute("alt");
  if (attrs.width) img.style.width = attrs.width + "px";
  else img.style.removeProperty("width");
  if (attrs.height) img.style.height = attrs.height + "px";
  else img.style.removeProperty("height");

  const source = snippetSource(attrs.src);
  const style = source && (attrs.refStyle === "both" || attrs.refStyle === "reference") ? attrs.refStyle : "image";
  img.style.display = style === "reference" ? "none" : "";
  wrapper.classList.toggle("img-wrap-ref-only", style === "reference");
  // Whole annexed pages sit side by side, as many as fit a line.
  wrapper.classList.toggle("img-wrap-page", /\/p\d+_page\.png$/.test((attrs.src || "").split("?")[0]));
  const caption = wrapper.querySelector(".snippet-ref");
  if (caption) {
    const text = style === "image" ? "" : (refs[source.docId] || {})[source.page] || MISSING_REF;
    caption.style.display = text ? "" : "none";
    caption.textContent = text;
    caption.classList.toggle("missing", text === MISSING_REF);
  }

  const align = attrs.align || "left";
  if (align === "center") {
    wrapper.style.marginLeft = "auto";
    wrapper.style.marginRight = "auto";
  } else if (align === "right") {
    wrapper.style.marginLeft = "auto";
    wrapper.style.marginRight = "0";
  } else {
    wrapper.style.marginLeft = "0";
    wrapper.style.marginRight = "auto";
  }
}

const RESIZE_CORNERS = ["nw", "ne", "sw", "se"];
const MIN_SIZE = 24;

export const ResizableImage = BaseImage.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      width: {
        default: null,
        parseHTML: (el) => {
          const n = parseInt(el.style.width || el.getAttribute("width") || "", 10);
          return Number.isFinite(n) ? n : null;
        },
        renderHTML: (attrs) => (attrs.width ? { style: `width: ${attrs.width}px` } : {}),
      },
      height: {
        default: null,
        parseHTML: (el) => {
          const n = parseInt(el.style.height || el.getAttribute("height") || "", 10);
          return Number.isFinite(n) ? n : null;
        },
        renderHTML: (attrs) => (attrs.height ? { style: `height: ${attrs.height}px` } : {}),
      },
      align: {
        default: "left",
        parseHTML: () => "left",
        renderHTML: () => ({}),
      },
      // Set when the image was split out of a paragraph (by switching an inline
      // reference to an image): the text before/after it was cut into separate
      // paragraphs, which switching back to "Only reference" rejoins.
      joinBefore: { default: false, parseHTML: () => false, renderHTML: () => ({}) },
      joinAfter: { default: false, parseHTML: () => false, renderHTML: () => ({}) },
      refStyle: {
        default: "image",
        parseHTML: (el) => el.getAttribute("data-ref-style") || "image",
        renderHTML: (attrs) => (attrs.refStyle && attrs.refStyle !== "image" ? { "data-ref-style": attrs.refStyle } : {}),
      },
    };
  },

  addNodeView() {
    return ({ node, getPos, editor }) => {
      let currentNode = node;

      const wrapper = document.createElement("span");
      wrapper.className = "img-wrap";

      const img = document.createElement("img");
      img.className = "doc-snippet";
      wrapper.appendChild(img);
      const caption = document.createElement("span");
      caption.className = "snippet-ref";
      wrapper.appendChild(caption);
      applyWrapperAttrs(wrapper, img, currentNode.attrs);
      const render = () => applyWrapperAttrs(wrapper, img, currentNode.attrs);
      views.add(render);

      const handles = {};
      RESIZE_CORNERS.forEach((corner) => {
        const h = document.createElement("span");
        h.className = `resize-handle resize-handle-${corner}`;
        h.addEventListener("pointerdown", (e) => startResize(e, corner));
        wrapper.appendChild(h);
        handles[corner] = h;
      });

      function setHandlesVisible(visible) {
        RESIZE_CORNERS.forEach((c) => (handles[c].style.display = visible ? "block" : "none"));
      }
      setHandlesVisible(false);
      wrapper.addEventListener("mouseenter", () => setHandlesVisible(true));
      wrapper.addEventListener("mouseleave", () => setHandlesVisible(false));

      function startResize(e, corner) {
        e.preventDefault();
        e.stopPropagation();
        const handle = e.currentTarget;
        const startRect = img.getBoundingClientRect();
        const aspect = startRect.width / (startRect.height || 1);
        const startX = e.clientX;
        const signX = corner.endsWith("e") ? 1 : -1;

        function onMove(ev) {
          const newWidth = Math.max(MIN_SIZE, startRect.width + (ev.clientX - startX) * signX);
          img.style.width = newWidth + "px";
          img.style.height = newWidth / aspect + "px";
        }
        function onUp(ev) {
          handle.releasePointerCapture(ev.pointerId);
          handle.removeEventListener("pointermove", onMove);
          handle.removeEventListener("pointerup", onUp);
          const pos = typeof getPos === "function" ? getPos() : null;
          if (pos == null) return;
          editor.view.dispatch(
            editor.state.tr.setNodeMarkup(pos, undefined, {
              ...currentNode.attrs,
              width: Math.round(parseFloat(img.style.width)),
              height: Math.round(parseFloat(img.style.height)),
            })
          );
        }
        handle.setPointerCapture(e.pointerId);
        handle.addEventListener("pointermove", onMove);
        handle.addEventListener("pointerup", onUp);
      }

      return {
        dom: wrapper,
        update(updatedNode) {
          if (updatedNode.type.name !== "image") return false;
          currentNode = updatedNode;
          applyWrapperAttrs(wrapper, img, currentNode.attrs);
          return true;
        },
        selectNode() {
          wrapper.classList.add("img-wrap-selected");
          setHandlesVisible(true);
        },
        deselectNode() {
          wrapper.classList.remove("img-wrap-selected");
          setHandlesVisible(false);
        },
        destroy() {
          views.delete(render);
        },
        stopEvent(event) {
          return !!(event.target && event.target.classList && event.target.classList.contains("resize-handle"));
        },
      };
    };
  },
});

// Sets alignment on the image the current selection is (a NodeSelection
// of) -- used by the toolbar's align buttons when a single image is
// selected, instead of wrapping it in a text-align'd block like a
// paragraph would get (an image node has no block ancestor of its own to
// carry that).
export function setSelectedImageAlign(editor, align) {
  const { selection } = editor.state;
  if (selection.node?.type.name !== "image") return false;
  return editor
    .chain()
    .command(({ tr }) => {
      tr.setNodeMarkup(selection.from, undefined, { ...selection.node.attrs, align });
      return true;
    })
    .run();
}

export function isImageSelected(editor) {
  return editor.state.selection.node?.type.name === "image";
}

// A reference-only snippet inserted inline ("Annexure P-1, page 3" in the
// middle of a sentence). The block-level image node can't sit inside a
// paragraph, so this is a separate atom node; it shows the same looked-up
// reference text and is exported as plain (linked) text.
export const SnippetRef = Node.create({
  name: "snippetRef",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    // alt/width/height/align aren't shown; they ride along so the snippet gets
    // its size and alignment back if it's switched to an image again.
    return {
      src: { default: null },
      alt: { default: null },
      width: { default: null },
      height: { default: null },
      align: { default: "left" },
    };
  },

  parseHTML() {
    return [{ tag: "span[data-snippet-ref]", getAttrs: (el) => ({ src: el.getAttribute("data-snippet-ref") }) }];
  },

  renderHTML({ node }) {
    return ["span", { "data-snippet-ref": node.attrs.src }];
  },

  addNodeView() {
    return ({ node }) => {
      let currentNode = node;
      const dom = document.createElement("span");
      dom.className = "snippet-ref-inline";
      const render = () => {
        const source = snippetSource(currentNode.attrs.src);
        const text = source && (refs[source.docId] || {})[source.page];
        dom.textContent = text || MISSING_REF;
        dom.classList.toggle("missing", !text);
      };
      render();
      views.add(render);
      return {
        dom,
        update(updated) {
          if (updated.type.name !== "snippetRef") return false;
          currentNode = updated;
          render();
          return true;
        },
        destroy() {
          views.delete(render);
        },
      };
    };
  },
});
