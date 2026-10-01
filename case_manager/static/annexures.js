(function () {
  const appEl = document.getElementById("app");
  const url = appEl.dataset.annexureUrl;
  // Viewers can read the annexure but not change it (the server rejects their saves with a 403).
  const canEdit = !!appEl.dataset.canEdit;
  const container = document.getElementById("viewerContainer");
  const listEl = document.getElementById("annexList");
  const addSelect = document.getElementById("addSelect");
  const addBtn = document.getElementById("addBtn");
  const statusMsg = document.getElementById("statusMsg");

  const annotationRadios = document.querySelectorAll("input[name=annexAnnotations]");
  const pnPosition = document.getElementById("pageNumberPositionInput");
  const pnStart = document.getElementById("pageNumberStartInput");
  const pnFirst = document.getElementById("pageNumberFirstInput");
  const pnFont = document.getElementById("pageNumberFontInput");
  const pnSize = document.getElementById("pageNumberFontSizeInput");
  const pnShape = document.getElementById("pageNumberShapeInput");
  const dnEnabled = document.getElementById("docNumberEnabled");
  const dnName = document.getElementById("docNumberNameInput");
  const dnPrefix = document.getElementById("docNumberPrefixInput");
  const dnFirst = document.getElementById("docNumberFirstInput");
  const NUMBER_BAND_PT = 46; // header/footer band the number sits in; matches ANNEXURE_PAGE_NUMBER_BAND in app.py

  let state = { documents: [], available: [], annotations: "none", pageNumbers: { position: "none", skip: 0, first: 1, font: "Arial", fontSize: 11, shape: "none", color: "#555555" }, docNumbers: { enabled: true, name: "Annexure", prefix: "", first: 1 } };
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

  // Pages "1-3, 7" names, ascending and clipped to the document; null if invalid.
  function parseRange(text, pageCount) {
    if (!/^\s*\d+\s*(-\s*\d+\s*)?(,\s*\d+\s*(-\s*\d+\s*)?)*$/.test(text)) return null;
    const pages = new Set();
    for (const part of text.split(",")) {
      const [lo, hi = lo] = part.split("-").map((n) => parseInt(n, 10));
      if (lo < 1 || hi < lo) return null;
      for (let p = lo; p <= Math.min(hi, pageCount); p++) pages.add(p);
    }
    return [...pages].sort((a, b) => a - b);
  }

  const isValid = (d) => d.page_mode !== "custom" || parseRange(d.page_range, 1 << 30) !== null;
  const applied = (d) => (isValid(d) ? d : d.valid);
  const remember = (d) => { d.valid = { page_mode: d.page_mode, page_range: d.page_range }; };

  // Pages of `d` that are part of the annexure, per its page mode.
  function pagesFor(doc, pageCount) {
    const d = { ...doc, ...applied(doc) };
    if (d.page_mode === "snippets") return d.snippet_pages.filter((p) => p <= pageCount);
    if (d.page_mode === "custom") return parseRange(d.page_range, pageCount) || [];
    return Array.from({ length: pageCount }, (_, i) => i + 1);
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
    const pn = state.pageNumbers;
    const dn = state.docNumbers;
    let shown = 0; // annexure pages laid out so far, across documents
    let docsNumbered = 0;
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
      let firstPage = true;
      for (const n of pagesFor(d, pages.length)) {
        const dim = pages[n - 1];
        const wrap = document.createElement("div");
        wrap.className = "page-wrap";
        wrap.style.width = Math.round(dim.width * 96 / 72 * 1.25) + "px";
        wrap.style.aspectRatio = `${dim.width} / ${dim.height}`;
        wrap.style.maxWidth = "100%";
        wrap.style.background = "white";
        wrap.innerHTML = `<span class="page-label">${esc(d.title)} &middot; page ${n}</span>` +
          `<img alt="${esc(d.title)} page ${n}" data-src="/api/doc/${encodeURIComponent(d.id)}/render/${n}?type=${encodeURIComponent(d.type)}${state.annotations !== "none" ? "&annotations=" + state.annotations : ""}">`;
        shown++;
        if (pn.position !== "none" && shown > pn.skip) {
          const scale = parseFloat(wrap.style.width) / dim.width; // px per pt
          const label = document.createElement("span");
          label.className = `page-number-label page-number-${pn.position}`;
          label.style.cssText = `--mg-top:${NUMBER_BAND_PT * scale}px;--mg-bottom:${NUMBER_BAND_PT * scale}px;` +
            `--mg-left:${36 * scale}px;--mg-right:${36 * scale}px;` +
            `font-family:${pn.font},Arial,sans-serif;font-size:${pn.fontSize * scale}px;color:${pn.color};z-index:1`;
          const num = document.createElement("span");
          num.className = "page-number-outline";
          num.textContent = shown - pn.skip + (pn.first || 1) - 1;
          if (pn.shape !== "none") {
            const h = pn.fontSize * 1.6 * scale;
            num.style.cssText = `height:${h}px;min-width:${h}px;line-height:${h}px;padding:0 ${pn.fontSize * 0.3 * scale}px;` +
              `border:1px solid ${pn.color};border-radius:${pn.shape === "circle" ? "50%" : "0"}`;
          }
          label.appendChild(num);
          wrap.appendChild(label);
        }
        if (dn.enabled && firstPage) {
          const scale = parseFloat(wrap.style.width) / dim.width;
          const label = document.createElement("span");
          // top right, unless the page number is there (same band, font, size and colour as the page number)
          label.className = `page-number-label page-number-top-${pn.position === "top-right" ? "left" : "right"}`;
          label.style.cssText = `--mg-top:${NUMBER_BAND_PT * scale}px;--mg-left:${36 * scale}px;--mg-right:${36 * scale}px;` +
            `font-family:${pn.font},Arial,sans-serif;font-size:${pn.fontSize * scale}px;color:${pn.color};z-index:1`;
          label.textContent = `${dn.name} ${dn.prefix}${dn.first + docsNumbered}`;
          wrap.appendChild(label);
          docsNumbered++;
        }
        firstPage = false;
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
      item.draggable = canEdit;
      item.dataset.doc = d.id;
      const pages = d.snippet_pages.length ? `snippets on p${d.snippet_pages.join(", p")}` : "added manually";
      item.innerHTML = `<span class="annex-grip" aria-hidden="true">&#8942;&#8942;</span>` +
        `<span class="annex-name"><a href="#" class="annex-jump" title="Scroll to this document">${esc(d.title)}</a>` +
        `<span class="report-card-meta">${esc(pages)}</span>` +
        `<input type="text" class="annex-desc" maxlength="500" placeholder="Add a description&hellip;" title="Description (used in the List of Documents)" value="${esc(d.description || "")}">` +
        `<a class="annex-view" href="/annotations?doc=${encodeURIComponent(d.id)}&type=${encodeURIComponent(d.type)}&page=1">View document</a>` +
        `<select class="annex-mode" title="Which pages of this document to include">` +
        `<option value="all">Include all pages</option>` +
        `<option value="snippets" ${d.snippet_pages.length ? "" : "disabled"}>Only pages with snippets</option>` +
        `<option value="custom">Custom page range</option></select>` +
        `<input type="text" class="annex-range" placeholder="e.g. 1-3, 7" value="${esc(d.page_range)}" ${d.page_mode === "custom" ? "" : "hidden"}></span>` +
        `<button type="button" class="annex-delete" ${d.locked ? "disabled" : ""} ` +
        `title="${d.locked ? "Snippets from this document are used in the report" : "Remove from annexure"}" aria-label="Remove ${esc(d.title)}">` +
        `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4h11"/><path d="M6 4V2.5h4V4"/><path d="M4 4l.7 9.5h6.6L12 4"/><path d="M6.5 6.5v5M9.5 6.5v5"/></svg></button>`;
      item.querySelector(".annex-jump").addEventListener("click", (e) => {
        e.preventDefault();
        const h = container.querySelector(`.annex-doc-heading[data-doc="${CSS.escape(d.id)}"]`);
        if (h) container.scrollTo({ top: h.offsetTop - container.offsetTop - 12, behavior: "smooth" });
      });
      const descIn = item.querySelector(".annex-desc");
      const modeSel = item.querySelector(".annex-mode");
      const rangeIn = item.querySelector(".annex-range");
      modeSel.value = d.page_mode;
      if (!canEdit) {
        modeSel.disabled = true;
        rangeIn.disabled = true;
        descIn.readOnly = true;
        item.querySelector(".annex-delete").disabled = true;
        listEl.appendChild(item);
        continue;
      }
      modeSel.addEventListener("change", () => {
        d.page_mode = modeSel.value;
        rangeIn.hidden = d.page_mode !== "custom";
        if (d.page_mode === "custom") { rangeIn.focus(); if (!isValid(d)) return; }
        commit();
      });
      // A custom range is applied once it parses; until then the last valid
      // selection stays in effect (and is what gets saved).
      rangeIn.addEventListener("input", () => {
        d.page_range = rangeIn.value;
        rangeIn.classList.toggle("invalid", !isValid(d));
      });
      rangeIn.addEventListener("change", () => { if (isValid(d)) commit(); });
      rangeIn.addEventListener("keydown", (e) => { if (e.key === "Enter") rangeIn.blur(); });
      rangeIn.addEventListener("focus", () => { item.draggable = false; });
      rangeIn.addEventListener("blur", () => { item.draggable = true; });
      // Saved on the document itself (not the annexure), so it also shows in the annotator.
      descIn.addEventListener("change", async () => {
        try {
          const res = await fetch(`/api/doc/${encodeURIComponent(d.id)}/description`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ description: descIn.value }),
          });
          if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || "HTTP " + res.status);
          d.description = (await res.json()).description;
          descIn.value = d.description;
          setStatus("Saved");
        } catch (e) {
          setStatus("Save failed: " + e.message, true);
        }
      });
      descIn.addEventListener("keydown", (e) => { if (e.key === "Enter") descIn.blur(); });
      descIn.addEventListener("focus", () => { item.draggable = false; });
      descIn.addEventListener("blur", () => { item.draggable = true; });
      item.querySelector(".annex-delete").addEventListener("click", () => {
        if (d.locked) return;
        state.documents = state.documents.filter((x) => x.id !== d.id);
        state.available.push({ id: d.id, title: d.title, type: d.type, description: d.description });
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
    addBtn.disabled = !canEdit || !state.available.length;
    addSelect.disabled = !canEdit;
  }

  function renderPageNumberInputs() {
    const pn = state.pageNumbers;
    annotationRadios.forEach((r) => { r.checked = r.value === state.annotations; });
    pnPosition.value = pn.position;
    pnStart.value = pn.skip + 1;
    pnFirst.value = pn.first || 1;
    pnFont.value = pn.font;
    pnSize.value = pn.fontSize;
    pnShape.value = pn.shape;
    markSelectedSwatch();
    const dn = state.docNumbers;
    dnEnabled.checked = dn.enabled;
    dnName.value = dn.name;
    dnPrefix.value = dn.prefix;
    dnFirst.value = dn.first;
    for (const el of [dnName, dnPrefix, dnFirst]) el.disabled = !canEdit || !dn.enabled;
  }

  const markSelectedSwatch = () => selectSwatch(state.pageNumbers.color);
  const selectSwatch = PageNumberPalette.mountPopover("pageNumberColor", (c) => {
    state.pageNumbers = { ...state.pageNumbers, color: c };
    markSelectedSwatch();
    commit();
  });

  // Applied as soon as every field is valid, like the per-document controls.
  function readPageNumbers() {
    const start = parseInt(pnStart.value, 10);
    const size = parseFloat(pnSize.value);
    const first = parseInt(pnFirst.value, 10);
    const firstOk = first >= 1 && first <= 100000;
    const ok = start >= 1 && start <= 51 && size >= 6 && size <= 72 && firstOk;
    pnFirst.classList.toggle("invalid", !firstOk);
    pnStart.classList.toggle("invalid", !(start >= 1 && start <= 51));
    pnSize.classList.toggle("invalid", !(size >= 6 && size <= 72));
    if (!ok) return;
    state.pageNumbers = { position: pnPosition.value, skip: start - 1, first, font: pnFont.value, fontSize: size, shape: pnShape.value, color: state.pageNumbers.color };
    commit();
  }
  function readDocNumbers() {
    const first = parseInt(dnFirst.value, 10);
    const firstOk = first >= 1 && first <= 100000;
    dnFirst.classList.toggle("invalid", !firstOk);
    if (!firstOk) return;
    state.docNumbers = { enabled: dnEnabled.checked, name: dnName.value, prefix: dnPrefix.value.trim(), first };
    for (const el of [dnName, dnPrefix, dnFirst]) el.disabled = !canEdit || !dnEnabled.checked;
    commit();
  }
  [dnEnabled, dnName, dnPrefix, dnFirst].forEach((el) => el.addEventListener("change", readDocNumbers));

  annotationRadios.forEach((r) => r.addEventListener("change", () => {
    if (!r.checked) return;
    state.annotations = r.value;
    commit();
  }));
  [pnPosition, pnStart, pnFirst, pnFont, pnSize, pnShape].forEach((el) => el.addEventListener("change", readPageNumbers));

  function render(pagesToo) {
    renderList();
    renderAddPicker();
    if (pagesToo) renderPages();
  }

  async function commit() {
    state.documents.forEach((d) => { if (isValid(d)) remember(d); });
    render(true);
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ documents: state.documents.map((d) => {
          const a = applied(d);
          return { id: d.id, page_mode: a.page_mode, page_range: a.page_range };
        }), pageNumbers: state.pageNumbers, docNumbers: state.docNumbers, annotations: state.annotations }),
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
    state.documents.push({ ...d, description: d.description || "", snippet_pages: [], locked: false, page_mode: "all", page_range: "" });
    commit();
  });

  if (!canEdit) {
    document.querySelectorAll(".sidebar select, .sidebar input, .sidebar .color-pick-btn").forEach((el) => {
      el.disabled = true;
    });
  }

  (async function init() {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error("HTTP " + res.status);
      state = await res.json();
      state.documents.forEach(remember);
      renderPageNumberInputs();
      render(true);
    } catch (e) {
      setStatus("Failed to load annexure: " + e.message, true);
    }
  })();
})();
