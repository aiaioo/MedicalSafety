// Rich-text editor for the admin page's cause title templates: a small Tiptap
// setup (paragraphs, bold/italic/underline, font, size, colour,
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
const svg = (d) => `<svg viewBox="0 0 16 14" width="16" height="14" fill="currentColor">${d}</svg>`;
const rect = (x, y, w) => `<rect x="${x}" y="${y}" width="${w}" height="2"/>`;
const ALIGNS = [
  ["left", "Align left", svg(rect(0, 0, 16) + rect(0, 4, 10) + rect(0, 8, 16) + rect(0, 12, 10))],
  ["center", "Align center", svg(rect(0, 0, 16) + rect(3, 4, 10) + rect(0, 8, 16) + rect(3, 12, 10))],
  ["right", "Align right", svg(rect(0, 0, 16) + rect(6, 4, 10) + rect(0, 8, 16) + rect(6, 12, 10))],
  ["justify", "Justify", svg(rect(0, 0, 16) + rect(0, 4, 16) + rect(0, 8, 16) + rect(0, 12, 16))],
];
const UNDO = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7H10a3.5 3.5 0 0 1 0 7H6.5"/><path d="M6 4 3 7l3 3"/></svg>';
const REDO = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12.5 7H6a3.5 3.5 0 0 0 0 7h3.5"/><path d="M10 4l3 3-3 3"/></svg>';
const DEFAULT_COLOR = "#1c1c1c";

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
  toolbar.className = "toolbar doc-toolbar title-template-toolbar";
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

  const sep = () => { const e = document.createElement("span"); e.className = "toolbar-sep"; return e; };
  const fontSel = select("Font", FONTS.map((f) => [f, f, `font-family:'${f}'`]), (v) => editor.chain().focus().setFontFamily(`'${v}'`).run());
  const sizeSel = select("Font size", SIZES.map((n) => [String(n), String(n)]), (v) => editor.chain().focus().setFontSize(v + "pt").run());
  sizeSel.classList.add("fmt-select-narrow");
  const markButtons = [
    ["bold", "<b>B</b>", "Bold", () => editor.chain().focus().toggleBold().run()],
    ["italic", "<i>I</i>", "Italic", () => editor.chain().focus().toggleItalic().run()],
    ["underline", "<u>U</u>", "Underline", () => editor.chain().focus().toggleUnderline().run()],
  ].map(([name, label, title, run]) => [name, button(label, title, run)]);

  // Text colour: the same swatch button + palette popover as the articles editor.
  const colourWrap = document.createElement("div");
  colourWrap.className = "color-pop-wrap";
  colourWrap.innerHTML = `<button type="button" class="color-btn" title="Text color" aria-haspopup="true" aria-expanded="false"><span>A</span><span class="swatch"></span></button>
    <div class="color-popover" hidden><div class="color-palette" role="radiogroup" aria-label="Text colour"></div></div>`;
  const colourBtn = colourWrap.querySelector(".color-btn");
  const colourSwatch = colourWrap.querySelector(".swatch");
  const colourPopover = colourWrap.querySelector(".color-popover");
  const setPopoverOpen = (open) => { colourPopover.hidden = !open; colourBtn.setAttribute("aria-expanded", open); };
  const selectColour = window.PageNumberPalette.mount(colourWrap.querySelector(".color-palette"), (c) => {
    colourSwatch.style.background = c;
    selectColour(c);
    setPopoverOpen(false);
    editor.chain().focus().setColor(c).run();
  });
  colourBtn.addEventListener("mousedown", (e) => e.preventDefault());
  colourBtn.addEventListener("click", () => setPopoverOpen(colourPopover.hidden));
  document.addEventListener("mousedown", (e) => {
    if (!colourPopover.hidden && !colourWrap.contains(e.target)) setPopoverOpen(false);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setPopoverOpen(false); });

  const alignButtons = ALIGNS.map(([name, title, icon]) => [name, button(icon, title, () => editor.chain().focus().setTextAlign(name).run())]);
  toolbar.append(
    button(UNDO, "Undo", () => editor.chain().focus().undo().run()),
    button(REDO, "Redo", () => editor.chain().focus().redo().run()),
    sep(), fontSel, sizeSel, sep(),
    ...markButtons.map(([, b]) => b), colourWrap, sep(),
    ...alignButtons.map(([, b]) => b),
  );

  function syncToolbar() {
    const style = editor.getAttributes("textStyle");
    const font = (style.fontFamily || "").replace(/['"]/g, "");
    fontSel.value = FONTS.includes(font) ? font : DEFAULT_FONT;
    const size = (style.fontSize || "").replace(/pt$/, "");
    sizeSel.value = SIZES.map(String).includes(size) ? size : String(DEFAULT_SIZE);
    const color = style.color || DEFAULT_COLOR;
    colourSwatch.style.background = color;
    selectColour(color);
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
