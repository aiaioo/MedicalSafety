// Rich-text editor for the admin page's cause title templates: a small Tiptap
// setup (paragraphs, bold/italic/underline/strike, font, size, colour,
// alignment) with its own toolbar. Exposed as window.createTitleTemplateEditor
// so admin.js (a plain script) can mount one per template card.
//
// A template body is stored as a ProseMirror doc serialized to a JSON string;
// older templates are plain text, which is turned into one paragraph per line.
import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { TextStyleKit } from "@tiptap/extension-text-style";
import { TextAlign } from "@tiptap/extension-text-align";

const FONTS = ["Arial", "Georgia", "Times New Roman", "Garamond", "Helvetica", "Verdana", "Courier New", "Bookman Old Style", "Calibri"];
const SIZES = [8, 9, 10, 11, 12, 14, 16, 18, 24, 30, 36];
const DEFAULT_FONT = "Times New Roman";
const DEFAULT_SIZE = 14;
const ALIGNS = [["left", "Align left", "⇤"], ["center", "Center", "↔"], ["right", "Align right", "⇥"], ["justify", "Justify", "☰"]];

function textToDoc(text) {
  return {
    type: "doc",
    content: String(text).split(/\r?\n/).map((line) => (line ? { type: "paragraph", content: [{ type: "text", text: line }] } : { type: "paragraph" })),
  };
}

function parseBody(body) {
  if (typeof body === "string" && body.trimStart().startsWith("{")) {
    try {
      const doc = JSON.parse(body);
      if (doc && doc.type === "doc") return doc;
    } catch (e) { /* plain text that merely starts with a brace */ }
  }
  return textToDoc(body || "");
}

function button(label, title, onClick, extraClass) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "fmt-btn" + (extraClass ? " " + extraClass : "");
  b.title = title;
  b.innerHTML = label;
  b.addEventListener("mousedown", (e) => e.preventDefault()); // keep the editor's selection
  b.addEventListener("click", onClick);
  return b;
}

function select(title, options, onChange) {
  const s = document.createElement("select");
  s.className = "fmt-select";
  s.title = title;
  for (const [value, text, style] of options) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = text;
    if (style) o.style.cssText = style;
    s.appendChild(o);
  }
  s.addEventListener("change", () => onChange(s.value));
  return s;
}

window.createTitleTemplateEditor = function (container, body) {
  container.classList.add("title-template-rte");
  const toolbar = document.createElement("div");
  toolbar.className = "toolbar title-template-toolbar";
  const surface = document.createElement("div");
  surface.className = "title-template-surface";
  container.append(toolbar, surface);

  const editor = new Editor({
    element: surface,
    extensions: [
      StarterKit.configure({
        heading: false, bulletList: false, orderedList: false, listItem: false, blockquote: false,
        codeBlock: false, code: false, horizontalRule: false, link: false,
      }),
      TextStyleKit.configure({ backgroundColor: false, lineHeight: false }),
      TextAlign.configure({ types: ["paragraph"] }),
    ],
    content: parseBody(body),
    editorProps: { attributes: { class: "tiptap-content", style: `font-family:'${DEFAULT_FONT}';font-size:${DEFAULT_SIZE}pt` } },
    onSelectionUpdate: syncToolbar,
    onUpdate: syncToolbar,
  });

  const fontSel = select("Font", FONTS.map((f) => [f, f, `font-family:'${f}'`]), (v) => editor.chain().focus().setFontFamily(`'${v}'`).run());
  const sizeSel = select("Font size", SIZES.map((n) => [String(n), String(n)]), (v) => editor.chain().focus().setFontSize(v + "pt").run());
  sizeSel.classList.add("fmt-select-narrow");
  toolbar.append(fontSel, sizeSel);

  const markButtons = [
    ["bold", "<b>B</b>", "Bold", () => editor.chain().focus().toggleBold().run()],
    ["italic", "<i>I</i>", "Italic", () => editor.chain().focus().toggleItalic().run()],
    ["underline", "<u>U</u>", "Underline", () => editor.chain().focus().toggleUnderline().run()],
    ["strike", "<s>S</s>", "Strikethrough", () => editor.chain().focus().toggleStrike().run()],
  ].map(([name, label, title, run]) => [name, button(label, title, run)]);
  markButtons.forEach(([, b]) => toolbar.appendChild(b));

  const colour = document.createElement("input");
  colour.type = "color";
  colour.value = "#1c1c1c";
  colour.title = "Text colour";
  colour.className = "title-template-colour";
  colour.addEventListener("input", () => editor.chain().focus().setColor(colour.value).run());
  toolbar.appendChild(colour);

  const alignButtons = ALIGNS.map(([name, title, label]) => [name, button(label, title, () => editor.chain().focus().setTextAlign(name).run())]);
  alignButtons.forEach(([, b]) => toolbar.appendChild(b));

  function syncToolbar() {
    const style = editor.getAttributes("textStyle");
    const font = (style.fontFamily || "").replace(/['"]/g, "");
    fontSel.value = FONTS.includes(font) ? font : DEFAULT_FONT;
    const size = (style.fontSize || "").replace(/pt$/, "");
    sizeSel.value = SIZES.map(String).includes(size) ? size : String(DEFAULT_SIZE);
    if (style.color) colour.value = style.color;
    markButtons.forEach(([name, b]) => b.classList.toggle("active", editor.isActive(name)));
    alignButtons.forEach(([name, b]) => b.classList.toggle("active", editor.isActive({ textAlign: name })));
  }
  syncToolbar();

  return {
    // The body to save: the doc as a JSON string.
    getBody: () => JSON.stringify(editor.getJSON()),
    isEmpty: () => !editor.getText().trim(),
    setBody: (b) => { editor.commands.setContent(parseBody(b)); },
  };
};
