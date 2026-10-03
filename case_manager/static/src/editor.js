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
import { ParagraphSpacing, hasStandardSpacing, tightenParagraphs } from "./paragraphSpacing.js";
import { handleTabCharacter } from "./tabKey.js";
import { Link } from "@tiptap/extension-link";
import { setupLinkAndImage } from "./linkImage.js";
import { ResizableImage, setSelectedImageAlign, isImageSelected, setSnippetRefs, snippetSource } from "./resizableImage.js";
import {
  OrderedList,
  ListItem,
  ListMarkers,
  ListMarkerSelect,
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
  // Viewers can open a report but not change it (the server rejects their
  // saves with a 403); the page is put into a read-only state below.
  const canEdit = !reportId || !!appEl.dataset.canEdit;
  // Guests (secret-key sessions) can't create reports or upload documents.
  const canCreate = !!appEl.dataset.canCreate;

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
  const downloadPdfAnnexBtn = document.getElementById("downloadPdfAnnexBtn");
  const downloadDocxBtn = document.getElementById("downloadDocxBtn");

  const pageSetupModal = document.getElementById("pageSetupModal");
  const marginLeftInput = document.getElementById("marginLeftInput");
  const marginRightInput = document.getElementById("marginRightInput");
  const marginHeaderInput = document.getElementById("marginHeaderInput");
  const marginFooterInput = document.getElementById("marginFooterInput");
  const pageNumberPositionInput = document.getElementById("pageNumberPositionInput");
  const pageNumberStartInput = document.getElementById("pageNumberStartInput");
  const pageNumberFirstInput = document.getElementById("pageNumberFirstInput");
  const pageNumberFontInput = document.getElementById("pageNumberFontInput");
  const pageNumberFontSizeInput = document.getElementById("pageNumberFontSizeInput");
  const pageNumberShapeInput = document.getElementById("pageNumberShapeInput");
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
  // Kept in sync with the #pageNumberFontInput <option> values in
  // document.html (and with REPORT_PAGE_NUMBER_FONTS in app.py).
  const PAGE_NUMBER_FONTS = new Set([
    "Arial", "Georgia", "'Times New Roman'", "'Courier New'",
    "Verdana", "'Trebuchet MS'", "'Comic Sans MS'", "'Bookman Old Style'", "Calibri",
  ]);
  const PAGE_NUMBER_FONT_SIZE_MIN = 6;
  const PAGE_NUMBER_FONT_SIZE_MAX = 72;
  const PAGE_NUMBER_SHAPES = new Set(["none", "circle", "rectangle"]);
  const HEX_COLOR = /^#[0-9a-f]{6}$/i;
  const DEFAULT_PAGE_NUMBERS = { position: "top-center", skip: 0, first: 1, font: "Arial", fontSize: 11, shape: "none", color: "#555555" };
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

  // Briefly highlights the title field to confirm an explicit save, since
  // the header's save-status text is easy to miss (mirrors static/cases.js).
  function flashSaved(el) {
    el.classList.remove("save-flash");
    void el.offsetWidth; // restart the animation if it's already running
    el.classList.add("save-flash");
    el.addEventListener("animationend", () => el.classList.remove("save-flash"), { once: true });
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

  if (!canCreate) {
    newDocBtn.disabled = true;
    uploadSourceMenuBtn.disabled = true;
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
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || res.statusText);
      window.location.href = `/reports?report=${encodeURIComponent(data.id)}`;
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

  // Same click-to-arm confirmation as the allegation/evidence cards
  // (static/allegations.js's wireConfirmDelete): first click shows a red
  // "Confirm?" for 3s, second click within that window actually deletes.
  function wireConfirmDelete(btn, onConfirm) {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!btn.classList.contains("confirming")) {
        btn.classList.add("confirming");
        btn.textContent = "Confirm?";
        btn._confirmTimer = setTimeout(() => {
          btn.classList.remove("confirming");
          btn.textContent = "✕";
        }, 3000);
        return;
      }
      clearTimeout(btn._confirmTimer);
      onConfirm();
    });
  }

  // `withThumbnail`: the landing grid uses the same card as the articles
  // workspace (thumbnail = the report's first image, if it has one); the
  // compact "Open" modal list keeps the plain card.
  function renderReportCard(r, container, onDeleted, withThumbnail) {
    const card = document.createElement("a");
    card.href = `/reports?report=${encodeURIComponent(r.id)}`;
    const sourceLabel = r.source_doc ? `${r.source_doc} (${r.source_type})` : "No source document";
    const causes = (r.cause_titles || []).join(", ") || "None";
    const delBtn = (cls) => r.role === "owner" ? `<button type="button" class="${cls} card-delete-btn" title="Delete report">✕</button>` : "";
    if (withThumbnail) {
      card.className = "article-card";
      const thumbnail = r.thumbnail_url ? `<img class="thumbnail" draggable="false" loading="lazy" src="${escapeHtml(r.thumbnail_url + (r.thumbnail_url.includes("?") ? "&" : "?") + "thumb=1")}" alt="">` : "";
      card.innerHTML = `
        ${delBtn("article-card-delete")}
        ${thumbnail}
        <div class="card-body">
          <div class="title">${escapeHtml(r.name || r.id)}</div>
          <div class="meta">${escapeHtml(sourceLabel)}</div>
          <div class="meta">${r.snippet_count || 0} ${r.snippet_count === 1 ? "snippet" : "snippets"}</div>
          <div class="meta" title="${escapeHtml(causes)}">Cause: ${escapeHtml(causes)}</div>
          <div class="meta">Updated ${escapeHtml(fmtDate(r.updated_at))}</div>
        </div>`;
    } else {
      card.className = "report-card";
      card.innerHTML = `
        ${delBtn("report-card-delete")}
        <div class="report-card-name">${escapeHtml(r.name || r.id)}</div>
        <div class="report-card-meta">${escapeHtml(sourceLabel)}</div>
        <div class="report-card-meta">${r.snippet_count || 0} ${r.snippet_count === 1 ? "snippet" : "snippets"}</div>
        <div class="report-card-meta" title="${escapeHtml(causes)}">Cause: ${escapeHtml(causes)}</div>
        <div class="report-card-meta">Updated ${escapeHtml(fmtDate(r.updated_at))}</div>`;
    }
    const deleteBtn = card.querySelector(".card-delete-btn");
    if (deleteBtn) wireConfirmDelete(deleteBtn, async () => {
      try {
        const res = await fetch(`/api/report/${encodeURIComponent(r.id)}`, { method: "DELETE" });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
        card.remove();
        if (onDeleted) onDeleted();
      } catch (err) {
        window.alert("Could not delete report: " + err.message);
      }
    });
    container.appendChild(card);
  }

  async function fetchReports() {
    const res = await fetch(reportsUrl + "?default_cause=1");
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
      for (const r of list) {
        renderReportCard(r, openDocList, () => {
          if (!openDocList.querySelector(".report-card")) {
            openDocList.innerHTML = '<p class="empty">No documents yet.</p>';
          }
        });
      }
    } catch (e) {
      openDocList.innerHTML = '<p class="empty">Failed to load documents.</p>';
    }
  });
  openDocCancel.addEventListener("click", () => closeModal(openDocModal));
  openDocModal.addEventListener("click", (e) => {
    if (e.target === openDocModal) closeModal(openDocModal);
  });

  // ---- Landing page sections ----
  // Users organise report cards into named sections. "General" is implicit
  // and holds every report not assigned elsewhere. The layout is stored on
  // the user's account (users.report_sections): [{id, name, reports: [...]}].
  const sectionsUrl = appEl.dataset.sectionsUrl;
  const legacySectionsKey = "reportSections:" + (appEl.dataset.userId || "");
  let saveTimer = null;
  function saveSections() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      fetch(sectionsUrl, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sections),
      }).catch((e) => console.error("Could not save sections", e));
    }, 200);
  }
  async function loadSections() {
    try {
      const res = await fetch(sectionsUrl);
      if (!res.ok) return [];
      const server = await res.json();
      if (server.length) return server;
      // One-time import of a layout saved in this browser before it moved
      // to the database.
      const old = JSON.parse(localStorage.getItem(legacySectionsKey));
      if (Array.isArray(old) && old.length) {
        localStorage.removeItem(legacySectionsKey);
        sections = old;
        saveSections();
        return old;
      }
      return server;
    } catch (e) {
      return [];
    }
  }
  let sections = [];
  let landingReports = [];
  const addSectionBtn = document.getElementById("addSectionBtn");

  function moveReportToSection(reportId, sectionId) {
    for (const sec of sections) sec.reports = sec.reports.filter((id) => id !== reportId);
    if (sectionId) sections.find((s) => s.id === sectionId).reports.push(reportId);
    saveSections();
    renderLanding();
  }

  function buildSection(title, sectionId) {
    const wrap = document.createElement("section");
    wrap.className = "report-section";
    if (sectionId) wrap.dataset.sectionId = sectionId;
    const head = document.createElement("div");
    head.className = "report-section-head";
    head.innerHTML = sectionId
      ? '<span class="section-handle" title="Drag to reorder">&#8942;&#8942;</span><h3 class="section-title"></h3><button type="button" class="section-rename" title="Rename section">Rename</button><button type="button" class="section-delete card-delete-btn" title="Delete section (reports move to General)">✕</button>'
      : '<h3 class="section-title"></h3>';
    head.querySelector(".section-title").textContent = title;
    const grid = document.createElement("div");
    grid.className = "article-card-grid report-section-grid";
    wrap.append(head, grid);

    // Drop zone for report cards.
    wrap.addEventListener("dragover", (e) => {
      if (!dragReportId) return;
      e.preventDefault();
      wrap.classList.add("drop-target");
    });
    wrap.addEventListener("dragleave", (e) => {
      if (!wrap.contains(e.relatedTarget)) wrap.classList.remove("drop-target");
    });
    wrap.addEventListener("drop", (e) => {
      if (!dragReportId) return;
      e.preventDefault();
      wrap.classList.remove("drop-target");
      moveReportToSection(dragReportId, sectionId || null);
    });

    if (sectionId) {
      const sec = sections.find((s) => s.id === sectionId);
      const handle = head.querySelector(".section-handle");
      handle.addEventListener("mousedown", () => { wrap.draggable = true; });
      wrap.addEventListener("dragend", () => { wrap.draggable = false; wrap.classList.remove("dragging"); dragSectionId = null; });
      wrap.addEventListener("dragstart", (e) => {
        if (!wrap.draggable || dragReportId) return;
        dragSectionId = sectionId;
        wrap.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", sectionId);
      });
      wrap.addEventListener("dragover", (e) => {
        if (!dragSectionId || dragSectionId === sectionId) return;
        e.preventDefault();
        const rect = wrap.getBoundingClientRect();
        const before = e.clientY < rect.top + rect.height / 2;
        const from = sections.findIndex((s) => s.id === dragSectionId);
        let to = sections.findIndex((s) => s.id === sectionId) + (before ? 0 : 1);
        if (from < to) to--;
        if (from === to) return;
        const [moved] = sections.splice(from, 1);
        sections.splice(to, 0, moved);
        const dragged = reportListLanding.querySelector(".report-section.dragging");
        reportListLanding.insertBefore(dragged, before ? wrap : wrap.nextSibling);
      });
      wrap.addEventListener("drop", (e) => { if (dragSectionId) { e.preventDefault(); saveSections(); } });
      head.querySelector(".section-rename").addEventListener("click", () => {
        const name = window.prompt("Section name", sec.name);
        if (name && name.trim()) { sec.name = name.trim().slice(0, 80); saveSections(); renderLanding(); }
      });
      wireConfirmDelete(head.querySelector(".section-delete"), () => {
        sections = sections.filter((s) => s.id !== sectionId);
        saveSections();
        renderLanding();
      });
    }
    reportListLanding.appendChild(wrap);
    return grid;
  }

  let dragReportId = null;
  let dragSectionId = null;

  function renderLanding() {
    reportListLanding.innerHTML = "";
    landingEmptyHint.style.display = landingReports.length ? "none" : "block";
    const byId = new Map(landingReports.map((r) => [r.id, r]));
    const placed = new Set();
    const cardsFor = (ids) => ids.map((id) => byId.get(id)).filter((r) => r && !placed.has(r.id) && placed.add(r.id));
    const sectionCards = sections.map((sec) => cardsFor(sec.reports));
    const general = landingReports.filter((r) => !placed.has(r.id));
    const showSections = sections.length > 0;
    const fill = (grid, list) => {
      for (const r of list) {
        renderReportCard(r, grid, () => {
          landingReports = landingReports.filter((x) => x.id !== r.id);
          renderLanding();
        }, true);
        const card = grid.lastElementChild;
        card.draggable = true;
        card.addEventListener("dragstart", (e) => {
          dragReportId = r.id;
          card.classList.add("dragging");
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", r.id);
          e.stopPropagation();
        });
        card.addEventListener("dragend", () => { dragReportId = null; card.classList.remove("dragging"); });
      }
    };
    // General is shown only when sections exist (a lone General needs no
    // heading) but is always a valid drop target for moving cards back.
    fill(buildSection("General", null), general);
    if (!showSections) reportListLanding.firstChild.querySelector(".report-section-head").style.display = "none";
    sections.forEach((sec, i) => fill(buildSection(sec.name, sec.id), sectionCards[i]));
  }

  if (addSectionBtn) {
    addSectionBtn.addEventListener("click", () => {
      const name = window.prompt("New section name");
      if (!name || !name.trim()) return;
      sections.push({ id: "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), name: name.trim().slice(0, 80), reports: [] });
      saveSections();
      renderLanding();
    });
  }

  async function loadLanding() {
    if (!reportListLanding) return;
    try {
      [landingReports, sections] = await Promise.all([fetchReports(), loadSections()]);
      renderLanding();
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

  // One-off confirmation after File > Make a copy opened the new report.
  function showCopyNotice() {
    let info = null;
    try {
      info = JSON.parse(sessionStorage.getItem("reportCopyNotice") || "null");
      sessionStorage.removeItem("reportCopyNotice");
    } catch (e) { /* no notice */ }
    if (!info || info.id !== reportId) return;
    const el = document.createElement("div");
    el.className = "flash-notice";
    el.textContent = `A copy of report titled "${info.original}" has been created and it is called "${info.name}".`;
    document.getElementById("formatToolbar").parentNode.insertBefore(el, document.getElementById("formatToolbar"));
    setTimeout(() => el.remove(), 10000);
  }
  showCopyNotice();

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
    const firstRaw = raw && typeof raw === "object" ? Number(raw.first) : NaN;
    const first = Number.isInteger(firstRaw) && firstRaw >= 1 && firstRaw <= 100000 ? firstRaw : DEFAULT_PAGE_NUMBERS.first;
    const font = raw && PAGE_NUMBER_FONTS.has(raw.font) ? raw.font : DEFAULT_PAGE_NUMBERS.font;
    const fontSizeRaw = raw && typeof raw === "object" ? Number(raw.fontSize) : NaN;
    const fontSize = Number.isFinite(fontSizeRaw) && fontSizeRaw >= PAGE_NUMBER_FONT_SIZE_MIN && fontSizeRaw <= PAGE_NUMBER_FONT_SIZE_MAX
      ? fontSizeRaw : DEFAULT_PAGE_NUMBERS.fontSize;
    const shape = raw && PAGE_NUMBER_SHAPES.has(raw.shape) ? raw.shape : DEFAULT_PAGE_NUMBERS.shape;
    const color = raw && typeof raw.color === "string" && HEX_COLOR.test(raw.color) ? raw.color.toLowerCase() : DEFAULT_PAGE_NUMBERS.color;
    return { position, skip, first, font, fontSize, shape, color };
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
        label.style.fontFamily = pageNumbers.font;
        label.style.fontSize = pageNumbers.fontSize + "pt";
        label.style.color = pageNumbers.color;
        const num = document.createElement("span");
        num.className = "page-number-outline";
        num.textContent = String(i - pageNumbers.skip + pageNumbers.first);
        if (pageNumbers.shape !== "none") {
          num.style.border = "1px solid " + pageNumbers.color;
          num.style.borderRadius = pageNumbers.shape === "circle" ? "50%" : "0";
          num.classList.add("page-number-shaped");
        }
        label.appendChild(num);
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

  // UI-facing: the page number (1-based) at which numbering should start.
  // Maps to the internally-stored, 0-based `skip` count (skip = start - 1).
  function clampPageNumberStart(v) {
    const n = Number(v);
    if (!Number.isInteger(n)) return null;
    return Math.min(51, Math.max(1, n));
  }

  function clampPageNumberFirst(v) {
    const n = Number(v);
    if (!Number.isInteger(n)) return null;
    return Math.min(100000, Math.max(1, n));
  }

  function clampPageNumberFontSize(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    return Math.min(PAGE_NUMBER_FONT_SIZE_MAX, Math.max(PAGE_NUMBER_FONT_SIZE_MIN, n));
  }

  // Page numbers live in the sidebar and apply as soon as a control changes.
  const selectPageNumberSwatch = PageNumberPalette.mountPopover("pageNumberColor", (c) => {
    pageNumbers = { ...pageNumbers, color: c };
    selectPageNumberSwatch(c);
    applyPageNumbers();
  });

  function renderPageNumberInputs() {
    pageNumberPositionInput.value = pageNumbers.position;
    pageNumberStartInput.value = pageNumbers.skip + 1;
    pageNumberFirstInput.value = pageNumbers.first;
    pageNumberFontInput.value = pageNumbers.font;
    pageNumberFontSizeInput.value = pageNumbers.fontSize;
    pageNumberShapeInput.value = pageNumbers.shape;
    selectPageNumberSwatch(pageNumbers.color);
  }

  function applyPageNumbers() {
    renderMarginGuides();
    markDirty();
  }

  function readPageNumbers() {
    const start = clampPageNumberStart(pageNumberStartInput.value);
    const fontSize = clampPageNumberFontSize(pageNumberFontSizeInput.value);
    const first = clampPageNumberFirst(pageNumberFirstInput.value);
    pageNumberStartInput.classList.toggle("invalid", start === null);
    pageNumberFirstInput.classList.toggle("invalid", first === null);
    pageNumberFontSizeInput.classList.toggle("invalid", fontSize === null);
    if (start === null || first === null || fontSize === null) return;
    if (!PAGE_NUMBER_POSITIONS.has(pageNumberPositionInput.value) || !PAGE_NUMBER_FONTS.has(pageNumberFontInput.value)) return;
    pageNumbers = {
      position: pageNumberPositionInput.value, skip: start - 1, first, font: pageNumberFontInput.value, fontSize,
      shape: PAGE_NUMBER_SHAPES.has(pageNumberShapeInput.value) ? pageNumberShapeInput.value : "none",
      color: pageNumbers.color,
    };
    applyPageNumbers();
  }
  [pageNumberPositionInput, pageNumberStartInput, pageNumberFirstInput, pageNumberFontInput, pageNumberFontSizeInput, pageNumberShapeInput]
    .forEach((el) => el.addEventListener("change", readPageNumbers));

  pageSetupBtn.addEventListener("click", () => {
    marginLeftInput.value = margins.left;
    marginRightInput.value = margins.right;
    marginHeaderInput.value = margins.header;
    marginFooterInput.value = margins.footer;
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
    margins = { left, right, header, footer };
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
      ListMarkerSelect,
      BlockIndent,
      TextStyleKit.configure({ backgroundColor: false, lineHeight: false }),
      Highlight.configure({ multicolor: true }),
      TextAlign.configure({ types: ["heading", "paragraph"] }),
      ParagraphSpacing,
      Link.configure({ openOnClick: false, autolink: false }),
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

  if (!canEdit) {
    editor.setEditable(false);
    titleInput.readOnly = true;
    saveBtn.disabled = true;
    sourceSelect.disabled = true;
    document.querySelectorAll(".annex-numbering select, .annex-numbering input, .annex-numbering button").forEach((el) => {
      el.disabled = true;
    });
    // Grey out everything in the File menu except the downloads.
    fileMenuDropdown.querySelectorAll("button").forEach((b) => {
      b.disabled = b !== downloadPdfBtn && b !== downloadPdfAnnexBtn && b !== downloadDocxBtn;
    });
  }

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

  // Tab/Shift+Tab: sink/lift within a list, else type/remove a tab character. Enter on an empty list item exits the
  // list outright, regardless of nesting depth (see listNumbering.js).
  editorEl.addEventListener(
    "keydown",
    (e) => {
      if (e.key === "Tab") {
        e.preventDefault();
        const dir = e.shiftKey ? -1 : 1;
        if (dir === 1 && editor.can().sinkListItem("listItem")) editor.chain().focus().sinkListItem("listItem").run();
        else if (dir === -1 && editor.can().liftListItem("listItem")) editor.chain().focus().liftListItem("listItem").run();
        else handleTabCharacter(editor, e);
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

  const undoBtn = document.getElementById("undoBtn");
  const redoBtn = document.getElementById("redoBtn");
  undoBtn.addEventListener("click", () => editor.chain().focus().undo().run());
  redoBtn.addEventListener("click", () => editor.chain().focus().redo().run());
  function syncUndoRedoButtons() {
    undoBtn.disabled = !editor.can().undo();
    redoBtn.disabled = !editor.can().redo();
  }
  editor.on("transaction", syncUndoRedoButtons);
  syncUndoRedoButtons();

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
      } else if (cmd === "paragraphSpacing") {
        editor.chain().focus().toggleParagraphSpacing().run();
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

  // Text/highlight colour pickers: a button opening the same palette popover
  // the annotation viewer's Color button uses. The hidden input holds the value.
  function mountColorPopover(name, onPick) {
    const btn = document.getElementById(name + "ColorBtn");
    const popover = document.getElementById(name + "ColorPopover");
    const input = document.getElementById(name + "ColorInput");
    const swatch = document.getElementById(name + "ColorSwatch");
    const setOpen = (open) => {
      popover.hidden = !open;
      btn.setAttribute("aria-expanded", open);
    };
    const select = PageNumberPalette.mount(document.getElementById(name + "ColorPalette"), (c) => {
      input.value = c;
      swatch.style.background = c;
      select(c);
      setOpen(false);
      onPick(c);
    });
    // Keep the editor selection while the button is pressed.
    btn.addEventListener("mousedown", (e) => e.preventDefault());
    btn.addEventListener("click", () => setOpen(popover.hidden));
    document.addEventListener("mousedown", (e) => {
      if (!popover.hidden && !popover.contains(e.target) && !btn.contains(e.target)) setOpen(false);
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });
    swatch.style.background = input.value;
    select(input.value);
    return { input, swatch, select };
  }

  const textPicker = mountColorPopover("text", (c) => editor.chain().focus().setColor(c).run());
  const textColorInput = textPicker.input;
  const textColorSwatch = textPicker.swatch;

  const highlightPicker = mountColorPopover("highlight", (c) => editor.chain().focus().setHighlight({ color: c }).run());
  const highlightColorInput = highlightPicker.input;
  const highlightColorSwatch = highlightPicker.swatch;
  document.getElementById("noHighlightBtn").addEventListener("click", () => {
    editor.chain().focus().unsetHighlight().run();
    document.getElementById("highlightColorPopover").hidden = true;
    document.getElementById("highlightColorBtn").setAttribute("aria-expanded", false);
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
    textPicker.select(color);

    const highlightColor = editor.getAttributes("highlight").color;
    if (highlightColor) {
      highlightColorInput.value = highlightColor;
      highlightColorSwatch.style.background = highlightColor;
      highlightColorSwatch.style.outline = "";
      highlightPicker.select(highlightColorInput.value);
    } else {
      highlightColorSwatch.style.background = "transparent";
      highlightColorSwatch.style.outline = "1px solid #b8bfcf";
      highlightPicker.select(null);
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
      else if (cmd === "paragraphSpacing") active = hasStandardSpacing(editor);
      else if (cmd === "insertUnorderedList") active = editor.isActive("bulletList");
      else if (cmd === "insertOrderedList") active = editor.isActive("orderedList");
      else return;
      btn.classList.toggle("active", active);
    });
    document.getElementById("linkBtn").classList.toggle("active", editor.isActive("link"));
  }
  editor.on("transaction", syncToolbarToSelection);
  syncToolbarToSelection();

  setupLinkAndImage({
    editor, canEdit, setStatus, onChange: markDirty,
    imageUploadUrl: appEl.dataset.imageUploadUrl,
  });

  // ---------------------------------------------------------------------
  // Autosave / save / export
  // ---------------------------------------------------------------------
  function markDirty() {
    if (loadingReport || !canEdit) return;
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
    if (!canEdit) return;
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
      refreshSnippetRefs();
    } finally {
      saving = false;
      if (saveAgainAfter) {
        saveAgainAfter = false;
        await saveReport();
      }
    }
  }

  titleInput.addEventListener("input", markDirty);

  // File > Make a copy: saves this report, copies it server-side as
  // "Copy n of <title>" (same cause), then opens the copy, where the
  // confirmation is flashed (see showCopyNotice).
  const copyDocBtn = document.getElementById("copyDocBtn");
  copyDocBtn.addEventListener("click", async () => {
    try {
      setStatus("Copying…");
      await saveReport();
      const res = await fetch(`/api/report/${encodeURIComponent(reportId)}/copy`, { method: "POST" });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
      const copy = await res.json();
      try {
        sessionStorage.setItem("reportCopyNotice", JSON.stringify({ id: copy.id, name: copy.name, original: copy.original_name }));
      } catch (e) { /* the copy is still opened, just without the notice */ }
      window.location.href = `/reports?report=${encodeURIComponent(copy.id)}`;
    } catch (e) {
      setStatus("Could not make a copy: " + e.message, true);
    }
  });

  // Pressing Enter in the title field saves right away instead of waiting
  // out the autosave debounce, flashing the field green on success (mirrors
  // the card fields on the cases/allegations pages).
  titleInput.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    setTimeout(() => {
      saveReport()
        .then(() => flashSaved(titleInput))
        .catch((err) => setStatus("Save failed: " + err.message, true));
    }, 0);
  });

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
  downloadPdfAnnexBtn.addEventListener("click", async () => {
    try {
      await saveReport();
      window.location.href = exportUrl + "?annexures=1";
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
      saveReport()
        .then(() => flashSaved(titleInput))
        .catch((err) => setStatus("Save failed: " + err.message, true));
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

  // ---------------------------------------------------------------------
  // Case setup: associate the report with one of the cases under its
  // cause(s), or dissociate it. The button is only offered when the report
  // has no case yet and such cases exist, or when it already has one.
  // ---------------------------------------------------------------------
  const caseSetupBtn = document.getElementById("caseSetupBtn");
  const caseSetupModal = document.getElementById("caseSetupModal");
  const caseSetupBody = document.getElementById("caseSetupBody");
  const caseSetupError = document.getElementById("caseSetupError");
  const caseSetupClose = document.getElementById("caseSetupClose");
  let reportCauseIds = [];
  let reportCaseIds = [];
  let candidateCases = []; // editable cases under the report's causes

  async function refreshCaseSetup() {
    if (!caseSetupBtn || !canEdit) return;
    try {
      const [reportRes, casesRes] = await Promise.all([fetch(reportUrl), fetch("/api/allegation-cases")]);
      const report = await reportRes.json();
      const all = await casesRes.json();
      reportCauseIds = report.cause_ids || [];
      reportCaseIds = report.case_ids || [];
      const list = Array.isArray(all) ? all : all.cases || [];
      candidateCases = list.filter(
        (c) => reportCaseIds.includes(c.id) || (reportCauseIds.includes(c.cause_id) && (c.role === "owner" || c.role === "editor"))
      );
      caseSetupBtn.style.display = candidateCases.length ? "" : "none";
    } catch (e) {
      console.error(e);
    }
  }

  function renderCaseSetup() {
    caseSetupError.style.display = "none";
    const current = candidateCases.find((c) => reportCaseIds.includes(c.id));
    if (current) {
      caseSetupBody.innerHTML =
        `<p>This report is associated with the case <strong>${escapeHtml(current.name)}</strong>.</p>` +
        '<div class="modal-btn-stack">' +
        `<a class="btn" id="caseDetailsLink" href="${escapeHtml(appEl.dataset.caseDetailsUrl)}?case=${encodeURIComponent(current.id)}">Edit cause title</a>` +
        '<button type="button" class="btn" id="insertCauseTitleBtn">Insert cause title</button>' +
        '<button type="button" class="btn" id="caseDissociateBtn">Dissociate from case</button>' +
        '</div>';
      document.getElementById("insertCauseTitleBtn").addEventListener("click", insertCauseTitle);
      document.getElementById("caseDissociateBtn").addEventListener("click", () => updateCaseLink("DELETE", current.id));
      return;
    }
    const options = candidateCases
      .map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`)
      .join("");
    caseSetupBody.innerHTML =
      '<label class="modal-field">Associate this report with case' +
      `<select id="caseSetupSelect">${options}</select></label>` +
      '<div class="modal-btn-stack"><button type="button" class="btn" id="caseAssociateBtn">Associate</button></div>';
    document.getElementById("caseAssociateBtn").addEventListener("click", () =>
      updateCaseLink("POST", document.getElementById("caseSetupSelect").value)
    );
  }

  async function updateCaseLink(method, caseId) {
    try {
      const res = await fetch(`/api/report/${encodeURIComponent(reportId)}/links`, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_id: caseId }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${res.status}`);
      }
      reportCaseIds = method === "POST" ? [caseId] : reportCaseIds.filter((id) => id !== caseId);
      renderCaseSetup();
    } catch (e) {
      caseSetupError.textContent = e.message;
      caseSetupError.style.display = "";
    }
  }

  if (caseSetupBtn) {
    caseSetupBtn.addEventListener("click", () => {
      renderCaseSetup();
      openModal(caseSetupModal);
    });
    caseSetupClose.addEventListener("click", () => closeModal(caseSetupModal));
    caseSetupModal.addEventListener("click", (e) => {
      if (e.target === caseSetupModal) closeModal(caseSetupModal);
    });
  }

  // Insert cause title: puts the report's case's cause title at the very
  // start of the document. A case with no generated title sends the user to
  // its details page (with a notice) to make one first.
  async function insertCauseTitle() {
    const caseId = reportCaseIds[0];
    if (!caseId) return;
    closeModal(caseSetupModal);
    const detailsUrl = `${appEl.dataset.caseDetailsUrl}?case=${encodeURIComponent(caseId)}`;
    try {
      const [caseRes, templatesRes] = await Promise.all([
        fetch(appEl.dataset.caseUrlBase.replace("__ID__", encodeURIComponent(caseId))),
        fetch(appEl.dataset.templatesUrl),
      ]);
      if (!caseRes.ok) throw new Error((await caseRes.json().catch(() => ({}))).error || `HTTP ${caseRes.status}`);
      const caseData = await caseRes.json();
      caseData.parties = Array.isArray(caseData.parties) ? caseData.parties : [];
      caseData.cause_title_doc = caseData.cause_title_doc ? JSON.parse(caseData.cause_title_doc) : null;
      const templates = templatesRes.ok ? await templatesRes.json() : [];
      if (!window.CauseTitle.exists(caseData, templates)) {
        location.href = detailsUrl + "&notice=no-cause-title";
        return;
      }
      const doc = window.CauseTitle.doc(caseData, templates);
      editor.chain().focus().insertContentAt(0, tightenParagraphs(doc.content)).run();
    } catch (e) {
      setStatus("Could not insert the cause title: " + e.message, true);
    }
  }

  // Annexure references show the annexure's current numbering, so refetch it
  // on load, after a save (the saved snippets decide which documents are
  // annexed) and whenever the user comes back from the Annexures page.
  async function refreshSnippetRefs() {
    try {
      const res = await fetch(`${reportUrl}/annexure/refs`);
      if (res.ok) setSnippetRefs(await res.json());
    } catch (e) {
      console.error(e);
    }
  }
  window.addEventListener("pageshow", refreshSnippetRefs);
  window.addEventListener("focus", refreshSnippetRefs);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshSnippetRefs();
  });

  const INSERT_STYLE_KEY = "snippetInsertStyle";
  const insertStyleInputs = Array.from(document.querySelectorAll('input[name="snippetInsertStyle"]'));
  function insertStyle() {
    const checked = insertStyleInputs.find((el) => el.checked);
    return checked ? checked.value : "both";
  }
  function showInsertStyle(style) {
    insertStyleInputs.forEach((el) => { el.checked = el.value === style; });
    if (!insertStyleInputs.some((el) => el.checked) && insertStyleInputs[0]) insertStyleInputs[0].checked = true;
  }
  try { showInsertStyle(localStorage.getItem(INSERT_STYLE_KEY)); } catch (e) {}

  // Gives every snippet already in the report the chosen style.
  function applyStyleToSnippets(style) {
    const tr = editor.state.tr;
    editor.state.doc.descendants((node, pos) => {
      if (node.type.name === "image" && snippetSource(node.attrs.src) && node.attrs.refStyle !== style) {
        tr.setNodeMarkup(pos, undefined, { ...node.attrs, refStyle: style });
      }
    });
    if (tr.docChanged) editor.view.dispatch(tr);
  }
  insertStyleInputs.forEach((el) => el.addEventListener("change", () => {
    const style = insertStyle();
    try { localStorage.setItem(INSERT_STYLE_KEY, style); } catch (e) {}
    if (canEdit) applyStyleToSnippets(style);
  }));

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
        const annotationUrl = `/annotations?doc=${encodeURIComponent(source_doc)}&type=${encodeURIComponent(source_type)}&page=${encodeURIComponent(s.page)}`;
        card.innerHTML = `
          <img src="${s.url}" alt="${label} from page ${s.page}">
          <div class="snippet-actions">
            <a class="tag" href="${annotationUrl}" title="Open page ${s.page} in annotations">${label} &middot; p${s.page}</a>
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
          attrs.refStyle = insertStyle();
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
      renderPageNumberInputs();
      applyMarginsToCss();
      // Loading a report's saved content is not a user edit -- without
      // suppressing history here, this transaction becomes the first undo
      // step, so the very first Cmd+Z after opening a report wipes the
      // document back to the editor's pristine empty state instead of
      // undoing anything the user actually did.
      const loadChain = editor.chain().setMeta("addToHistory", false);
      if (data.doc && data.doc.type === "doc") loadChain.setContent(data.doc);
      else loadChain.setContent(data.html || "");
      loadChain.run();
      const combo = data.source_doc ? `${data.source_doc}|${data.source_type}` : "";
      sourceSelect.value = combo;
      if (sourceSelect.value !== combo) sourceSelect.value = "";
      updateAnnotateLink();
      refreshCaseSetup();
      loadSnippets();
      refreshSnippetRefs();
      let loaded = null;
      editor.state.doc.descendants((node) => {
        if (!loaded && node.type.name === "image" && snippetSource(node.attrs.src)) loaded = node.attrs.refStyle || "image";
      });
      if (loaded) showInsertStyle(loaded);
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
