(function () {
  const appEl = document.getElementById("app");
  const url = appEl.dataset.annexureUrl;
  const container = document.getElementById("viewerContainer");
  const listEl = document.getElementById("annexList");
  const addSelect = document.getElementById("addSelect");
  const addBtn = document.getElementById("addBtn");
  const statusMsg = document.getElementById("statusMsg");

  let state = { documents: [], available: [] };
  const infoCache = new Map(); // "id|type" -> Promise of /info pages
  let observer = null;
  let dragId = null;

  function esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function setStatus(msg, isError) {
    statusMsg.textContent = msg || "";
    statusMsg.style.color = isError ? "#c0392b" : "#2a7a2a";
    if (msg && !isError) setTimeout(() => { if (statusMsg.textContent === msg) statusMsg.textContent = ""; }, 2000);
  }

  function fetchInfo(d) {
    const key = d.id + "|" + d.type;
    if (!infoCache.has(key)) {
      infoCache.set(key, fetch(`/api/doc/${encodeURIComponent(d.id)}/info?type=${encodeURIComponent(d.type)}`)
        .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }));
    }
    return infoCache.get(key);
  }

  // Pages of `d` that are part of the annexure: all of them, or (toggle off,
  // only possible for a document with snippets in the report) just the pages
  // the snippets were taken from.
  function pagesFor(d, pageCount) {
    if (d.all_pages) return Array.from({ length: pageCount }, (_, i) => i + 1);
    return d.snippet_pages.filter((p) => p <= pageCount);
  }

  // ---- main panel: continuous scroll of every annexed document's pages ----
  async function renderPages() {
    if (observer) observer.disconnect();
    observer = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        const img = e.target.querySelector("img[data-src]");
        if (img) { img.src = img.dataset.src; img.removeAttribute("data-src"); }
        observer.unobserve(e.target);
      }
    }, { root: container, rootMargin: "600px 0px" });

    container.innerHTML = "";
    if (!state.documents.length) {
      container.innerHTML = '<p class="empty" style="color:#e7ebf3">No documents in this annexure yet.</p>';
      return;
    }
    const version = ++renderPages.version;
    for (const d of state.documents) {
      const heading = document.createElement("div");
      heading.className = "annex-doc-heading";
      heading.dataset.doc = d.id;
      heading.textContent = d.title;
      container.appendChild(heading);
      let pages;
      try {
        pages = (await fetchInfo(d)).pages;
      } catch (e) {
        heading.insertAdjacentHTML("afterend", '<p class="empty" style="color:#f5b7b1">Failed to load this document.</p>');
        continue;
      }
      if (version !== renderPages.version) return; // superseded by a newer render
      for (const n of pagesFor(d, pages.length)) {
        const dim = pages[n - 1];
        const wrap = document.createElement("div");
        wrap.className = "page-wrap";
        wrap.style.width = Math.round(dim.width * 96 / 72 * 1.25) + "px";
        wrap.style.aspectRatio = `${dim.width} / ${dim.height}`;
        wrap.style.maxWidth = "100%";
        wrap.style.background = "white";
        wrap.innerHTML = `<span class="page-label">${esc(d.title)} &middot; page ${n}</span>` +
          `<img alt="${esc(d.title)} page ${n}" data-src="/api/doc/${encodeURIComponent(d.id)}/render/${n}?type=${encodeURIComponent(d.type)}">`;
        container.appendChild(wrap);
        observer.observe(wrap);
      }
    }
  }
  renderPages.version = 0;

  // ---- right panel ----
  function renderList() {
    listEl.innerHTML = "";
    if (!state.documents.length) listEl.innerHTML = '<p class="empty">Nothing annexed yet.</p>';
    for (const d of state.documents) {
      const item = document.createElement("div");
      item.className = "annex-item";
      item.draggable = true;
      item.dataset.doc = d.id;
      const pages = d.snippet_pages.length ? `snippets on p${d.snippet_pages.join(", p")}` : "added manually";
      item.innerHTML = `<span class="annex-grip" aria-hidden="true">&#8942;&#8942;</span>` +
        `<span class="annex-name"><a href="#" class="annex-jump" title="Scroll to this document">${esc(d.title)}</a>` +
        `<span class="report-card-meta">${esc(pages)}</span>` +
        `<label class="toggle-label" title="Off: only the pages the report's snippets were taken from">` +
        `<input type="checkbox" class="annex-all-pages" ${d.all_pages ? "checked" : ""} ${d.snippet_pages.length ? "" : "disabled"}> Include all pages</label></span>` +
        `<button type="button" class="annex-delete" ${d.locked ? "disabled" : ""} ` +
        `title="${d.locked ? "Snippets from this document are used in the report" : "Remove from annexure"}" aria-label="Remove ${esc(d.title)}">` +
        `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4h11"/><path d="M6 4V2.5h4V4"/><path d="M4 4l.7 9.5h6.6L12 4"/><path d="M6.5 6.5v5M9.5 6.5v5"/></svg></button>`;
      item.querySelector(".annex-jump").addEventListener("click", (e) => {
        e.preventDefault();
        const h = container.querySelector(`.annex-doc-heading[data-doc="${CSS.escape(d.id)}"]`);
        if (h) container.scrollTo({ top: h.offsetTop - container.offsetTop - 12, behavior: "smooth" });
      });
      item.querySelector(".annex-all-pages").addEventListener("change", (e) => {
        d.all_pages = e.target.checked;
        commit();
      });
      item.querySelector(".annex-delete").addEventListener("click", () => {
        if (d.locked) return;
        state.documents = state.documents.filter((x) => x.id !== d.id);
        state.available.push({ id: d.id, title: d.title, type: d.type });
        commit();
      });
      item.addEventListener("dragstart", (e) => {
        dragId = d.id;
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", d.id);
        item.classList.add("dragging");
      });
      item.addEventListener("dragend", () => {
        dragId = null;
        item.classList.remove("dragging");
        listEl.querySelectorAll(".drop-before, .drop-after").forEach((el) => el.classList.remove("drop-before", "drop-after"));
      });
      item.addEventListener("dragover", (e) => {
        if (!dragId || dragId === d.id) return;
        e.preventDefault();
        const r = item.getBoundingClientRect();
        const before = e.clientY < r.top + r.height / 2;
        item.classList.toggle("drop-before", before);
        item.classList.toggle("drop-after", !before);
      });
      item.addEventListener("dragleave", () => item.classList.remove("drop-before", "drop-after"));
      item.addEventListener("drop", (e) => {
        if (!dragId || dragId === d.id) return;
        e.preventDefault();
        const before = item.classList.contains("drop-before");
        const moving = state.documents.find((x) => x.id === dragId);
        state.documents = state.documents.filter((x) => x.id !== dragId);
        const at = state.documents.findIndex((x) => x.id === d.id);
        state.documents.splice(before ? at : at + 1, 0, moving);
        commit();
      });
      listEl.appendChild(item);
    }
  }

  function renderAddPicker() {
    addSelect.innerHTML = state.available.length
      ? state.available.map((d) => `<option value="${esc(d.id)}">${esc(d.title)}</option>`).join("")
      : '<option value="">&mdash; no other documents &mdash;</option>';
    addBtn.disabled = !state.available.length;
  }

  function render(pagesToo) {
    renderList();
    renderAddPicker();
    if (pagesToo) renderPages();
  }

  async function commit() {
    render(true);
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ documents: state.documents.map((d) => ({ id: d.id, all_pages: d.all_pages })) }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || "HTTP " + res.status);
      setStatus("Saved");
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  }

  addBtn.addEventListener("click", () => {
    const d = state.available.find((x) => x.id === addSelect.value);
    if (!d) return;
    state.available = state.available.filter((x) => x.id !== d.id);
    state.documents.push({ ...d, snippet_pages: [], locked: false, all_pages: true });
    commit();
  });

  (async function init() {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error("HTTP " + res.status);
      state = await res.json();
      render(true);
    } catch (e) {
      setStatus("Failed to load annexure: " + e.message, true);
    }
  })();
})();
