// Document editor, now built on Tiptap/ProseMirror instead of a hand-rolled
// contenteditable + execCommand editor (see MULTILEVEL_SECTION_NUMBERING.md
// and EDITOR_DESIGN.md for why: no document schema meant every list-editing
// edge case -- Enter/Tab around list items, multilevel section numbers --
// had to be hand-built against raw DOM mutations).
//
// Known gap versus the old editor, deliberately deferred rather than
// dropped silently: pasting a table is flattened -- there's no Table
// extension yet, and the exporters have no table-rendering path.
import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { TextStyleKit } from "@tiptap/extension-text-style";
import { Highlight } from "@tiptap/extension-highlight";
import { TextAlign } from "@tiptap/extension-text-align";
import { ResizableImage, setSelectedImageAlign, isImageSelected } from "./resizableImage.js";
import {
  OrderedList,
  ListItem,
  ListMarkers,
  BlockIndent,
  LIST_MAX_LEVELS,
  LIST_PRESETS,
  applyListTemplate,
  currentListTemplate,
  findRootOrderedList,
  restartNumberingAt,
  currentNumberAt,
  setNumberingValueAt,
  continueFromPreviousListAt,
  exitEmptyListItemOnEnter,
} from "./listNumbering.js";
import { Pagination, repaginate } from "./pagination.js";

