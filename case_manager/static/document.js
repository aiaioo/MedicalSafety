(function () {
  const appEl = document.getElementById("docApp");
  const docId = appEl.dataset.doc;
  const docType = appEl.dataset.type;
  const reportUrl = appEl.dataset.reportUrl;
  const exportUrl = appEl.dataset.exportUrl;
  const snippetsUrl = appEl.dataset.snippetsUrl;

  if (!docId) return; // empty state (no documents at all): nothing to wire up

  const editor = document.getElementById("editor");
  const titleInput = document.getElementById("titleInput");
  const statusEl = document.getElementById("reportStatus");
  const snippetListEl = document.getElementById("reportSnippetList");
  const sourceSelect = document.getElementById("sourceSelect");
  const saveBtn = document.getElementById("saveReportBtn");
  const exportBtn = document.getElementById("exportBtn");

  let dirty = false;

  function setStatus(msg, isError) {
    statusEl.textContent = msg || "";
    statusEl.style.color = isError ? "#c0392b" : "#2a7a2a";
    if (msg) {
      setTimeout(() => {
        if (statusEl.textContent === msg) statusEl.textContent = "";
      }, 3000);
    }
  }

  function markDirty() {
    dirty = true;
  }

  // ---- formatting toolbar ----
  document.querySelectorAll(".fmt-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      editor.focus();
      document.execCommand(btn.dataset.cmd, false, btn.dataset.arg || null);
      markDirty();
    });
  });

  editor.addEventListener("input", markDirty);
  titleInput.addEventListener("input", markDirty);

  // ---- preserve cursor position across clicks on the sidebar ----
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

  function insertHtmlAtCursor(html) {
    restoreSelection();
    document.execCommand("insertHTML", false, html);
    saveSelection();
    markDirty();
  }

  function escapeAttr(s) {
    return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  }

  // ---- report load/save/export ----
  async function loadReport() {
    try {
      const res = await fetch(reportUrl);
      const data = await res.json();
      titleInput.value = data.title || "";
      editor.innerHTML = data.html || "";
      dirty = false;
    } catch (e) {
      setStatus("Failed to load document: " + e.message, true);
    }
  }

  async function saveReport() {
    const res = await fetch(reportUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: titleInput.value, html: editor.innerHTML }),
    });
    if (!res.ok) throw new Error(await res.text());
    dirty = false;
  }

  saveBtn.addEventListener("click", async () => {
    try {
      await saveReport();
      setStatus("Saved");
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  });

  exportBtn.addEventListener("click", async () => {
    try {
      await saveReport();
      window.location.href = exportUrl;
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  });

  window.addEventListener("beforeunload", (e) => {
    if (dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  // ---- source document switcher ----
  if (sourceSelect) {
    sourceSelect.addEventListener("change", () => {
      if (dirty && !confirm("Discard unsaved changes and switch source document?")) {
        sourceSelect.value = `${docId}|${docType}`;
        return;
      }
      const [id, type] = sourceSelect.value.split("|");
      window.location.href = `/document?doc=${encodeURIComponent(id)}&type=${encodeURIComponent(type)}`;
    });
  }

  // ---- snippets & annotations sidebar ----
  async function loadSnippets() {
    try {
      const res = await fetch(snippetsUrl);
      const list = await res.json();
      snippetListEl.innerHTML = "";
      if (!list.length) {
        snippetListEl.innerHTML =
          '<p class="empty">No snippets or annotations yet. Create some on the <a href="' +
          `/annotations?doc=${encodeURIComponent(docId)}&type=${encodeURIComponent(docType)}&page=1` +
          '">annotation page</a>, then come back here.</p>';
        return;
      }
      for (const s of list) {
        const label = s.annotated ? "Annotation" : "Snippet";
        const card = document.createElement("div");
        card.className = "snippet-card";
        card.innerHTML = `
          <img src="${s.url}" alt="${label} from page ${s.page}">
          <div class="snippet-actions">
            <span class="tag">${label} · p${s.page}</span>
            <button type="button" class="insert-snippet">Insert</button>
          </div>`;
        card.querySelector(".insert-snippet").addEventListener("click", () => {
          const html =
            `<figure class="doc-figure"><img src="${escapeAttr(s.url)}" alt="${label} from page ${s.page}">` +
            `<figcaption>${label} — page ${s.page}</figcaption></figure><p><br></p>`;
          insertHtmlAtCursor(html);
          setStatus("Inserted");
        });
        snippetListEl.appendChild(card);
      }
    } catch (e) {
      console.error(e);
      setStatus("Failed to load snippets: " + e.message, true);
    }
  }

  loadReport();
  loadSnippets();
})();
