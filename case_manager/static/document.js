(function () {
  const appEl = document.getElementById("docApp");
  const reportId = appEl.dataset.report;
  const reportsUrl = appEl.dataset.reportsUrl;
  const reportUrl = appEl.dataset.reportUrl;
  const exportUrl = appEl.dataset.exportUrl;
  const preselectSource = appEl.dataset.preselectSource;
  const preselectType = appEl.dataset.preselectType || "pdf";

  const editor = document.getElementById("editor");
  const titleInput = document.getElementById("titleInput");
  const saveStatusEl = document.getElementById("saveStatus");
  const snippetListEl = document.getElementById("reportSnippetList");
  const sourceSelect = document.getElementById("sourceSelect");
  const annotateLink = document.getElementById("annotateLink");

  const fileMenuBtn = document.getElementById("fileMenuBtn");
  const fileMenuDropdown = document.getElementById("fileMenuDropdown");
  const newDocBtn = document.getElementById("newDocBtn");
  const openDocBtn = document.getElementById("openDocBtn");
  const downloadPdfBtn = document.getElementById("downloadPdfBtn");

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

  downloadPdfBtn.addEventListener("click", async () => {
    try {
      await saveReport();
      window.location.href = exportUrl;
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
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
      html: editor.innerHTML,
      source_doc,
      source_type,
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
        [JSON.stringify({ name: titleInput.value.trim() || "Untitled document", html: editor.innerHTML, source_doc, source_type })],
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

  async function loadSnippets() {
    const { source_doc, source_type } = currentSource();
    if (!source_doc) {
      snippetListEl.innerHTML = '<p class="empty">Pick a source document above to see its snippets here.</p>';
      return;
    }
    try {
      const res = await fetch(`/api/doc/${encodeURIComponent(source_doc)}/snippets?type=${encodeURIComponent(source_type)}`);
      const list = await res.json();
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
          const html =
            `<figure class="doc-figure"><img src="${escapeHtml(s.url)}" alt="${label} from page ${s.page}">` +
            `<figcaption>${label} — page ${s.page}</figcaption></figure><p><br></p>`;
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

  // ---------------------------------------------------------------------
  // Initial load
  // ---------------------------------------------------------------------
  async function loadReport() {
    try {
      const res = await fetch(reportUrl);
      const data = await res.json();
      titleInput.value = data.name || "";
      editor.innerHTML = data.html || "";
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
