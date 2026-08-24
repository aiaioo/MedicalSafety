(function () {
  const appEl = document.getElementById("docApp");
  const reportId = appEl.dataset.report;
  const reportsUrl = appEl.dataset.reportsUrl;
  const reportUrl = appEl.dataset.reportUrl;
  const exportUrl = appEl.dataset.exportUrl;
  const exportDocxUrl = appEl.dataset.exportDocxUrl;
  const preselectSource = appEl.dataset.preselectSource;
  const preselectType = appEl.dataset.preselectType || "pdf";

  const editor = document.getElementById("editor");
  const marginGuidesEl = document.getElementById("marginGuides");
  const figureResizeOverlay = document.getElementById("figureResizeOverlay");
  const editorPageWrap = document.getElementById("editorPageWrap");
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
  const pageSetupError = document.getElementById("pageSetupError");
  const pageSetupCancel = document.getElementById("pageSetupCancel");
  const pageSetupApply = document.getElementById("pageSetupApply");

  // Page margins in points (pt); defaults match the geometry render_report_pdf
  // has always used. Overridden by whatever the report has saved, on load.
  const DEFAULT_MARGINS = { left: 36, right: 36, header: 46, footer: 46 };
  let margins = { ...DEFAULT_MARGINS };

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

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
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

  if (!reportId) {
    loadLanding();
    return; // nothing else to wire up until a document is open
  }

  // Ask the browser to express foreColor/hiliteColor/etc. as CSS (span style=)
  // rather than legacy <font>/... tags, which our sanitizer whitelist doesn't
  // allow and would otherwise strip the formatting right back out on save.
  document.execCommand("styleWithCSS", false, true);
  // Keep top-level blocks as <p> (rather than Chrome's default bare <div>)
  // so pagination has a predictable, semantic set of block children to walk.
  document.execCommand("defaultParagraphSeparator", false, "p");

  // ---------------------------------------------------------------------
  // Pagination: simulate A4 pages inside the single contenteditable by
  // inserting non-editable spacer elements between top-level blocks that
  // would otherwise overflow past a page boundary. Blocks are never split
  // mid-paragraph -- a block that doesn't fit on the current page moves to
  // the next page whole. Spacers only ever sit *between* blocks, never
  // inside the one the caret is in, so the current selection stays valid
  // across repagination without any manual save/restore.
  // ---------------------------------------------------------------------
  const PAGE_HEIGHT_PX = 1123; // px, A4 height at 96dpi
  const PT_TO_PX = 96 / 72;
  // Fixed visual gap between simulated pages (1cm at 96dpi) -- deliberately
  // NOT derived from header/footer margins. A page's header/footer margins
  // are its own white space (rendered as .page-filler, part of that page's
  // sheet); the gray gap is just a constant separator between sheets, same
  // idea as the fixed gap Google Docs/Word show regardless of your margins.
  const PAGE_GAP_PX = 96 / 2.54;

  function normalizeMargins(raw) {
    const out = {};
    for (const key of Object.keys(DEFAULT_MARGINS)) {
      const v = raw && typeof raw === "object" ? Number(raw[key]) : NaN;
      out[key] = Number.isFinite(v) && v >= 0 && v <= 200 ? v : DEFAULT_MARGINS[key];
    }
    return out;
  }

  function marginsPx() {
    return {
      left: margins.left * PT_TO_PX,
      right: margins.right * PT_TO_PX,
      header: margins.header * PT_TO_PX,
      footer: margins.footer * PT_TO_PX,
    };
  }

  // Pushes the current `margins` (pt) onto the CSS custom properties that
  // drive both .editor's padding and the margin-guide overlay, then
  // re-paginates (changing margins doesn't mutate #editor's DOM, so the
  // MutationObserver-driven schedulePaginate() below wouldn't otherwise fire).
  function applyMarginsToCss() {
    const px = marginsPx();
    editor.style.setProperty("--m-left", px.left + "px");
    editor.style.setProperty("--m-right", px.right + "px");
    editor.style.setProperty("--m-header", px.header + "px");
    editor.style.setProperty("--m-footer", px.footer + "px");
    if (marginGuidesEl) {
      marginGuidesEl.style.setProperty("--mg-left", px.left + "px");
      marginGuidesEl.style.setProperty("--mg-right", px.right + "px");
      marginGuidesEl.style.setProperty("--mg-top", px.header + "px");
      marginGuidesEl.style.setProperty("--mg-bottom", px.footer + "px");
    }
    schedulePaginate();
  }

  // Purely visual dashed-outline guides showing the printable area of each
  // simulated page, one .page-rect per page. Lives in the #marginGuides
  // overlay (a sibling of #editor, not inside it) so it never touches
  // #editor's DOM/content and can't leak into saved HTML.
  //
  // Page rects are measured from the actual .page-break elements just
  // inserted, not computed as uniform PAGE_HEIGHT_PX slices: pagination never
  // pads a page out to full height (a block that doesn't fit moves whole to
  // the next page), so a page's real on-screen height is usually less than
  // PAGE_HEIGHT_PX. Assuming a fixed height there made the guide overshoot
  // past the real page-break and bleed into the gray inter-page gap.
  function renderMarginGuides() {
    if (!marginGuidesEl) return;
    const editorRect = editor.getBoundingClientRect();
    const breaks = Array.from(editor.querySelectorAll(":scope > .page-break"));

    const frag = document.createDocumentFragment();
    let top = 0;
    for (let i = 0; i <= breaks.length; i++) {
      const bottom =
        i < breaks.length ? breaks[i].getBoundingClientRect().top - editorRect.top : editorRect.height;
      const rect = document.createElement("div");
      rect.className = "page-rect";
      rect.style.top = top + "px";
      rect.style.height = Math.max(0, bottom - top) + "px";
      frag.appendChild(rect);
      if (i < breaks.length) top = breaks[i].getBoundingClientRect().bottom - editorRect.top;
    }
    marginGuidesEl.replaceChildren(frag);
  }

  let paginateScheduled = false;
  let paginating = false;

  function schedulePaginate() {
    if (paginateScheduled) return;
    paginateScheduled = true;
    requestAnimationFrame(() => {
      paginateScheduled = false;
      paginate();
    });
  }

  // Inserted at the end of a page whose content falls short of
  // pageContentHeight, so every simulated page is at least a full A4 sheet
  // rather than shrinking to fit its content. contentEditable="false" like
  // .page-break, but pointer-events stays off (default) here on purpose:
  // this is blank space *within* a page (not the gap between pages), so a
  // click on it should fall through and land the caret at the nearest real
  // text, same as clicking below the last line of a normal document.
  function insertPageFiller(beforeNode, heightPx) {
    if (heightPx <= 0.5) return;
    const filler = document.createElement("div");
    filler.className = "page-filler";
    filler.contentEditable = "false";
    filler.style.height = heightPx + "px";
    if (beforeNode) editor.insertBefore(filler, beforeNode);
    else editor.appendChild(filler);
  }

  function paginate() {
    if (paginating) return;
    paginating = true;
    paginationObserver.disconnect();
    try {
      editor.querySelectorAll(":scope > .page-break, :scope > .page-filler").forEach((el) => el.remove());

      const px = marginsPx();
      const pageContentHeight = PAGE_HEIGHT_PX - px.header - px.footer;

      const blocks = Array.from(editor.childNodes).filter(
        (n) => n.nodeType === 1 && !n.classList.contains("page-break") && !n.classList.contains("page-filler")
      );

      // pageContentTop anchors to the true structural start of the current
      // page's content region -- editor's own CSS padding-top for page 1,
      // or (previous break's bottom + header margin) for later pages -- NOT
      // to a block's own rect.top. A block's own margin-top is usually much
      // smaller than the header margin, so using it as the anchor let text
      // start well above where the header margin guide line actually is;
      // using rect.bottom - pageContentTop (an actual measured distance,
      // not a manual margin sum) also avoids CSS margin-collapse making the
      // overflow check overshoot the footer line.
      const editorRect = editor.getBoundingClientRect();
      let pageContentTop = editorRect.top + px.header;
      let prevBottom = null;
      let pageNum = 1;
      for (const block of blocks) {
        let rect = block.getBoundingClientRect();
        if (prevBottom !== null && rect.bottom - pageContentTop > pageContentHeight) {
          // Close out the ending page: pad its remaining content area, then
          // its own footer margin (white, part of that page's sheet) --
          // editor's own padding-bottom only covers the *last* page's
          // footer, not a mid-document page's.
          insertPageFiller(block, pageContentHeight - (prevBottom - pageContentTop) + px.footer);
          pageNum += 1;
          const brk = document.createElement("div");
          brk.className = "page-break";
          brk.contentEditable = "false";
          brk.dataset.page = String(pageNum);
          brk.style.height = PAGE_GAP_PX + "px";
          editor.insertBefore(brk, block);
          insertPageFiller(block, px.header); // new page's own header margin (white)
          pageContentTop = brk.getBoundingClientRect().bottom + px.header;
          rect = block.getBoundingClientRect(); // re-measure: insertions shifted it down
        }
        prevBottom = rect.bottom;
      }
      // Last page's footer is already real (editor's own CSS padding-bottom);
      // only the leftover content area needs padding here.
      if (blocks.length > 0) insertPageFiller(null, pageContentHeight - (prevBottom - pageContentTop));

      renderMarginGuides();
    } finally {
      paginating = false;
      paginationObserver.observe(editor, { childList: true, subtree: true, characterData: true });
    }
  }

  const paginationObserver = new MutationObserver(() => {
    // A mutation (e.g. deleting the paragraph a hovered snippet lived in)
    // may have detached the image the resize handles are currently
    // tracking -- drop them rather than leaving stale handles on screen.
    if (hoveredImg && !hoveredImg.isConnected) hideResizeHandles();
    schedulePaginate();
  });
  paginationObserver.observe(editor, { childList: true, subtree: true, characterData: true });
  // Images load asynchronously, so a block's height is wrong until they do.
  editor.addEventListener(
    "load",
    (e) => {
      if (e.target.tagName === "IMG") schedulePaginate();
    },
    true
  );
  window.addEventListener("resize", schedulePaginate);

  // .page-break is contentEditable="false", but that alone doesn't stop a
  // click from landing a caret in the nearest real text — preventing the
  // mousedown's default keeps the inter-page gap fully inert to clicks.
  editor.addEventListener("mousedown", (e) => {
    if (e.target.classList && e.target.classList.contains("page-break")) {
      e.preventDefault();
    }
  });

  function getCleanEditorHtml() {
    const clone = editor.cloneNode(true);
    clone.querySelectorAll(".page-break, .page-filler").forEach((el) => el.remove());
    return clone.innerHTML;
  }

  // ---------------------------------------------------------------------
  // Inserted snippet figures: no caption text (it used to get left behind
  // when a drag only picked up the <img>, not its <figcaption> sibling),
  // and sized (see loadSnippets below) to match how large the snippet
  // actually was on the source page — not the PNG's raw pixel dimensions,
  // which are rasterized at a higher DPI than the page is displayed at
  // and so look too big at native size. Otherwise plain, fully-editable
  // content: dragging to reposition and the main toolbar's Align left/
  // center/right buttons are the browser's native contenteditable
  // behavior, not custom JS (a grab/grabbing cursor and hover-to-resize
  // handles are layered on top of that below, but neither changes what
  // gets dragged). An earlier version made the figure
  // contenteditable="false" + draggable="true" to fix the caption-left-
  // behind bug, but native HTML5 drag-and-drop over a contenteditable
  // region doesn't reliably remove the drag source, which turned into a
  // worse bug (dragging a snippet could leave duplicate copies behind).
  // Dropping the caption fixes the original bug on its own, without
  // needing the figure to be non-editable at all.
  // ---------------------------------------------------------------------

  // Cleans up `.doc-figure` snippets left over from that in-between
  // approach (and drops legacy captions from further back), so an older
  // saved document displays the same plain, native-editable figure this
  // app inserts today. Idempotent and safe to call on every load.
  function cleanLegacyDocFigures() {
    editor.querySelectorAll(".doc-figure").forEach((figure) => {
      figure.querySelectorAll("figcaption, .resize-handle, .figure-align-toolbar").forEach((el) => el.remove());
      figure.removeAttribute("contenteditable");
      figure.removeAttribute("draggable");
      figure.classList.remove("align-center", "align-right");
      figure.style.removeProperty("width");
      figure.querySelectorAll("img").forEach((img) => img.style.removeProperty("max-width"));
    });
  }

  // Grab/grabbing cursor on a snippet's native contenteditable drag: purely
  // cosmetic, toggled off the browser's own dragstart/dragend so it can't
  // affect which node the drag actually moves.
  editor.addEventListener("dragstart", (e) => {
    if (e.target.tagName === "IMG" && e.target.closest(".doc-figure")) {
      e.target.classList.add("dragging");
    }
  });
  editor.addEventListener("dragend", (e) => {
    if (e.target.tagName === "IMG") e.target.classList.remove("dragging");
  });

  // ---------------------------------------------------------------------
  // Snippet resize handles: shown on hover, positioned in the
  // #figureResizeOverlay layer (a sibling of #editor, same idea as
  // #marginGuides above) rather than as DOM children of the figure -- see
  // the .figure-resize-overlay comment in style.css for why they can't
  // live inside the figure without risking the old drag-leaves-something-
  // behind bug. Dragging a handle just writes width/height directly onto
  // the <img>'s own inline style, the same style attribute loadSnippets()
  // already sets on insert.
  // ---------------------------------------------------------------------
  const RESIZE_CORNERS = ["nw", "ne", "sw", "se"];
  const resizeHandleEls = {};
  RESIZE_CORNERS.forEach((corner) => {
    const h = document.createElement("span");
    h.className = `resize-handle resize-handle-${corner}`;
    h.addEventListener("pointerdown", (e) => startFigureResize(e, corner));
    figureResizeOverlay.appendChild(h);
    resizeHandleEls[corner] = h;
  });

  let hoveredImg = null;
  let resizingImg = null;
  let hideHandlesTimer = null;

  function positionResizeHandles(img) {
    const wrapRect = editorPageWrap.getBoundingClientRect();
    const r = img.getBoundingClientRect();
    const corners = {
      nw: [r.left, r.top],
      ne: [r.right, r.top],
      sw: [r.left, r.bottom],
      se: [r.right, r.bottom],
    };
    for (const corner of RESIZE_CORNERS) {
      const [x, y] = corners[corner];
      resizeHandleEls[corner].style.left = x - wrapRect.left + "px";
      resizeHandleEls[corner].style.top = y - wrapRect.top + "px";
    }
  }

  function showResizeHandles(img) {
    hoveredImg = img;
    positionResizeHandles(img);
    RESIZE_CORNERS.forEach((corner) => (resizeHandleEls[corner].style.display = "block"));
  }

  function hideResizeHandles() {
    hoveredImg = null;
    RESIZE_CORNERS.forEach((corner) => (resizeHandleEls[corner].style.display = "none"));
  }

  function cancelHideHandles() {
    if (hideHandlesTimer) {
      clearTimeout(hideHandlesTimer);
      hideHandlesTimer = null;
    }
  }

  // Debounced so moving the pointer from the image onto a handle (a
  // separate element, overlapping only visually) doesn't hide the handles
  // out from under the pointer before it arrives.
  function scheduleHideHandles() {
    cancelHideHandles();
    hideHandlesTimer = setTimeout(() => {
      if (!resizingImg) hideResizeHandles();
    }, 100);
  }

  editor.addEventListener("mouseover", (e) => {
    const img = e.target.closest && e.target.closest(".doc-figure img");
    if (img) {
      cancelHideHandles();
      showResizeHandles(img);
    }
  });
  editor.addEventListener("mouseout", (e) => {
    if (e.target.closest && e.target.closest(".doc-figure img")) scheduleHideHandles();
  });
  figureResizeOverlay.addEventListener("mouseover", (e) => {
    if (e.target.classList.contains("resize-handle")) cancelHideHandles();
  });
  figureResizeOverlay.addEventListener("mouseout", (e) => {
    if (e.target.classList.contains("resize-handle")) scheduleHideHandles();
  });

  function startFigureResize(e, corner) {
    if (!hoveredImg) return;
    e.preventDefault();
    e.stopPropagation();
    const img = hoveredImg;
    resizingImg = img;
    const handle = e.currentTarget;
    const startRect = img.getBoundingClientRect();
    const aspect = startRect.width / (startRect.height || 1);
    const startX = e.clientX;
    const signX = corner.endsWith("e") ? 1 : -1;
    const MIN_SIZE = 24;

    function onMove(ev) {
      const newWidth = Math.max(MIN_SIZE, startRect.width + (ev.clientX - startX) * signX);
      img.style.width = newWidth + "px";
      img.style.height = newWidth / aspect + "px";
      positionResizeHandles(img);
      schedulePaginate();
    }
    function onUp(ev) {
      handle.releasePointerCapture(ev.pointerId);
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      resizingImg = null;
      positionResizeHandles(img);
      markDirty();
    }
    handle.setPointerCapture(e.pointerId);
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  }

  // Keep handles glued to the image across repagination, window resize, and
  // scrolling the (overflow:auto) editor container.
  window.addEventListener("resize", () => {
    if (hoveredImg) positionResizeHandles(hoveredImg);
  });
  document.querySelector(".editor-container").addEventListener("scroll", () => {
    if (hoveredImg) positionResizeHandles(hoveredImg);
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

  // ---------------------------------------------------------------------
  // Page setup (margins)
  // ---------------------------------------------------------------------
  function clampMargin(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    return Math.min(200, Math.max(0, n));
  }

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
  // Autosave
  // ---------------------------------------------------------------------
  function markDirty() {
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
      html: getCleanEditorHtml(),
      source_doc,
      source_type,
      margins,
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

  window.addEventListener("beforeunload", (e) => {
    if (!dirty) return;
    // best-effort flush; browsers no longer allow a confirmation dialog reliably,
    // so just try to get the latest state persisted before the page goes away.
    try {
      const { source_doc, source_type } = currentSource();
      const blob = new Blob(
        [JSON.stringify({ name: titleInput.value.trim() || "Untitled document", html: getCleanEditorHtml(), source_doc, source_type, margins })],
        { type: "application/json" }
      );
      navigator.sendBeacon(reportUrl, blob);
    } catch (err) {
      // ignore — best effort only
    }
  });

  // ---------------------------------------------------------------------
  // Formatting toolbar
  // ---------------------------------------------------------------------
  let savedRange = null;
  function saveSelection() {
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0) {
      const r = sel.getRangeAt(0);
      if (editor.contains(r.commonAncestorContainer)) savedRange = r.cloneRange();
    }
  }
  editor.addEventListener("keyup", saveSelection);
  editor.addEventListener("mouseup", saveSelection);
  editor.addEventListener("blur", saveSelection);

  function restoreSelection() {
    editor.focus();
    const sel = window.getSelection();
    sel.removeAllRanges();
    if (savedRange) {
      sel.addRange(savedRange);
    } else {
      const r = document.createRange();
      r.selectNodeContents(editor);
      r.collapse(false);
      sel.addRange(r);
    }
  }

  editor.addEventListener("input", markDirty);

  document.querySelectorAll(".fmt-btn[data-cmd]").forEach((btn) => {
    btn.addEventListener("click", () => {
      restoreSelection();
      document.execCommand(btn.dataset.cmd, false, btn.dataset.arg || null);
      saveSelection();
      markDirty();
    });
  });

  const fontFamilySelect = document.getElementById("fontFamilySelect");
  fontFamilySelect.addEventListener("change", () => {
    restoreSelection();
    document.execCommand("fontName", false, fontFamilySelect.value);
    saveSelection();
    markDirty();
  });

  const fontSizeSelect = document.getElementById("fontSizeSelect");
  fontSizeSelect.addEventListener("change", () => {
    restoreSelection();
    // execCommand("fontSize") only accepts the legacy 1-7 scale. Mark the
    // selection with size 7, then normalize whatever the browser produced for
    // it into a span with a real pt size: with styleWithCSS enabled, Chrome
    // emits <span style="font-size:xxx-large"> directly; other engines still
    // emit legacy <font size="7"> tags (which our sanitizer would strip).
    document.execCommand("fontSize", false, "7");
    const touched = [];
    editor.querySelectorAll('font[size="7"]').forEach((f) => {
      const span = document.createElement("span");
      while (f.firstChild) span.appendChild(f.firstChild);
      f.replaceWith(span);
      touched.push(span);
    });
    editor.querySelectorAll('span[style*="xxx-large"]').forEach((s) => touched.push(s));
    touched.forEach((s) => {
      s.style.fontSize = fontSizeSelect.value + "pt";
    });
    // Replacing font->span invalidates the live selection, so re-select the
    // new span(s) — otherwise a color/bold applied right after this would
    // silently land on whatever the collapsed cursor happens to be near,
    // not the text the user just resized.
    if (touched.length) {
      const range = document.createRange();
      range.setStartBefore(touched[0]);
      range.setEndAfter(touched[touched.length - 1]);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    saveSelection();
    markDirty();
  });

  const blockFormatSelect = document.getElementById("blockFormatSelect");
  blockFormatSelect.addEventListener("change", () => {
    restoreSelection();
    document.execCommand("formatBlock", false, blockFormatSelect.value);
    saveSelection();
    markDirty();
  });

  const textColorInput = document.getElementById("textColorInput");
  const textColorSwatch = document.getElementById("textColorSwatch");
  textColorSwatch.style.background = textColorInput.value;
  textColorInput.addEventListener("input", () => {
    restoreSelection();
    document.execCommand("foreColor", false, textColorInput.value);
    textColorSwatch.style.background = textColorInput.value;
    saveSelection();
    markDirty();
  });

  const highlightColorInput = document.getElementById("highlightColorInput");
  const highlightColorSwatch = document.getElementById("highlightColorSwatch");
  highlightColorSwatch.style.background = highlightColorInput.value;
  function applyHighlight(color) {
    restoreSelection();
    if (!document.execCommand("hiliteColor", false, color)) {
      document.execCommand("backColor", false, color);
    }
    saveSelection();
    markDirty();
  }
  highlightColorInput.addEventListener("input", () => {
    highlightColorSwatch.style.background = highlightColorInput.value;
    applyHighlight(highlightColorInput.value);
  });
  document.getElementById("clearHighlightBtn").addEventListener("click", () => {
    applyHighlight("transparent");
  });

  // ---------------------------------------------------------------------
  // Source picker (which document's snippets show in the sidebar)
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

  // Snippet PNGs are rasterized at a fixed export DPI (300, see
  // api_create_snippet in app.py) that's higher than the ~150dpi the
  // source page itself is rendered at for on-screen viewing -- so the
  // PNG's raw pixel dimensions render roughly 2x too large if dropped in
  // at native size. The physically-correct size is independent of either
  // of those DPI values: a snippet's fractional rect (x/y/w/h, 0-1) times
  // its source page's real dimensions (in points, from /info) gives its
  // true size, which converts to CSS px the same way margins do elsewhere
  // in this file (PT_TO_PX = 96/72) -- that's "the same size it was in
  // the source," matching the page as it's rendered at 96dpi.
  const PT_TO_PX_SNIPPET = 96 / 72;

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
          let sizeStyle = "";
          if (pageInfo && s.rect) {
            const w = s.rect.w * pageInfo.width * PT_TO_PX_SNIPPET;
            const h = s.rect.h * pageInfo.height * PT_TO_PX_SNIPPET;
            sizeStyle = ` style="width:${w.toFixed(1)}px;height:${h.toFixed(1)}px;"`;
          }
          const html =
            `<figure class="doc-figure"><img src="${escapeHtml(s.url)}" alt="${label} from page ${s.page}"${sizeStyle}></figure><p><br></p>`;
          restoreSelection();
          document.execCommand("insertHTML", false, html);
          saveSelection();
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

  // ---------------------------------------------------------------------
  // Initial load
  // ---------------------------------------------------------------------
  async function loadReport() {
    try {
      const res = await fetch(reportUrl);
      const data = await res.json();
      titleInput.value = data.name || "";
      margins = normalizeMargins(data.margins);
      applyMarginsToCss();
      editor.innerHTML = data.html || "";
      cleanLegacyDocFigures();
      const combo = data.source_doc ? `${data.source_doc}|${data.source_type}` : "";
      sourceSelect.value = combo;
      if (sourceSelect.value !== combo) sourceSelect.value = "";
      updateAnnotateLink();
      loadSnippets();
      dirty = false;
      setStatus("");
    } catch (e) {
      setStatus("Failed to load document: " + e.message, true);
    }
  }

  loadReport();
})();
