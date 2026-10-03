// Inline reference to a page of an annexed document ("Annexure P-1, page 3").
// The node stores only which source document page it points at; its text is
// looked up from the annexure's current numbering, so it follows the annexure
// as documents are reordered or renumbered. The server resolves it the same
// way when exporting.
import { Node } from "@tiptap/core";

const MISSING = "[not in annexure]";
let refs = {};
const views = new Set();

function refText(attrs) {
  return (refs[attrs.docId] || {})[attrs.page] || MISSING;
}

// Replaces the known reference texts ({docId: {page: text}}) and redraws every reference.
export function setAnnexRefs(next) {
  refs = next || {};
  views.forEach((v) => v.render());
}

export const AnnexRef = Node.create({
  name: "annexRef",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    return {
      docId: { default: "" },
      page: { default: 1, parseHTML: (el) => parseInt(el.getAttribute("data-page"), 10) || 1 },
      file: { default: "" },
    };
  },

  parseHTML() {
    return [{ tag: "span[data-annex-ref]", getAttrs: (el) => ({ docId: el.getAttribute("data-annex-ref"), file: el.getAttribute("data-file") || "" }) }];
  },

  renderHTML({ node }) {
    return ["span", { "data-annex-ref": node.attrs.docId, "data-page": node.attrs.page, "data-file": node.attrs.file, class: "annex-ref" }, refText(node.attrs)];
  },

  addNodeView() {
    return ({ node }) => {
      const dom = document.createElement("span");
      dom.className = "annex-ref";
      let current = node;
      const view = {
        dom,
        render: () => {
          dom.textContent = refText(current.attrs);
          dom.classList.toggle("annex-ref-missing", dom.textContent === MISSING);
        },
        update: (updated) => {
          if (updated.type !== current.type) return false;
          current = updated;
          view.render();
          return true;
        },
        destroy: () => views.delete(view),
      };
      views.add(view);
      view.render();
      return view;
    };
  },
});