(function () {
  const appEl = document.getElementById("docApp");
  const reportId = appEl.dataset.report;
  const reportsUrl = appEl.dataset.reportsUrl;
  const reportUrl = appEl.dataset.reportUrl;
  const exportUrl = appEl.dataset.exportUrl;
  const exportDocxUrl = appEl.dataset.exportDocxUrl;
  const preselectSource = appEl.dataset.preselectSource;
  const preselectType = appEl.dataset.preselectType || "pdf";

  const editorEl = document.getElementById("editor");
  const editorPageWrap = document.getElementById("editorPageWrap");
  const marginGuidesEl = document.getElementById("marginGuides");
  const titleInput = document.getElementById("titleInput");
  const saveBtn = document.getElementById("saveBtn");
  const saveStatusEl = document.getElementById("saveStatus");
  const snippetListEl = document.getElementById("reportSnippetList");
  const sourceSelect = document.getElementById("sourceSelect");
  const annotateLink = document.getElementById("annotateLink");
  const uploadSourceMenuBtn = document.getElementById("uploadSourceMenuBtn");
  const uploadSourceInput = document.getElementById("uploadSourceInput");
  const uploadSourceStatus = document.getElementById("uploadSourceStatus");

  const fileMenuBtn = document.getElementById("fileMenuBtn");
  const fileMenuDropdown = document.getElementById("fileMenuDropdown");
  const newDocBtn = document.getElementById("newDocBtn");
  const openDocBtn = document.getElementById("openDocBtn");
  const pageSetupBtn = document.getElementById("pageSetupBtn");
  const downloadPdfBtn = document.getElementById("downloadPdfBtn");
  const downloadDocxBtn = document.getElementById("downloadDocxBtn");

  const pageSetupModal = document.getElementById("pageSetupModal");
  const marginLeftInput = document.getElementById("marginLeftInput");
  const marginRightInput = document.getElementById("marginRightInput");
  const marginHeaderInput = document.getElementById("marginHeaderInput");
  const marginFooterInput = document.getElementById("marginFooterInput");
  const pageNumberPositionInput = document.getElementById("pageNumberPositionInput");
  const pageNumberSkipInput = document.getElementById("pageNumberSkipInput");
  const pageSetupError = document.getElementById("pageSetupError");
  const pageSetupCancel = document.getElementById("pageSetupCancel");
  const pageSetupApply = document.getElementById("pageSetupApply");

  const DEFAULT_MARGINS = { left: 36, right: 36, header: 46, footer: 46 };
  let margins = { ...DEFAULT_MARGINS };

  const PAGE_NUMBER_POSITIONS = new Set([
    "top-left", "top-center", "top-right",
    "bottom-left", "bottom-center", "bottom-right",
    "none",
  ]);
  const DEFAULT_PAGE_NUMBERS = { position: "top-center", skip: 0 };
  let pageNumbers = { ...DEFAULT_PAGE_NUMBERS };

  const newDocModal = document.getElementById("newDocModal");
  const newDocName = document.getElementById("newDocName");
  const newDocSource = document.getElementById("newDocSource");
  const newDocError = document.getElementById("newDocError");
  const newDocCancel = document.getElementById("newDocCancel");
  const newDocCreate = document.getElementById("newDocCreate");

  const openDocModal = document.getElementById("openDocModal");
  const openDocList = document.getElementById("openDocList");
  const openDocCancel = document.getElementById("openDocCancel");

  const reportListLanding = document.getElementById("reportListLanding");
  const landingEmptyHint = document.getElementById("landingEmptyHint");

  let dirty = false;
  let saving = false;
  let saveAgainAfter = false;
  let autosaveTimer = null;
  let loadingReport = false;

  function setStatus(text, isError) {
    saveStatusEl.textContent = text || "";
    saveStatusEl.style.color = isError ? "#c0392b" : "#8a92a5";
  }

  function fmtDate(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // ---------------------------------------------------------------------
  // File menu
  // ---------------------------------------------------------------------
  fileMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    fileMenuDropdown.classList.toggle("open");
  });
  document.addEventListener("click", () => fileMenuDropdown.classList.remove("open"));

  function openModal(el) {
    el.classList.add("open");
  }
  function closeModal(el) {
    el.classList.remove("open");
  }

  newDocBtn.addEventListener("click", () => {
    newDocName.value = "";
    newDocError.style.display = "none";
    newDocSource.value = sourceSelect && sourceSelect.value ? sourceSelect.value : "";
    openModal(newDocModal);
    setTimeout(() => newDocName.focus(), 0);
  });
  newDocCancel.addEventListener("click", () => closeModal(newDocModal));
  newDocModal.addEventListener("click", (e) => {
    if (e.target === newDocModal) closeModal(newDocModal);
  });

  async function createDocument() {
    const name = newDocName.value.trim();
    if (!name) {
      newDocError.textContent = "Please enter a name for the document.";
      newDocError.style.display = "block";
      return;
    }
    let source_doc = "";
    let source_type = "pdf";
    if (newDocSource.value) {
      [source_doc, source_type] = newDocSource.value.split("|");
    }
    newDocCreate.disabled = true;
    try {
      const res = await fetch(reportsUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, source_doc, source_type }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      window.location.href = `/document?report=${encodeURIComponent(data.id)}`;
    } catch (e) {
      newDocError.textContent = "Could not create document: " + e.message;
      newDocError.style.display = "block";
      newDocCreate.disabled = false;
    }
  }
  newDocCreate.addEventListener("click", createDocument);
  newDocName.addEventListener("keydown", (e) => {
    if (e.key === "Enter") createDocument();
  });

  function renderReportCard(r, container) {
    const card = document.createElement("a");
    card.className = "report-card";
    card.href = `/document?report=${encodeURIComponent(r.id)}`;
    const sourceLabel = r.source_doc ? `${r.source_doc} (${r.source_type})` : "No source document";
    card.innerHTML = `
      <div class="report-card-name">${escapeHtml(r.name || r.id)}</div>
      <div class="report-card-meta">${escapeHtml(sourceLabel)}</div>
      <div class="report-card-meta">Updated ${escapeHtml(fmtDate(r.updated_at))}</div>`;
    container.appendChild(card);
  }

  async function fetchReports() {
    const res = await fetch(reportsUrl);
    return res.json();
  }

  openDocBtn.addEventListener("click", async () => {
    openDocList.innerHTML = '<p class="empty">Loading&hellip;</p>';
    openModal(openDocModal);
    try {
      const list = await fetchReports();
      openDocList.innerHTML = "";
      if (!list.length) {
        openDocList.innerHTML = '<p class="empty">No documents yet.</p>';
        return;
      }
      for (const r of list) renderReportCard(r, openDocList);
    } catch (e) {
      openDocList.innerHTML = '<p class="empty">Failed to load documents.</p>';
    }
  });
  openDocCancel.addEventListener("click", () => closeModal(openDocModal));
  openDocModal.addEventListener("click", (e) => {
    if (e.target === openDocModal) closeModal(openDocModal);
  });

  async function loadLanding() {
    if (!reportListLanding) return;
    try {
      const list = await fetchReports();
      reportListLanding.innerHTML = "";
      landingEmptyHint.style.display = list.length ? "none" : "block";
      for (const r of list) renderReportCard(r, reportListLanding);
    } catch (e) {
      console.error(e);
    }
  }

  if (preselectSource) {
    newDocSource.value = `${preselectSource}|${preselectType}`;
    openModal(newDocModal);
    setTimeout(() => newDocName.focus(), 0);
  }

  if (uploadSourceMenuBtn) {
    uploadSourceMenuBtn.addEventListener("click", () => uploadSourceInput.click());
  }

  if (uploadSourceInput) {
    uploadSourceInput.addEventListener("change", async () => {
      const file = uploadSourceInput.files[0];
      if (!file) return;

      uploadSourceStatus.textContent = "Uploading…";
      uploadSourceStatus.classList.remove("error");

      try {
        const body = new FormData();
        body.append("file", file);
        const res = await fetch("/api/documents/upload", { method: "POST", body });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Upload failed");

        const combo = `${data.id}|${data.type}`;
        const label = `${data.id} (${data.type})`;
        for (const select of [sourceSelect, newDocSource]) {
          if (!select) continue;
          const opt = document.createElement("option");
          opt.value = combo;
          opt.textContent = label;
          select.appendChild(opt);
        }

        if (reportId) {
          sourceSelect.disabled = false;
          sourceSelect.value = combo;
          updateAnnotateLink();
          loadSnippets();
          markDirty();
        }

        uploadSourceStatus.textContent = `Uploaded as "${data.id}".`;
      } catch (err) {
        uploadSourceStatus.textContent = err.message;
        uploadSourceStatus.classList.add("error");
      } finally {
        uploadSourceInput.value = "";
      }
    });
  }

  if (!reportId) {
    loadLanding();
    return; // nothing else to wire up until a document is open
  }

  // ---------------------------------------------------------------------
  // Page setup (margins) -- CSS padding on the editor only; no repagination
  // (see module comment: pagination is deferred).
  // ---------------------------------------------------------------------
  const PT_TO_PX = 96 / 72;

  function normalizeMargins(raw) {
    const out = {};
    for (const key of Object.keys(DEFAULT_MARGINS)) {
      const v = raw && typeof raw === "object" ? Number(raw[key]) : NaN;
      out[key] = Number.isFinite(v) && v >= 0 && v <= 200 ? v : DEFAULT_MARGINS[key];
    }
    return out;
  }

  function normalizePageNumbers(raw) {
    const position = raw && PAGE_NUMBER_POSITIONS.has(raw.position) ? raw.position : DEFAULT_PAGE_NUMBERS.position;
    const skipRaw = raw && typeof raw === "object" ? Number(raw.skip) : NaN;
    const skip = Number.isInteger(skipRaw) && skipRaw >= 0 && skipRaw <= 50 ? skipRaw : DEFAULT_PAGE_NUMBERS.skip;
    return { position, skip };
  }

  function marginsPx() {
    return {
      left: margins.left * PT_TO_PX,
      right: margins.right * PT_TO_PX,
      header: margins.header * PT_TO_PX,
      footer: margins.footer * PT_TO_PX,
    };
  }

  function applyMarginsToCss() {
    const px = marginsPx();
    editorEl.style.setProperty("--m-left", px.left + "px");
    editorEl.style.setProperty("--m-right", px.right + "px");
    editorEl.style.setProperty("--m-header", px.header + "px");
    editorEl.style.setProperty("--m-footer", px.footer + "px");
    if (marginGuidesEl) {
      marginGuidesEl.style.setProperty("--mg-left", px.left + "px");
      marginGuidesEl.style.setProperty("--mg-right", px.right + "px");
      marginGuidesEl.style.setProperty("--mg-top", px.header + "px");
      marginGuidesEl.style.setProperty("--mg-bottom", px.footer + "px");
    }
    if (editorReady) repaginate(editor);
  }

  // Purely visual dashed-outline guides showing the printable area of each
  // simulated page, one .page-rect per page -- lives in the #marginGuides
  // overlay (a sibling of #editor, not inside it) so it can never leak into
  // saved content. Page rects are measured from the actual page-break
  // widgets the Pagination plugin just rendered (see pagination.js), not
  // computed as uniform page-height slices, since a page that doesn't fill
  // out to a full sheet is common (see fillerBefore there).
  function renderMarginGuides() {
    if (!marginGuidesEl) return;
    const editorRect = editorEl.getBoundingClientRect();
    const breaks = Array.from(editorEl.querySelectorAll(".tiptap-content .page-break"));

    const frag = document.createDocumentFragment();
    let top = 0;
    for (let i = 0; i <= breaks.length; i++) {
      const bottom = i < breaks.length ? breaks[i].getBoundingClientRect().top - editorRect.top : editorRect.height;
      const rect = document.createElement("div");
      rect.className = "page-rect";
      rect.style.top = top + "px";
      rect.style.height = Math.max(0, bottom - top) + "px";
      if (pageNumbers.position !== "none" && i >= pageNumbers.skip) {
        const label = document.createElement("div");
        label.className = `page-number-label page-number-${pageNumbers.position}`;
        label.textContent = String(i - pageNumbers.skip + 1);
        rect.appendChild(label);
      }
      frag.appendChild(rect);
      if (i < breaks.length) top = breaks[i].getBoundingClientRect().bottom - editorRect.top;
    }
    marginGuidesEl.replaceChildren(frag);
  }

  function clampMargin(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    return Math.min(200, Math.max(0, n));
  }

  function clampPageNumberSkip(v) {
    const n = Number(v);
    if (!Number.isInteger(n)) return null;
    return Math.min(50, Math.max(0, n));
  }

  pageSetupBtn.addEventListener("click", () => {
    marginLeftInput.value = margins.left;
    marginRightInput.value = margins.right;
    marginHeaderInput.value = margins.header;
    marginFooterInput.value = margins.footer;
    pageNumberPositionInput.value = pageNumbers.position;
    pageNumberSkipInput.value = pageNumbers.skip;
    pageSetupError.style.display = "none";
    openModal(pageSetupModal);
  });
  pageSetupCancel.addEventListener("click", () => closeModal(pageSetupModal));
  pageSetupModal.addEventListener("click", (e) => {
    if (e.target === pageSetupModal) closeModal(pageSetupModal);
  });
  pageSetupApply.addEventListener("click", () => {
    const left = clampMargin(marginLeftInput.value);
    const right = clampMargin(marginRightInput.value);
    const header = clampMargin(marginHeaderInput.value);
    const footer = clampMargin(marginFooterInput.value);
    if ([left, right, header, footer].some((v) => v === null)) {
      pageSetupError.textContent = "Enter margins between 0 and 200pt.";
      pageSetupError.style.display = "block";
      return;
    }
    const skip = clampPageNumberSkip(pageNumberSkipInput.value);
    if (skip === null || !PAGE_NUMBER_POSITIONS.has(pageNumberPositionInput.value)) {
      pageSetupError.textContent = "Enter a page count between 0 and 50 to skip.";
      pageSetupError.style.display = "block";
      return;
    }
    margins = { left, right, header, footer };
    pageNumbers = { position: pageNumberPositionInput.value, skip };
    applyMarginsToCss();
    markDirty();
    closeModal(pageSetupModal);
  });

  // ---------------------------------------------------------------------
  // Tiptap editor
  // ---------------------------------------------------------------------
  let editorReady = false;
  const editor = new Editor({
    element: editorEl,
    extensions: [
      StarterKit.configure({ orderedList: false, listItem: false }),
      OrderedList,
      ListItem,
      ListMarkers,
      BlockIndent,
      TextStyleKit.configure({ backgroundColor: false, lineHeight: false }),
      Highlight.configure({ multicolor: true }),
      TextAlign.configure({ types: ["heading", "paragraph"] }),
      ResizableImage,
      Pagination.configure({ getMargins: () => margins, onPaginate: renderMarginGuides }),
    ],
    content: "",
    editorProps: {
      attributes: { class: "tiptap-content" },
    },
    onUpdate: () => markDirty(),
    onCreate: () => {
      applyMarginsToCss();
    },
  });
  editorReady = true;

  editor.view.dom.setAttribute("data-placeholder", "Start writing your document. Click a snippet on the right to insert it here.");

  // A window resize or an async image load changes layout height without
  // producing a ProseMirror transaction of its own, so the Pagination
  // plugin (which only re-measures on `view.update()`) needs a manual
  // nudge -- dispatching a no-op transaction is enough to trigger it.
  window.addEventListener("resize", () => repaginate(editor));
  editorEl.addEventListener(
    "load",
    (e) => {
      if (e.target.tagName === "IMG") repaginate(editor);
    },
    true
  );
  document.querySelector(".editor-container").addEventListener("scroll", () => {
    if (marginGuidesEl) renderMarginGuides();
  });

  // .page-break is contentEditable="false", but that alone doesn't stop a
  // click from landing a caret in the nearest real text -- preventing the
  // mousedown's default keeps the inter-page gap fully inert to clicks.
  editorEl.addEventListener("mousedown", (e) => {
    if (e.target.closest && e.target.closest(".page-break")) e.preventDefault();
  });

  // Tab/Shift+Tab: sink/lift within a list, else nudge the current block's
  // left margin (see BlockIndent). Enter on an empty list item exits the
  // list outright, regardless of nesting depth (see listNumbering.js).
  editorEl.addEventListener(
    "keydown",
    (e) => {
      if (e.key === "Tab") {
        e.preventDefault();
        const dir = e.shiftKey ? -1 : 1;
        if (dir === 1 && editor.can().sinkListItem("listItem")) editor.chain().focus().sinkListItem("listItem").run();
        else if (dir === -1 && editor.can().liftListItem("listItem")) editor.chain().focus().liftListItem("listItem").run();
        else editor.chain().focus().indentBlock(dir).run();
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        if (exitEmptyListItemOnEnter(editor)) e.preventDefault();
      }
    },
    true
  );

  // -----------------------------------------------------------------
  // Right-click on a numbered-list item: restart / set value / continue.
  // -----------------------------------------------------------------
  const listContextMenu = document.createElement("div");
  listContextMenu.className = "context-menu";
  listContextMenu.innerHTML = [
    '<button type="button" data-action="restart">Restart numbering (start at 1)</button>',
    '<button type="button" data-action="setvalue">Set numbering value&hellip;</button>',
    '<button type="button" data-action="continue">Continue from previous list</button>',
  ].join("");
  document.body.appendChild(listContextMenu);
  let listContextPos = null;

  function hideListContextMenu() {
    listContextMenu.classList.remove("open");
    listContextPos = null;
  }
  editorEl.addEventListener("contextmenu", (e) => {
    const li = e.target.closest && e.target.closest("li");
    if (!li || !editorEl.contains(li)) return;
    const posInfo = editor.view.posAtDOM(li, 0);
    if (posInfo == null) return;
    if (!findRootOrderedList(editor.state.doc, posInfo)) return;
    e.preventDefault();
    listContextPos = posInfo;
    listContextMenu.style.left = e.clientX + "px";
    listContextMenu.style.top = e.clientY + "px";
    listContextMenu.classList.add("open");
  });
  document.addEventListener("click", (e) => {
    if (!listContextMenu.contains(e.target)) hideListContextMenu();
  });
  window.addEventListener("blur", hideListContextMenu);
  window.addEventListener("resize", hideListContextMenu);

  listContextMenu.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn || listContextPos == null) return;
    if (btn.dataset.action === "restart") {
      restartNumberingAt(editor, listContextPos);
    } else if (btn.dataset.action === "setvalue") {
      const current = currentNumberAt(editor, listContextPos) || 1;
      const input = window.prompt("Set this item's number to:", String(current));
      const n = input === null ? NaN : parseInt(input, 10);
      if (Number.isFinite(n) && n > 0) setNumberingValueAt(editor, listContextPos, n);
    } else if (btn.dataset.action === "continue") {
      continueFromPreviousListAt(editor, listContextPos);
    }
    markDirty();
    hideListContextMenu();
  });

  // -----------------------------------------------------------------
  // Numbering-style toolbar: split button + preset dropdown + custom-levels
  // modal.
  // -----------------------------------------------------------------
  let defaultListPreset = "legal-numeric";
  const numListStyleBtn = document.getElementById("numListStyleBtn");
  const numListStyleDropdown = document.getElementById("numListStyleDropdown");
  const customLevelsBtn = document.getElementById("customLevelsBtn");
  const listLevelsModal = document.getElementById("listLevelsModal");
  const listLevelsGrid = document.getElementById("listLevelsGrid");
  const listCascadeInput = document.getElementById("listCascadeInput");
  const listLevelsCancel = document.getElementById("listLevelsCancel");
  const listLevelsApply = document.getElementById("listLevelsApply");

  if (numListStyleBtn) {
    numListStyleBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      numListStyleDropdown.classList.toggle("open");
    });
    document.addEventListener("click", () => numListStyleDropdown.classList.remove("open"));
  }

  if (numListStyleDropdown) {
    numListStyleDropdown.querySelectorAll("button[data-preset]").forEach((btn) => {
      btn.addEventListener("click", () => {
        defaultListPreset = btn.dataset.preset;
        applyListTemplate(editor, defaultListPreset);
        markDirty();
        numListStyleDropdown.classList.remove("open");
      });
    });
  }

  function populateListLevelsGrid(spec) {
    listCascadeInput.checked = !!spec.cascade;
    listLevelsGrid.innerHTML = "";
    for (let i = 1; i <= LIST_MAX_LEVELS; i++) {
      const lvl = spec.levels[i - 1] || { type: "decimal", wrap: "period" };
      const label = document.createElement("span");
      label.className = "list-level-label";
      label.textContent = `Level ${i}`;
      const typeSelect = document.createElement("select");
      typeSelect.dataset.role = "type";
      [
        ["decimal", "1, 2, 3"],
        ["alpha", "a, b, c"],
        ["upalpha", "A, B, C"],
        ["roman", "i, ii, iii"],
        ["uproman", "I, II, III"],
      ].forEach(([val, text]) => {
        const opt = document.createElement("option");
        opt.value = val;
        opt.textContent = text;
        if (val === lvl.type) opt.selected = true;
        typeSelect.appendChild(opt);
      });
      const wrapSelect = document.createElement("select");
      wrapSelect.dataset.role = "wrap";
      [
        ["none", "1"],
        ["period", "1."],
        ["paren", "(1)"],
        ["trail", "1)"],
      ].forEach(([val, text]) => {
        const opt = document.createElement("option");
        opt.value = val;
        opt.textContent = text;
        if (val === lvl.wrap) opt.selected = true;
        wrapSelect.appendChild(opt);
      });
      listLevelsGrid.appendChild(label);
      listLevelsGrid.appendChild(typeSelect);
      listLevelsGrid.appendChild(wrapSelect);
    }
  }

  function readListLevelsGrid() {
    const rows = [];
    const types = listLevelsGrid.querySelectorAll('select[data-role="type"]');
    const wraps = listLevelsGrid.querySelectorAll('select[data-role="wrap"]');
    for (let i = 0; i < LIST_MAX_LEVELS; i++) rows.push({ type: types[i].value, wrap: wraps[i].value });
    return { cascade: listCascadeInput.checked, levels: rows };
  }

  if (customLevelsBtn) {
    customLevelsBtn.addEventListener("click", () => {
      numListStyleDropdown.classList.remove("open");
      const spec = currentListTemplate(editor) || LIST_PRESETS[defaultListPreset];
      populateListLevelsGrid(spec);
      openModal(listLevelsModal);
    });
    listLevelsCancel.addEventListener("click", () => closeModal(listLevelsModal));
    listLevelsModal.addEventListener("click", (e) => {
      if (e.target === listLevelsModal) closeModal(listLevelsModal);
    });
    listLevelsApply.addEventListener("click", () => {
      applyListTemplate(editor, readListLevelsGrid());
      markDirty();
      closeModal(listLevelsModal);
    });
  }

  document.querySelector('.fmt-btn[data-cmd="insertOrderedList"]').addEventListener("click", () => {
    editor.chain().focus().toggleOrderedList().run();
    const root = findRootOrderedList(editor.state.doc, editor.state.selection.from);
    if (root && !root.node.attrs.numLevels) applyListTemplate(editor, defaultListPreset);
  });
  document.querySelector('.fmt-btn[data-cmd="insertUnorderedList"]').addEventListener("click", () => {
    editor.chain().focus().toggleBulletList().run();
  });

  // ---------------------------------------------------------------------
  // Formatting toolbar
  // ---------------------------------------------------------------------
  const TEXT_ALIGNS = { justifyLeft: "left", justifyCenter: "center", justifyRight: "right", justifyFull: "justify" };
  const MARK_TOGGLES = { bold: "bold", italic: "italic", underline: "underline" };

  document.querySelectorAll(".fmt-btn[data-cmd]").forEach((btn) => {
    const cmd = btn.dataset.cmd;
    if (cmd === "insertOrderedList" || cmd === "insertUnorderedList") return; // wired above
    btn.addEventListener("click", () => {
      if (TEXT_ALIGNS[cmd] && isImageSelected(editor)) {
        setSelectedImageAlign(editor, TEXT_ALIGNS[cmd] === "justify" ? "left" : TEXT_ALIGNS[cmd]);
      } else if (TEXT_ALIGNS[cmd]) {
        editor.chain().focus().setTextAlign(TEXT_ALIGNS[cmd]).run();
      } else if (MARK_TOGGLES[cmd]) {
        editor.chain().focus().toggleMark(MARK_TOGGLES[cmd]).run();
      }
    });
  });

  const fontFamilySelect = document.getElementById("fontFamilySelect");
  fontFamilySelect.addEventListener("change", () => {
    editor.chain().focus().setFontFamily(fontFamilySelect.value).run();
  });

  const fontSizeSelect = document.getElementById("fontSizeSelect");
  fontSizeSelect.addEventListener("change", () => {
    editor.chain().focus().setFontSize(fontSizeSelect.value + "pt").run();
  });

  // Every textblock spanned by the current selection, as a single
  // {start, end} content range -- used below to strip a leftover explicit
  // font-size override across a whole paragraph/heading, not just whatever
  // substring happens to be selected (often nothing, if the caret is just
  // sitting in the block with no selection at all).
  function selectedBlockContentRange(state) {
    const { $from, $to } = state.selection;
    let start = null;
    let end = null;
    state.doc.nodesBetween($from.pos, $to.pos, (node, pos) => {
      if (!node.isTextblock) return;
      const s = pos + 1;
      const e = pos + node.nodeSize - 1;
      start = start === null ? s : Math.min(start, s);
      end = end === null ? e : Math.max(end, e);
    });
    return start === null ? null : { from: start, to: end };
  }

  const blockFormatSelect = document.getElementById("blockFormatSelect");
  blockFormatSelect.addEventListener("change", () => {
    const val = blockFormatSelect.value;
    const { from: cursorFrom, to: cursorTo } = editor.state.selection;
    // setHeading/setParagraph only change the block's node type, never the
    // doc's size, so a content range measured beforehand still lines up
    // with the same text afterward.
    const blockRange = selectedBlockContentRange(editor.state);

    let chain = editor.chain().focus();
    chain = val === "P" ? chain.setParagraph() : chain.setHeading({ level: Number(val.slice(1)) });
    if (blockRange) {
      // A character-level font-size override (set via the font-size
      // dropdown) otherwise keeps overriding the heading/paragraph's own
      // size forever, even though nothing about it still looks like the
      // newly chosen style -- so applying a block format also clears any
      // such override across the whole affected block(s).
      chain = chain.setTextSelection(blockRange).unsetFontSize().setTextSelection({ from: cursorFrom, to: cursorTo });
    }
    chain.run();
  });

  const textColorInput = document.getElementById("textColorInput");
  const textColorSwatch = document.getElementById("textColorSwatch");
  textColorSwatch.style.background = textColorInput.value;
  textColorInput.addEventListener("input", () => {
    editor.chain().focus().setColor(textColorInput.value).run();
    textColorSwatch.style.background = textColorInput.value;
  });

  const highlightColorInput = document.getElementById("highlightColorInput");
  const highlightColorSwatch = document.getElementById("highlightColorSwatch");
  highlightColorSwatch.style.background = highlightColorInput.value;
  highlightColorInput.addEventListener("input", () => {
    highlightColorSwatch.style.background = highlightColorInput.value;
    editor.chain().focus().setHighlight({ color: highlightColorInput.value }).run();
  });
  document.getElementById("clearHighlightBtn").addEventListener("click", () => {
    editor.chain().focus().unsetHighlight().run();
  });

  // Toolbar dropdowns/buttons otherwise only ever *write* to the editor (on
  // "change"/"click") and never read back, so they'd keep showing whatever
  // was last picked instead of the formatting under the cursor. Re-derive
  // every control from the current selection on every transaction (typing,
  // clicking, arrow-key movement -- ProseMirror fires "transaction" for a
  // selection-only change too, not just a doc change).
  const DEFAULT_FONT_FAMILY = fontFamilySelect.options[0].value;
  const DEFAULT_FONT_SIZE = "11";
  const DEFAULT_TEXT_COLOR = textColorInput.value;

  function syncToolbarToSelection() {
    const styleAttrs = editor.getAttributes("textStyle");
    fontFamilySelect.value = styleAttrs.fontFamily || DEFAULT_FONT_FAMILY;

    const size = styleAttrs.fontSize ? styleAttrs.fontSize.replace(/pt$/, "") : DEFAULT_FONT_SIZE;
    if ([...fontSizeSelect.options].some((opt) => opt.value === size)) fontSizeSelect.value = size;

    const color = styleAttrs.color || DEFAULT_TEXT_COLOR;
    textColorInput.value = color;
    textColorSwatch.style.background = color;

    const highlightColor = editor.getAttributes("highlight").color;
    if (highlightColor) {
      highlightColorInput.value = highlightColor;
      highlightColorSwatch.style.background = highlightColor;
      highlightColorSwatch.style.outline = "";
    } else {
      highlightColorSwatch.style.background = "transparent";
      highlightColorSwatch.style.outline = "1px solid #b8bfcf";
    }

    let headingLevel = null;
    for (let level = 1; level <= 3; level++) {
      if (editor.isActive("heading", { level })) headingLevel = level;
    }
    blockFormatSelect.value = headingLevel ? `H${headingLevel}` : "P";

    document.querySelectorAll(".fmt-btn[data-cmd]").forEach((btn) => {
      const cmd = btn.dataset.cmd;
      let active;
      if (MARK_TOGGLES[cmd]) active = editor.isActive(MARK_TOGGLES[cmd]);
      else if (TEXT_ALIGNS[cmd]) active = editor.isActive({ textAlign: TEXT_ALIGNS[cmd] });
      else if (cmd === "insertUnorderedList") active = editor.isActive("bulletList");
      else if (cmd === "insertOrderedList") active = editor.isActive("orderedList");
      else return;
      btn.classList.toggle("active", active);
    });
  }
  editor.on("transaction", syncToolbarToSelection);
  syncToolbarToSelection();

  // ---------------------------------------------------------------------
  // Autosave / save / export
  // ---------------------------------------------------------------------
  function markDirty() {
    if (loadingReport) return;
    dirty = true;
    setStatus("Saving…");
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => {
      saveReport().catch((e) => setStatus("Save failed: " + e.message, true));
    }, 1200);
  }

  function currentSource() {
    if (!sourceSelect.value) return { source_doc: "", source_type: "pdf" };
    const [source_doc, source_type] = sourceSelect.value.split("|");
    return { source_doc, source_type };
  }

  async function saveReport() {
    if (saving) {
      saveAgainAfter = true;
      return;
    }
    saving = true;
    if (autosaveTimer) {
      clearTimeout(autosaveTimer);
      autosaveTimer = null;
    }
    const { source_doc, source_type } = currentSource();
    const payload = {
      name: titleInput.value.trim() || "Untitled document",
      doc: editor.getJSON(),
      source_doc,
      source_type,
      margins,
      pageNumbers,
    };
    try {
      const res = await fetch(reportUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(await res.text());
      dirty = false;
      setStatus("Saved");
    } finally {
      saving = false;
      if (saveAgainAfter) {
        saveAgainAfter = false;
        await saveReport();
      }
    }
  }

  titleInput.addEventListener("input", markDirty);

  window.addEventListener("beforeunload", () => {
    if (!dirty) return;
    try {
      const { source_doc, source_type } = currentSource();
      const blob = new Blob(
        [JSON.stringify({ name: titleInput.value.trim() || "Untitled document", doc: editor.getJSON(), source_doc, source_type, margins, pageNumbers })],
        { type: "application/json" }
      );
      navigator.sendBeacon(reportUrl, blob);
    } catch (err) {
      // best effort only
    }
  });

  downloadPdfBtn.addEventListener("click", async () => {
    try {
      await saveReport();
      window.location.href = exportUrl;
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  });
  downloadDocxBtn.addEventListener("click", async () => {
    try {
      await saveReport();
      window.location.href = exportDocxUrl;
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  });
  saveBtn.addEventListener("click", async () => {
    try {
      await saveReport();
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveReport().catch((err) => setStatus("Save failed: " + err.message, true));
    }
  });

  // ---------------------------------------------------------------------
  // Source picker + snippet sidebar
  // ---------------------------------------------------------------------
  function updateAnnotateLink() {
    const { source_doc, source_type } = currentSource();
    if (source_doc) {
      annotateLink.href = `/annotations?doc=${encodeURIComponent(source_doc)}&type=${encodeURIComponent(source_type)}&page=1`;
      annotateLink.style.display = "";
    } else {
      annotateLink.style.display = "none";
    }
  }

  async function loadSnippets() {
    const { source_doc, source_type } = currentSource();
    if (!source_doc) {
      snippetListEl.innerHTML = '<p class="empty">Pick a source document above to see its snippets here.</p>';
      return;
    }
    try {
      const [snippetsRes, infoRes] = await Promise.all([
        fetch(`/api/doc/${encodeURIComponent(source_doc)}/snippets?type=${encodeURIComponent(source_type)}`),
        fetch(`/api/doc/${encodeURIComponent(source_doc)}/info?type=${encodeURIComponent(source_type)}`),
      ]);
      const list = await snippetsRes.json();
      const pages = infoRes.ok ? (await infoRes.json()).pages : [];
      snippetListEl.innerHTML = "";
      if (!list.length) {
        snippetListEl.innerHTML =
          '<p class="empty">No snippets or annotations yet. Create some on the ' +
          `<a href="/annotations?doc=${encodeURIComponent(source_doc)}&type=${encodeURIComponent(source_type)}&page=1">annotation page</a>, then come back here.</p>`;
        return;
      }
      for (const s of list) {
        const label = s.annotated ? "Annotation" : "Snippet";
        const card = document.createElement("div");
        card.className = "snippet-card";
        card.innerHTML = `
          <img src="${s.url}" alt="${label} from page ${s.page}">
          <div class="snippet-actions">
            <span class="tag">${label} &middot; p${s.page}</span>
            <button type="button" class="insert-snippet">Insert</button>
          </div>`;
        card.querySelector(".insert-snippet").addEventListener("click", () => {
          const pageInfo = pages[s.page - 1];
          const attrs = { src: s.url, alt: `${label} from page ${s.page}` };
          // Snippet PNGs are rasterized at a fixed export DPI (300, see
          // api_create_snippet in app.py) that's higher than the ~150dpi a
          // page is rendered at for on-screen viewing, so the PNG's raw
          // pixel size renders roughly 2x too large if dropped in as-is.
          // The physically-correct size is independent of either DPI: the
          // snippet's fractional rect (x/y/w/h, 0-1) times its source
          // page's real size (in points, from /info) gives its true size,
          // which converts to CSS px the same way margins do (PT_TO_PX).
          if (pageInfo && s.rect) {
            attrs.width = Math.round(s.rect.w * pageInfo.width * PT_TO_PX);
            attrs.height = Math.round(s.rect.h * pageInfo.height * PT_TO_PX);
          }
          editor.chain().focus().setImage(attrs).run();
          markDirty();
        });
        snippetListEl.appendChild(card);
      }
    } catch (e) {
      console.error(e);
      snippetListEl.innerHTML = '<p class="empty">Failed to load snippets.</p>';
    }
  }

  sourceSelect.addEventListener("change", () => {
    updateAnnotateLink();
    loadSnippets();
    markDirty();
  });

  // ---------------------------------------------------------------------
  // Initial load
  // ---------------------------------------------------------------------
  async function loadReport() {
    loadingReport = true;
    try {
      const res = await fetch(reportUrl);
      const data = await res.json();
      titleInput.value = data.name || "";
      margins = normalizeMargins(data.margins);
      pageNumbers = normalizePageNumbers(data.pageNumbers);
      applyMarginsToCss();
      if (data.doc && data.doc.type === "doc") editor.commands.setContent(data.doc);
      else editor.commands.setContent(data.html || "");
      const combo = data.source_doc ? `${data.source_doc}|${data.source_type}` : "";
      sourceSelect.value = combo;
      if (sourceSelect.value !== combo) sourceSelect.value = "";
      updateAnnotateLink();
      loadSnippets();
      dirty = false;
      setStatus("");
    } catch (e) {
      setStatus("Failed to load document: " + e.message, true);
    } finally {
      loadingReport = false;
    }
  }

  loadReport();
})();
