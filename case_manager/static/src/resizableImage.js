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
import { Image as BaseImage } from "@tiptap/extension-image";

function applyWrapperAttrs(wrapper, img, attrs) {
  img.src = attrs.src || "";
  if (attrs.alt) img.alt = attrs.alt;
  else img.removeAttribute("alt");
  if (attrs.width) img.style.width = attrs.width + "px";
  else img.style.removeProperty("width");
  if (attrs.height) img.style.height = attrs.height + "px";
  else img.style.removeProperty("height");

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
      applyWrapperAttrs(wrapper, img, currentNode.attrs);

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
