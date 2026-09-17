(function () {
  const appEl = document.getElementById("allegationsApp");
  if (!appEl) return;
  const allegationsUrl = appEl.dataset.allegationsUrl;
  const allegationUrlBase = appEl.dataset.allegationUrlBase;
  const allegationsOrderUrl = appEl.dataset.allegationsOrderUrl;
  const casesUrl = appEl.dataset.casesUrl;
  const casesPageUrl = appEl.dataset.casesPageUrl;
  const filterCaseId = appEl.dataset.filterCase || "";

  function allegationUrl(id) {
    return allegationUrlBase.replace("__ID__", encodeURIComponent(id));
  }

  const saveStatusEl = document.getElementById("saveStatus");
  const addAllegationBtn = document.getElementById("addAllegationBtn");
  const filterBarEl = document.getElementById("filterBar");
  const allegationListEl = document.getElementById("allegationList");
  const allegationListEmpty = document.getElementById("allegationListEmpty");
  const allegationDetailEl = document.getElementById("allegationDetail");

  // -------------------------------------------------------------------
  // Small helpers (mirrors static/cases.js)
  // -------------------------------------------------------------------
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function genId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "").slice(0, 12);
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }

  function wireConfirmDelete(btn, onConfirm) {
    btn.addEventListener("click", (e) => {
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

  function setStatus(text, isError) {
    saveStatusEl.textContent = text || "";
    saveStatusEl.style.color = isError ? "#c0392b" : "#8a92a5";
  }

  // ---------------------------------------------------------------------
  // Drag-to-reorder — plain HTML5 drag and drop. `onDrop` runs once, after
  // the drag finishes, so it can read the final order straight back out of
  // the DOM instead of tracking indices during every dragover.
  // ---------------------------------------------------------------------
  function dragAfterElement(container, selector, y) {
    const els = [...container.querySelectorAll(`${selector}:not(.dragging)`)];
    let closest = { offset: Number.NEGATIVE_INFINITY, element: null };
    for (const el of els) {
      const box = el.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) closest = { offset, element: el };
    }
    return closest.element;
  }

  function enableDragReorder(listEl, selector, onDrop) {
    let draggingEl = null;
    listEl.addEventListener("dragstart", (e) => {
      const card = e.target.closest(selector);
      if (!card) return;
      draggingEl = card;
      setTimeout(() => card.classList.add("dragging"), 0);
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.dataset.id || "");
    });
    listEl.addEventListener("dragover", (e) => {
      if (!draggingEl) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const afterEl = dragAfterElement(listEl, selector, e.clientY);
      if (afterEl == null) listEl.appendChild(draggingEl);
      else listEl.insertBefore(draggingEl, afterEl);
    });
    listEl.addEventListener("dragend", () => {
      if (draggingEl) draggingEl.classList.remove("dragging");
      draggingEl = null;
      onDrop();
    });
  }

  // ---------------------------------------------------------------------
  // State: allegations are global records, each optionally linked to one or
  // more cases (allegation.case_ids) rather than owned by a single case.
  // ---------------------------------------------------------------------
  let allegations = [];
  let cases = []; // [{id, name, ...}] for the link picker + filter label
  let selectedId = null;
  let filterActive = !!filterCaseId;
  const saveTimers = {};

  function caseName(id) {
    const c = cases.find((x) => x.id === id);
    return c ? c.name || id : id;
  }

  function visibleAllegations() {
    if (!filterActive) return allegations;
    return allegations.filter((a) => (a.case_ids || []).includes(filterCaseId));
  }

  function renderFilterBar() {
    if (!filterCaseId) {
      filterBarEl.style.display = "none";
      return;
    }
    filterBarEl.style.display = "block";
    filterBarEl.innerHTML = filterActive
      ? `Showing allegations linked to <strong>${escapeHtml(caseName(filterCaseId))}</strong> &middot; <a href="#" id="showAllLink">Show all</a>`
      : `Showing all allegations &middot; <a href="#" id="showAllLink">Show only ${escapeHtml(caseName(filterCaseId))}</a>`;
    const link = filterBarEl.querySelector("#showAllLink");
    if (link) {
      link.addEventListener("click", (e) => {
        e.preventDefault();
        filterActive = !filterActive;
        renderFilterBar();
        renderAllegationList();
      });
    }
  }

  function scheduleSave(id) {
    setStatus("Saving…");
    if (saveTimers[id]) clearTimeout(saveTimers[id]);
    saveTimers[id] = setTimeout(() => {
      saveTimers[id] = null;
      saveAllegation(id).catch((e) => setStatus("Save failed: " + e.message, true));
    }, 900);
  }

  async function saveAllegation(id) {
    const allegation = allegations.find((a) => a.id === id);
    if (!allegation) return;
    const payload = {
      title: allegation.title,
      description: allegation.description,
      inculpatory: allegation.inculpatory,
      exculpatory: allegation.exculpatory,
      case_ids: allegation.case_ids,
    };
    const res = await fetch(allegationUrl(id), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await res.text());
    setStatus("Saved");
  }

  // ---------------------------------------------------------------------
  // Allegation list (left column)
  // ---------------------------------------------------------------------
  function evidenceCountLabel(allegation) {
    const inc = allegation.inculpatory.length;
    const exc = allegation.exculpatory.length;
    const base = `${inc} inculpatory · ${exc} exculpatory`;
    const links = allegation.case_ids || [];
    if (!links.length) return base;
    return `${base} · Linked: ${links.map(caseName).join(", ")}`;
  }

  function updateAllegationCardMeta(id) {
    const allegation = allegations.find((a) => a.id === id);
    const card = allegationListEl.querySelector(`.allegation-card[data-id="${id}"]`);
    if (allegation && card) {
      card.querySelector(".allegation-card-meta").textContent = evidenceCountLabel(allegation);
    }
  }

  function buildAllegationCard(allegation) {
    const card = document.createElement("div");
    card.className = "allegation-card" + (allegation.id === selectedId ? " selected" : "");
    card.draggable = true;
    card.dataset.id = allegation.id;
    card.innerHTML = `
      <div class="allegation-card-head">
        <span class="drag-handle" title="Drag to reorder">⠿</span>
        <input type="text" class="allegation-title-input" placeholder="Allegation title" maxlength="300">
        <button type="button" class="card-delete-btn" title="Delete allegation">✕</button>
      </div>
      <textarea class="allegation-summary-input" rows="2" placeholder="Brief description of the allegation…" maxlength="10000"></textarea>
      <div class="allegation-card-meta"></div>`;

    const titleEl = card.querySelector(".allegation-title-input");
    titleEl.value = allegation.title;
    titleEl.addEventListener("input", () => {
      allegation.title = titleEl.value;
      if (selectedId === allegation.id) {
        const h2 = allegationDetailEl.querySelector(".allegation-detail-head h2");
        if (h2) h2.textContent = allegation.title || "Untitled allegation";
      }
      scheduleSave(allegation.id);
    });

    const summaryEl = card.querySelector(".allegation-summary-input");
    summaryEl.value = allegation.description;
    summaryEl.addEventListener("input", () => {
      allegation.description = summaryEl.value;
      scheduleSave(allegation.id);
    });

    card.querySelector(".allegation-card-meta").textContent = evidenceCountLabel(allegation);

    card.addEventListener("click", (e) => {
      if (e.target.closest("input, textarea, button")) return;
      selectAllegation(allegation.id);
    });

    wireConfirmDelete(card.querySelector(".card-delete-btn"), () => deleteAllegation(allegation.id));

    return card;
  }

  function renderAllegationList() {
    const visible = visibleAllegations();
    allegationListEl.innerHTML = "";
    allegationListEmpty.style.display = visible.length ? "none" : "block";
    for (const allegation of visible) {
      allegationListEl.appendChild(buildAllegationCard(allegation));
    }
  }

  function reorderAllegationsFromDom() {
    const ids = [...allegationListEl.querySelectorAll(".allegation-card")].map((el) => el.dataset.id);
    // Only the visible (possibly filtered) subset was reordered; splice that
    // subset back into `allegations` in its new order, leaving the rest
    // (hidden by the filter) exactly where they were.
    const idSet = new Set(ids);
    const reordered = ids.map((id) => allegations.find((a) => a.id === id));
    let cursor = 0;
    allegations = allegations.map((a) => (idSet.has(a.id) ? reordered[cursor++] : a));
    saveOrder();
  }
  enableDragReorder(allegationListEl, ".allegation-card", reorderAllegationsFromDom);

  function saveOrder() {
    setStatus("Saving…");
    fetch(allegationsOrderUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order: allegations.map((a) => a.id) }),
    })
      .then((res) => {
        if (!res.ok) throw new Error(res.statusText);
        setStatus("Saved");
      })
      .catch((e) => setStatus("Save failed: " + e.message, true));
  }

  function selectAllegation(id) {
    selectedId = id;
    renderAllegationList();
    renderDetail();
  }

  function addAllegation() {
    const payload = {
      title: "",
      description: "",
      inculpatory: [],
      exculpatory: [],
      // Creating from a case-filtered view links the new allegation to that
      // case immediately, since that's almost always the intent.
      case_ids: filterCaseId ? [filterCaseId] : [],
    };
    setStatus("Saving…");
    fetch(allegationsUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((res) => {
        if (!res.ok) throw new Error(res.statusText);
        return res.json();
      })
      .then((data) => {
        allegations.push(data);
        selectedId = data.id;
        renderAllegationList();
        renderDetail();
        setStatus("Saved");
        const card = allegationListEl.querySelector(`.allegation-card[data-id="${data.id}"]`);
        if (card) setTimeout(() => card.querySelector(".allegation-title-input").focus(), 0);
      })
      .catch((e) => setStatus("Could not create allegation: " + e.message, true));
  }
  addAllegationBtn.addEventListener("click", addAllegation);

  function deleteAllegation(id) {
    fetch(allegationUrl(id), { method: "DELETE" })
      .then((res) => {
        if (!res.ok) throw new Error(res.statusText);
        allegations = allegations.filter((a) => a.id !== id);
        if (selectedId === id) selectedId = null;
        renderAllegationList();
        renderDetail();
      })
      .catch((e) => setStatus("Could not delete allegation: " + e.message, true));
  }

  // ---------------------------------------------------------------------
  // Evidence sublists (part of the detail pane, one allegation at a time)
  // ---------------------------------------------------------------------
  function buildEvidenceCard(allegation, kind, item) {
    const card = document.createElement("div");
    card.className = "evidence-card";
    card.draggable = true;
    card.dataset.id = item.id;
    card.innerHTML = `
      <span class="drag-handle" title="Drag to reorder">⠿</span>
      <textarea rows="2" placeholder="Describe this evidence…" maxlength="10000"></textarea>
      <button type="button" class="card-delete-btn" title="Delete">✕</button>`;

    const textEl = card.querySelector("textarea");
    textEl.value = item.text;
    textEl.addEventListener("input", () => {
      item.text = textEl.value;
      scheduleSave(allegation.id);
    });

    wireConfirmDelete(card.querySelector(".card-delete-btn"), () => {
      allegation[kind] = allegation[kind].filter((e) => e.id !== item.id);
      renderDetail();
      updateAllegationCardMeta(allegation.id);
      scheduleSave(allegation.id);
    });

    return card;
  }

  function buildEvidenceColumn(allegation, kind, label) {
    const col = document.createElement("div");
    col.className = `evidence-column evidence-${kind}`;
    col.innerHTML = `
      <div class="evidence-column-head">
        <h3>${escapeHtml(label)}</h3>
        <button type="button" class="btn add-evidence-btn">+ Add evidence</button>
      </div>
      <p class="empty evidence-empty" style="display:none">None added yet.</p>
      <div class="evidence-list"></div>`;

    const listEl = col.querySelector(".evidence-list");
    const emptyEl = col.querySelector(".evidence-empty");
    emptyEl.style.display = allegation[kind].length ? "none" : "block";
    for (const item of allegation[kind]) listEl.appendChild(buildEvidenceCard(allegation, kind, item));

    function reorderFromDom() {
      const ids = [...listEl.querySelectorAll(".evidence-card")].map((el) => el.dataset.id);
      allegation[kind].sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
      scheduleSave(allegation.id);
    }
    enableDragReorder(listEl, ".evidence-card", reorderFromDom);

    col.querySelector(".add-evidence-btn").addEventListener("click", () => {
      const item = { id: genId(), text: "" };
      allegation[kind].push(item);
      renderDetail();
      updateAllegationCardMeta(allegation.id);
      scheduleSave(allegation.id);
      const newCard = allegationDetailEl.querySelector(
        `.evidence-column.evidence-${kind} .evidence-card[data-id="${item.id}"] textarea`
      );
      if (newCard) setTimeout(() => newCard.focus(), 0);
    });

    return col;
  }

  // ---------------------------------------------------------------------
  // Linked cases picker
  // ---------------------------------------------------------------------
  function buildCaseLinks(allegation) {
    const wrap = document.createElement("div");
    wrap.className = "case-links";
    if (!cases.length) {
      wrap.innerHTML = `<h3>Linked cases</h3><p class="empty">No cases yet. Create one in the <a href="${casesPageUrl}">Cases</a> workspace.</p>`;
      return wrap;
    }
    const linked = new Set(allegation.case_ids || []);
    const rows = cases
      .map(
        (c) => `
      <label class="case-link-row">
        <input type="checkbox" value="${escapeHtml(c.id)}" ${linked.has(c.id) ? "checked" : ""}>
        ${escapeHtml(c.name || c.id)}
      </label>`
      )
      .join("");
    wrap.innerHTML = `<h3>Linked cases</h3><div class="case-link-list">${rows}</div>`;
    wrap.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
      cb.addEventListener("change", () => {
        const set = new Set(allegation.case_ids || []);
        if (cb.checked) set.add(cb.value);
        else set.delete(cb.value);
        allegation.case_ids = [...set];
        updateAllegationCardMeta(allegation.id);
        scheduleSave(allegation.id);
      });
    });
    return wrap;
  }

  function renderDetail() {
    const allegation = allegations.find((a) => a.id === selectedId);
    if (!allegation) {
      allegationDetailEl.innerHTML = '<p class="empty">Select or add an allegation on the left to see its evidence.</p>';
      return;
    }
    allegationDetailEl.innerHTML = `
      <div class="allegation-detail-head">
        <h2>${escapeHtml(allegation.title || "Untitled allegation")}</h2>
      </div>
      <div class="evidence-columns"></div>`;
    const columnsEl = allegationDetailEl.querySelector(".evidence-columns");
    columnsEl.appendChild(buildEvidenceColumn(allegation, "inculpatory", "Inculpatory Evidence"));
    columnsEl.appendChild(buildEvidenceColumn(allegation, "exculpatory", "Exculpatory Evidence"));
    allegationDetailEl.appendChild(buildCaseLinks(allegation));
  }

  // ---------------------------------------------------------------------
  // Initial load
  // ---------------------------------------------------------------------
  (async function init() {
    try {
      const [allegationsRes, casesRes] = await Promise.all([fetch(allegationsUrl), fetch(casesUrl)]);
      if (!allegationsRes.ok) throw new Error(await allegationsRes.text());
      allegations = await allegationsRes.json();
      cases = casesRes.ok ? await casesRes.json() : [];
      renderFilterBar();
      renderAllegationList();
      renderDetail();
    } catch (e) {
      setStatus("Failed to load: " + e.message, true);
    }
  })();
})();
