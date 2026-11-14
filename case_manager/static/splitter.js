// The document splitter: File > Split document... on the annotations page opens a narrow strip down the left of
// the screen where the user picks the pages a document is cut at. It stays in front of the document (and the
// snippets panel) and stays open, across page loads, until its x is clicked.
//
// Each number is the first page of a new document; page 1 always starts the first, so it needs no card unless
// the user adds one. Splitting is done by the server (see splitter.py): this file only collects the pages, asks
// for confirmation, starts the job and follows it. While it runs the document is locked, here and on the server.
//
// Works with viewer.js through window.AnnotatorHooks (go to a page in either display mode, save unsaved
// annotations first, lock the page's editing controls).
(function () {
  "use strict";
  const appEl = document.getElementById("app");
  const hooks = window.AnnotatorHooks;
  const menuBtn = document.getElementById("splitDocBtn");
  if (!appEl || !hooks || !menuBtn) return;

  const docId = hooks.docId;
  const pageCount = hooks.pageCount;
  const splitUrl = appEl.dataset.splitUrl + "?type=" + encodeURIComponent(appEl.dataset.type || "pdf");
  const canSplit = !!appEl.dataset.canSplit;
  const OPEN_KEY = "annotator:splitterOpen";
  const POINTS_KEY = "annotator:splitPoints:" + docId;
  const POLL_MS = 1500;

  function lsGet(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function lsSet(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (e) { /* storage unavailable: the strip just forgets its state on reload */ }
  }

  // ---- state ----
  let points = loadPoints(); // [{ id, page }] in the order the user arranged them
  let nextId = points.reduce((m, p) => Math.max(m, p.id), 0) + 1;
  let phase = "edit"; // edit | confirm | working | done
  let editingId = null;
  let result = null; // the server's description of a finished split
  let sawActive = false; // this page saw a split under way (so it can tell a rolled-back one from none)
  let pollTimer = null;
  let dragId = null;
  let strip = null;
  const el = {};

  function loadPoints() {
    try {
      const raw = JSON.parse(lsGet(POINTS_KEY) || "[]");
      const seen = new Set();
      const out = [];
      for (const p of Array.isArray(raw) ? raw : []) {
        if (Number.isInteger(p.page) && p.page >= 1 && p.page <= pageCount && !seen.has(p.page)) {
          seen.add(p.page);
          out.push({ id: out.length + 1, page: p.page });
        }
      }
      return out;
    } catch (e) {
      return [];
    }
  }
  function savePoints() {
    lsSet(POINTS_KEY, points.length ? JSON.stringify(points) : null);
  }

  // ---- pure helpers ----
  function parts() {
    const starts = Array.from(new Set([1].concat(points.map((p) => p.page)))).sort((a, b) => a - b);
    return starts.map((s, i) => ({ start: s, end: i + 1 < starts.length ? starts[i + 1] - 1 : pageCount }));
  }
  function range(p) {
    return p.start === p.end ? "" + p.start : p.start + "–" + p.end;
  }
  function problem(value, exceptId) {
    if (!Number.isInteger(value)) return "Enter a whole page number.";
    if (value < 1) return "Page numbers start at 1.";
    if (value > pageCount) return "This document has only " + pageCount + (pageCount === 1 ? " page." : " pages.");
    if (points.some((p) => p.id !== exceptId && p.page === value)) return "Page " + value + " is already in the list.";
    return "";
  }
  function parseNumber(text) {
    const t = String(text).trim();
    return t === "" ? null : Number(t);
  }
  function h(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (v === true) node.setAttribute(k, "");
      else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v);
    }
    for (const c of children || []) node.appendChild(c);
    return node;
  }

  // ---- building the strip ----
  function build() {
    el.close = h("button", { type: "button", class: "splitter-close", "aria-label": "Close the splitter", title: "Close", text: "✕" });
    el.close.addEventListener("click", close);
    el.addInput = h("input", { type: "number", min: 1, max: pageCount, step: 1, placeholder: "Page", "aria-label": "Page to split at" });
    el.addBtn = h("button", { type: "submit", class: "splitter-add-btn", title: "Add a page where a new document starts (the current page if the box is empty)", text: "Add" });
    el.addForm = h("form", { class: "splitter-add" }, [el.addInput, el.addBtn]);
    el.addForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const typed = parseNumber(el.addInput.value);
      addPage(typed === null ? hooks.getCurrentPage() : typed);
    });
    el.error = h("p", { class: "splitter-error", role: "alert" });
    el.list = h("ul", { class: "splitter-list" });
    el.list.addEventListener("dragover", onDragOver);
    el.list.addEventListener("drop", onDrop);
    el.summary = h("div", { class: "splitter-summary" });
    el.foot = h("div", { class: "splitter-foot" });
    strip = h("aside", { class: "splitter", "aria-label": "Split document" }, [
      h("div", { class: "splitter-head" }, [h("h3", { text: "Split document" }), el.close]),
      h("p", { class: "splitter-hint", text: "Each page number is the start of a new segment. Page 1 always starts the first one." }),
      el.addForm,
      el.error,
      el.list,
      el.summary,
      el.foot,
    ]);
    document.body.appendChild(strip);
    placeStrip();
    render();
  }

  // The strip starts below the toolbar, so the title bar and menus stay reachable.
  function placeStrip() {
    if (!strip) return;
    const layout = document.querySelector(".viewer-layout");
    strip.style.top = (layout ? Math.max(0, layout.getBoundingClientRect().top) : 0) + "px";
  }
  window.addEventListener("resize", placeStrip);
  if (window.ResizeObserver) {
    const layout = document.querySelector(".viewer-layout");
    if (layout) new ResizeObserver(placeStrip).observe(layout);
  }

  function showError(message) {
    if (el.error) el.error.textContent = message || "";
  }

  // ---- the list of page numbers ----
  function addPage(value) {
    const msg = problem(value, null);
    if (msg) return showError(msg);
    showError("");
    points.push({ id: nextId++, page: value });
    savePoints();
    el.addInput.value = "";
    render();
  }

  function removePage(id) {
    points = points.filter((p) => p.id !== id);
    if (editingId === id) editingId = null;
    showError("");
    savePoints();
    render();
  }

  function commitEdit(id, input) {
    if (editingId !== id) return;
    const value = parseNumber(input.value);
    const msg = value === null ? "Enter a whole page number." : problem(value, id);
    if (msg) {
      showError(msg);
      input.focus();
      return;
    }
    points.find((p) => p.id === id).page = value;
    editingId = null;
    showError("");
    savePoints();
    render();
  }

  function cancelEdit() {
    editingId = null;
    showError("");
    render();
  }

  function move(id, delta) {
    const i = points.findIndex((p) => p.id === id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= points.length) return;
    const [item] = points.splice(i, 1);
    points.splice(j, 0, item);
    savePoints();
    render();
    const again = el.list.querySelector('[data-id="' + id + '"] .splitter-num');
    if (again) again.focus();
  }

  function card(p) {
    const li = h("li", { class: "splitter-card", "data-id": p.id, draggable: editingId === null && phase === "edit" ? "true" : false });
    li.appendChild(h("span", { class: "splitter-grip", "aria-hidden": "true", text: "⋮⋮", title: "Drag to reorder" }));
    if (editingId === p.id) {
      const input = h("input", { type: "number", min: 1, max: pageCount, step: 1, value: p.page, "aria-label": "Page number" });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); commitEdit(p.id, input); }
        else if (e.key === "Escape") { e.preventDefault(); cancelEdit(); }
      });
      input.addEventListener("blur", () => setTimeout(() => commitEdit(p.id, input), 0));
      li.appendChild(input);
      setTimeout(() => { input.focus(); input.select(); }, 0);
    } else {
      const num = h("button", { type: "button", class: "splitter-num", title: "Go to page " + p.page + " (Alt+Up or Down moves this card)", text: "Page " + p.page });
      num.addEventListener("click", () => hooks.goToPage(p.page));
      num.addEventListener("keydown", (e) => {
        if (e.altKey && e.key === "ArrowUp") { e.preventDefault(); move(p.id, -1); }
        else if (e.altKey && e.key === "ArrowDown") { e.preventDefault(); move(p.id, 1); }
      });
      const edit = h("button", { type: "button", class: "splitter-icon", title: "Change this page number", "aria-label": "Edit page " + p.page, text: "✎" });
      edit.addEventListener("click", () => { editingId = p.id; showError(""); render(); });
      li.appendChild(num);
      li.appendChild(edit);
    }
    const del = h("button", { type: "button", class: "splitter-icon", title: "Remove this split point", "aria-label": "Remove page " + p.page, text: "✕" });
    del.addEventListener("click", () => removePage(p.id));
    li.appendChild(del);
    li.addEventListener("dragstart", (e) => {
      dragId = p.id;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(p.id));
      li.classList.add("dragging");
    });
    li.addEventListener("dragend", endDrag);
    return li;
  }

  // Drag to reorder: the card goes in front of the first card whose middle is below the pointer.
  function dropTarget(clientY) {
    for (const li of el.list.querySelectorAll(".splitter-card:not(.dragging)")) {
      const r = li.getBoundingClientRect();
      if (clientY < r.top + r.height / 2) return li;
    }
    return null;
  }
  function onDragOver(e) {
    if (dragId === null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    clearMarks();
    const before = dropTarget(e.clientY);
    if (before) before.classList.add("drop-before");
    else el.list.classList.add("drop-end");
  }
  function onDrop(e) {
    if (dragId === null) return;
    e.preventDefault();
    const before = dropTarget(e.clientY);
    const beforeId = before ? Number(before.dataset.id) : null;
    const dragged = points.find((p) => p.id === dragId);
    const others = points.filter((p) => p.id !== dragId);
    others.splice(beforeId === null ? others.length : others.findIndex((p) => p.id === beforeId), 0, dragged);
    points = others;
    endDrag();
    savePoints();
    render();
  }
  function clearMarks() {
    el.list.classList.remove("drop-end");
    el.list.querySelectorAll(".drop-before").forEach((n) => n.classList.remove("drop-before"));
  }
  function endDrag() {
    dragId = null;
    clearMarks();
    el.list.querySelectorAll(".dragging").forEach((n) => n.classList.remove("dragging"));
  }

  // ---- drawing ----
  function render() {
    if (!strip) return;
    const editable = phase === "edit" || phase === "confirm";
    el.addForm.hidden = el.list.hidden = el.summary.hidden = !(phase === "edit" || phase === "confirm");
    strip.querySelector(".splitter-hint").hidden = phase === "working" || phase === "done";
    el.addInput.disabled = el.addBtn.disabled = !editable || phase === "confirm";
    el.close.disabled = phase === "working";
    el.close.title = phase === "working" ? "The strip can be closed once the split has finished" : "Close";

    el.list.textContent = "";
    for (const p of points) el.list.appendChild(card(p));
    if (!points.length) el.list.appendChild(h("li", { class: "splitter-empty", text: "No split pages yet. Add the first page of each new document." }));

    const ps = parts();
    el.summary.textContent = "";
    if (ps.length > 1) {
      el.summary.appendChild(h("div", { class: "splitter-summary-title", text: ps.length + " documents (in page order)" }));
      ps.forEach((p, i) => el.summary.appendChild(h("div", { class: "splitter-part", text: "Split " + (i + 1) + ": page" + (p.start === p.end ? " " : "s ") + range(p) })));
    }
    renderFoot(ps);
  }

  function renderFoot(ps) {
    el.foot.textContent = "";
    if (phase === "edit") {
      const btn = h("button", { type: "button", class: "splitter-split", text: "Split", disabled: ps.length < 2,
        title: ps.length < 2 ? "Add at least one page after page 1" : "Split the document at these pages" });
      btn.addEventListener("click", () => { editingId = null; showError(""); phase = "confirm"; render(); });
      el.foot.appendChild(btn);
    } else if (phase === "confirm") {
      const yes = h("button", { type: "button", class: "splitter-confirm-yes", text: "Yes, split" });
      const no = h("button", { type: "button", class: "splitter-confirm-no", text: "Cancel" });
      yes.addEventListener("click", startSplit);
      no.addEventListener("click", () => { phase = "edit"; render(); });
      el.foot.appendChild(h("div", { class: "splitter-confirm", role: "alertdialog" }, [
        h("p", { text: "Split into " + ps.length + " documents? Annotations and snippets move to the new documents. This can’t be undone." }),
        h("div", { class: "splitter-confirm-actions" }, [yes, no]),
      ]));
    } else if (phase === "working") {
      el.foot.appendChild(h("div", { class: "splitter-working", role: "status" }, [
        h("p", { text: "Splitting… this document is locked until it finishes." }),
        h("p", { class: "splitter-note", text: "You can leave this page; the split carries on." }),
      ]));
    } else if (phase === "done" && result) {
      const links = result.documents.map((d) => {
        const a = h("a", { href: d.url, text: d.title, title: d.title });
        return h("li", {}, [a, h("span", { class: "splitter-pages", text: " (page" + (d.start === d.end ? " " : "s ") + range(d) + ")" })]);
      });
      el.foot.appendChild(h("div", { class: "splitter-done", role: "status" }, [
        h("p", { class: "splitter-done-title", text: "Split into " + result.documents.length + " documents" }),
        h("ul", {}, links),
        h("p", { class: "splitter-note", text: "This original is kept, without its annotations and snippets. Reports now point to the new documents." }),
      ]));
    }
  }

  // ---- splitting ----
  async function startSplit() {
    showError("");
    phase = "working";
    hooks.setLocked(true);
    render();
    try {
      await hooks.saveAllDirty(); // unsaved annotations are saved first, so they move with their pages
      const res = await fetch(splitUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ points: points.map((p) => p.page) }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || res.statusText);
      sawActive = true;
      follow(body);
      startPolling();
    } catch (e) {
      phase = "edit";
      hooks.setLocked(false);
      showError("Not split: " + e.message);
      render();
    }
  }

  function follow(status) {
    if (status.state === "building" || status.state === "committed") {
      sawActive = true;
      phase = "working";
      hooks.setLocked(true);
    } else if (status.state === "done") {
      stopPolling();
      phase = "done";
      result = status;
      points = [];
      savePoints();
      hooks.setLocked(true); // what this page shows is out of date now; closing the strip reloads it
    } else if (sawActive) {
      stopPolling();
      sawActive = false;
      phase = "edit";
      hooks.setLocked(false);
      showError("The split did not finish and was rolled back. Nothing was changed.");
    } else if (phase === "working") {
      phase = "edit";
    }
    render();
  }

  async function checkStatus() {
    try {
      const res = await fetch(splitUrl);
      if (!res.ok) return;
      const status = await res.json();
      follow(status);
      if (status.state === "building" || status.state === "committed") startPolling();
    } catch (e) { /* a transient failure: the next poll tries again */ }
  }

  function startPolling() {
    if (pollTimer === null) pollTimer = setInterval(checkStatus, POLL_MS);
  }
  function stopPolling() {
    if (pollTimer !== null) clearInterval(pollTimer);
    pollTimer = null;
  }

  // ---- opening and closing ----
  function open() {
    if (!strip) build();
    strip.hidden = false;
    lsSet(OPEN_KEY, "1");
    placeStrip();
    checkStatus();
  }

  function close() {
    if (phase === "working") return;
    lsSet(OPEN_KEY, null);
    if (phase === "done") {
      location.reload(); // the page was showing the document as it was before the split
      return;
    }
    stopPolling();
    if (strip) strip.hidden = true;
  }

  menuBtn.addEventListener("click", open);
  if (appEl.dataset.splitting || (canSplit && lsGet(OPEN_KEY) === "1")) open();
})();
