// Per-paragraph vertical spacing. The "Line spacing" toolbar button selects
// the standard (roomier) gap between paragraphs; deselected, a paragraph
// gets no extra gap, so its lines sit as tight as lines within a paragraph.
// Stored as a boolean `tight` attribute (default false = standard spacing,
// so existing documents look unchanged); exports read it in app.py.
import { Extension } from "@tiptap/core";

export const ParagraphSpacing = Extension.create({
  name: "paragraphSpacing",
  addGlobalAttributes() {
    return [{
      types: ["paragraph"],
      attributes: {
        tight: {
          default: false,
          parseHTML: (el) => el.classList.contains("tight-p"),
          renderHTML: (attrs) => (attrs.tight ? { class: "tight-p" } : {}),
        },
      },
    }];
  },
  addCommands() {
    return {
      // Toggles the standard spacing on every paragraph in the selection.
      toggleParagraphSpacing: () => ({ state, tr, dispatch }) => {
        const { from, to } = state.selection;
        const paras = [];
        state.doc.nodesBetween(from, to, (node, pos) => {
          if (node.type.name === "paragraph") paras.push([node, pos]);
        });
        if (!paras.length) return false;
        const makeTight = paras.every(([n]) => !n.attrs.tight);
        if (dispatch) {
          paras.forEach(([n, pos]) => tr.setNodeMarkup(pos, undefined, { ...n.attrs, tight: makeTight }));
        }
        return true;
      },
    };
  },
});

// True when every paragraph in the selection has the standard spacing.
export function hasStandardSpacing(editor) {
  const { from, to } = editor.state.selection;
  let any = false;
  let all = true;
  editor.state.doc.nodesBetween(from, to, (node) => {
    if (node.type.name !== "paragraph") return;
    any = true;
    if (node.attrs.tight) all = false;
  });
  return any && all;
}

// Marks a ProseMirror doc's paragraphs as tight (used for the cause title).
export function tightenParagraphs(nodes) {
  return nodes.map((n) => {
    const out = n.type === "paragraph" ? { ...n, attrs: { ...(n.attrs || {}), tight: true } } : { ...n };
    if (out.content) out.content = tightenParagraphs(out.content);
    return out;
  });
}
