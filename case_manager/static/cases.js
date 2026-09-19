(function () {
  const appEl = document.getElementById("casesApp");
  if (!appEl) return;
  const casesUrl = appEl.dataset.casesUrl;
  const caseUrlBase = appEl.dataset.caseUrlBase;
  const allegationsUrlBase = appEl.dataset.allegationsUrlBase;

  function caseUrl(id) {
    return caseUrlBase.replace("__ID__", encodeURIComponent(id));
  }

  const saveStatusEl = document.getElementById("saveStatus");
  const addCaseBtn = document.getElementById("addCaseBtn");
  const caseListEl = document.getElementById("caseList");
  const caseListEmpty = document.getElementById("caseListEmpty");
  const caseDetailEl = document.getElementById("caseDetail");

  // -------------------------------------------------------------------
  // Small helpers (mirrors static/allegations.js)
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

  // Debounced partial saves, keyed independently so a card edit (name/
  // summary) and a detail-pane edit (court/case_number/hearings) for the
  // same case never clobber each other's in-flight timer.
  const saveTimers = {};
  function scheduleSave(key, id, partial) {
    setStatus("Saving…");
    if (saveTimers[key]) clearTimeout(saveTimers[key]);
    saveTimers[key] = setTimeout(() => {
      saveTimers[key] = null;
      savePartial(id, partial).catch((e) => setStatus("Save failed: " + e.message, true));
    }, 900);
  }

  async function savePartial(id, partial) {
    const res = await fetch(caseUrl(id), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(partial),
    });
    if (!res.ok) throw new Error(await res.text());
    setStatus("Saved");
    return res.json();
  }

  // Briefly highlights a field right after Enter forces its save, since the
  // header's save-status text is easy to miss while eyes are on the field.
  function flashSaved(el) {
    el.classList.remove("save-flash");
    void el.offsetWidth; // restart the animation if it's already running
    el.classList.add("save-flash");
    el.addEventListener("animationend", () => el.classList.remove("save-flash"), { once: true });
  }

  function isSaveShortcut(e) {
    return e.key === "Enter" || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s");
  }

  // Pressing Enter, or Ctrl/Cmd+S, in a field should save right away instead
  // of waiting out the debounce. `buildPartial` is called at flush time (not
  // capture time) so it picks up the keystroke's own "input" event, e.g. a
  // textarea's freshly-inserted newline. Deferred via setTimeout so that
  // event has a chance to run first; Ctrl/Cmd+S has no such side effect, but
  // the browser's own save-page shortcut must still be suppressed either way.
  function flushSaveOnEnter(el, key, id, buildPartial) {
    el.addEventListener("keydown", (e) => {
      if (!isSaveShortcut(e)) return;
      if (e.key !== "Enter") e.preventDefault();
      setTimeout(() => {
        if (!saveTimers[key]) return;
        clearTimeout(saveTimers[key]);
        saveTimers[key] = null;
        savePartial(id, buildPartial())
          .then(() => flashSaved(el))
          .catch((err) => setStatus("Save failed: " + err.message, true));
      }, 0);
    });
  }

  // ---------------------------------------------------------------------
  // Drag-to-reorder (mirrors static/allegations.js)
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
  // Case list (left column)
  // ---------------------------------------------------------------------
  let cases = [];
  let selectedId = null;

  function caseMetaLabel(c) {
    const idBits = [c.court || "No court", c.case_number || "No case #"].join(" · ");
    const hearingLabel = `${c.hearing_count} hearing${c.hearing_count === 1 ? "" : "s"}`;
    return `${idBits} · ${hearingLabel}`;
  }

  function buildCaseCard(c) {
    const card = document.createElement("div");
    card.className = "allegation-card case-card" + (c.id === selectedId ? " selected" : "");
    card.dataset.id = c.id;
    card.innerHTML = `
      <div class="allegation-card-head">
        <input type="text" class="allegation-title-input" placeholder="Case name" maxlength="200">
        ${c.hearing_count === 0 ? '<button type="button" class="card-delete-btn" title="Delete case">✕</button>' : ""}
      </div>
      <textarea class="allegation-summary-input" rows="2" placeholder="Brief case summary…" maxlength="10000"></textarea>
      <div class="allegation-card-meta"></div>`;

    const titleEl = card.querySelector(".allegation-title-input");
    titleEl.value = c.name;
    titleEl.addEventListener("input", () => {
      c.name = titleEl.value;
      if (selectedId === c.id) caseDetailEl.querySelector(".allegation-detail-head h2").textContent = c.name || "Untitled case";
      scheduleSave("card:" + c.id, c.id, { name: c.name, summary: c.summary });
    });
    flushSaveOnEnter(titleEl, "card:" + c.id, c.id, () => ({ name: c.name, summary: c.summary }));

    const summaryEl = card.querySelector(".allegation-summary-input");
    summaryEl.value = c.summary;
    summaryEl.addEventListener("input", () => {
      c.summary = summaryEl.value;
      scheduleSave("card:" + c.id, c.id, { name: c.name, summary: c.summary });
    });
    flushSaveOnEnter(summaryEl, "card:" + c.id, c.id, () => ({ name: c.name, summary: c.summary }));

    card.querySelector(".allegation-card-meta").textContent = caseMetaLabel(c);

    const deleteBtn = card.querySelector(".card-delete-btn");
    if (deleteBtn) wireConfirmDelete(deleteBtn, () => deleteCase(c.id));

    card.addEventListener("click", (e) => {
      if (e.target.closest("input, textarea, button")) return;
      selectCase(c.id);
    });

    return card;
  }

  function deleteCase(id) {
    fetch(caseUrl(id), { method: "DELETE" })
      .then((res) => {
        if (!res.ok) throw new Error(res.statusText);
        cases = cases.filter((c) => c.id !== id);
        if (selectedId === id) {
          selectedId = null;
          detailCase = null;
          renderDetail();
        }
        renderCaseList();
      })
      .catch((e) => setStatus("Could not delete case: " + e.message, true));
  }

  function renderCaseList() {
    caseListEl.innerHTML = "";
    caseListEmpty.style.display = cases.length ? "none" : "block";
    for (const c of cases) caseListEl.appendChild(buildCaseCard(c));
  }

  async function loadCases() {
    try {
      const res = await fetch(casesUrl);
      cases = await res.json();
      renderCaseList();
    } catch (e) {
      console.error(e);
    }
  }

  addCaseBtn.addEventListener("click", async () => {
    addCaseBtn.disabled = true;
    try {
      const res = await fetch(casesUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "Untitled case" }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      cases.unshift({
        id: data.id,
        name: data.name,
        court: data.court,
        case_number: data.case_number,
        summary: data.summary,
        allegation_count: 0,
        hearing_count: 0,
        created_at: data.created_at,
        updated_at: data.updated_at,
      });
      renderCaseList();
      selectCase(data.id);
      const card = caseListEl.querySelector(`.case-card[data-id="${data.id}"]`);
      if (card) setTimeout(() => card.querySelector(".allegation-title-input").focus(), 0);
    } catch (e) {
      setStatus("Could not create case: " + e.message, true);
    } finally {
      addCaseBtn.disabled = false;
    }
  });

  // ---------------------------------------------------------------------
  // Case detail (right pane): court / case number + hearings
  // ---------------------------------------------------------------------
  let detailCase = null; // full record for the selected case, incl. hearings

  function hearingListLabel(h) {
    return h.date ? h.date : "No date";
  }

  function buildHearingCard(hearing) {
    const card = document.createElement("div");
    card.className = "evidence-card hearing-card";
    card.draggable = true;
    card.dataset.id = hearing.id;
    card.innerHTML = `
      <span class="drag-handle" title="Drag to reorder">⠿</span>
      <div class="hearing-fields">
        <input type="text" class="hearing-date-input" placeholder="Date (e.g. 2026-03-05)" maxlength="40">
        <textarea rows="2" placeholder="What happened at this hearing…" maxlength="10000"></textarea>
      </div>
      <button type="button" class="card-delete-btn" title="Delete">✕</button>`;

    const dateEl = card.querySelector(".hearing-date-input");
    dateEl.value = hearing.date;
    dateEl.addEventListener("input", () => {
      hearing.date = dateEl.value;
      saveDetail();
    });
    flushSaveOnEnter(dateEl, "detail:" + detailCase.id, detailCase.id, () => ({
      court: detailCase.court,
      case_number: detailCase.case_number,
      hearings: detailCase.hearings,
    }));

    const summaryEl = card.querySelector("textarea");
    summaryEl.value = hearing.summary;
    summaryEl.addEventListener("input", () => {
      hearing.summary = summaryEl.value;
      saveDetail();
    });
    flushSaveOnEnter(summaryEl, "detail:" + detailCase.id, detailCase.id, () => ({
      court: detailCase.court,
      case_number: detailCase.case_number,
      hearings: detailCase.hearings,
    }));

    wireConfirmDelete(card.querySelector(".card-delete-btn"), () => {
      detailCase.hearings = detailCase.hearings.filter((h) => h.id !== hearing.id);
      renderHearingList();
      updateSelectedCaseMeta();
      saveDetail();
    });

    return card;
  }

  function renderHearingList() {
    const listEl = caseDetailEl.querySelector(".hearing-list");
    const emptyEl = caseDetailEl.querySelector(".hearing-empty");
    if (!listEl) return;
    listEl.innerHTML = "";
    emptyEl.style.display = detailCase.hearings.length ? "none" : "block";
    for (const h of detailCase.hearings) listEl.appendChild(buildHearingCard(h));
  }

  function reorderHearingsFromDom() {
    const listEl = caseDetailEl.querySelector(".hearing-list");
    const ids = [...listEl.querySelectorAll(".hearing-card")].map((el) => el.dataset.id);
    detailCase.hearings.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
    saveDetail();
  }

  function saveDetail() {
    if (!detailCase) return;
    scheduleSave("detail:" + detailCase.id, detailCase.id, {
      court: detailCase.court,
      case_number: detailCase.case_number,
      hearings: detailCase.hearings,
    });
  }

  function updateSelectedCaseMeta() {
    const c = cases.find((x) => x.id === selectedId);
    if (!c) return;
    c.court = detailCase.court;
    c.case_number = detailCase.case_number;
    c.hearing_count = detailCase.hearings.length;
    const card = caseListEl.querySelector(`.case-card[data-id="${c.id}"]`);
    if (!card) return;
    card.querySelector(".allegation-card-meta").textContent = caseMetaLabel(c);
    const head = card.querySelector(".allegation-card-head");
    const existingBtn = head.querySelector(".card-delete-btn");
    if (c.hearing_count === 0 && !existingBtn) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "card-delete-btn";
      btn.title = "Delete case";
      btn.textContent = "✕";
      head.appendChild(btn);
      wireConfirmDelete(btn, () => deleteCase(c.id));
    } else if (c.hearing_count !== 0 && existingBtn) {
      existingBtn.remove();
    }
  }

  function renderDetail() {
    if (!detailCase) {
      caseDetailEl.innerHTML = '<p class="empty">Select or add a case on the left to see its hearings.</p>';
      return;
    }
    caseDetailEl.innerHTML = `
      <div class="allegation-detail-head">
        <h2>${escapeHtml(detailCase.name || "Untitled case")}</h2>
        <a class="btn" href="${allegationsUrlBase}?case=${encodeURIComponent(detailCase.id)}">Open allegations &rarr;</a>
      </div>
      <div class="case-identity-fields">
        <label class="modal-field">Court
          <input type="text" id="courtInput" maxlength="200" placeholder="e.g. Superior Court of California">
        </label>
        <label class="modal-field">Case number
          <input type="text" id="caseNumberInput" maxlength="100" placeholder="e.g. CV-2026-00123">
        </label>
      </div>
      <div class="allegations-column-head">
        <h2>Hearings</h2>
        <button type="button" class="btn" id="addHearingBtn">+ Add hearing</button>
      </div>
      <p class="empty hearing-empty" style="display:none">No hearings yet.</p>
      <div class="evidence-list hearing-list"></div>`;

    const courtInput = caseDetailEl.querySelector("#courtInput");
    courtInput.value = detailCase.court;
    courtInput.addEventListener("input", () => {
      detailCase.court = courtInput.value;
      updateSelectedCaseMeta();
      saveDetail();
    });
    flushSaveOnEnter(courtInput, "detail:" + detailCase.id, detailCase.id, () => ({
      court: detailCase.court,
      case_number: detailCase.case_number,
      hearings: detailCase.hearings,
    }));

    const caseNumberInput = caseDetailEl.querySelector("#caseNumberInput");
    caseNumberInput.value = detailCase.case_number;
    caseNumberInput.addEventListener("input", () => {
      detailCase.case_number = caseNumberInput.value;
      updateSelectedCaseMeta();
      saveDetail();
    });
    flushSaveOnEnter(caseNumberInput, "detail:" + detailCase.id, detailCase.id, () => ({
      court: detailCase.court,
      case_number: detailCase.case_number,
      hearings: detailCase.hearings,
    }));

    caseDetailEl.querySelector("#addHearingBtn").addEventListener("click", () => {
      const hearing = { id: genId(), date: "", summary: "" };
      detailCase.hearings.push(hearing);
      renderHearingList();
      updateSelectedCaseMeta();
      saveDetail();
      const newCard = caseDetailEl.querySelector(`.hearing-card[data-id="${hearing.id}"] .hearing-date-input`);
      if (newCard) setTimeout(() => newCard.focus(), 0);
    });

    renderHearingList();
    enableDragReorder(caseDetailEl.querySelector(".hearing-list"), ".hearing-card", reorderHearingsFromDom);
  }

  async function selectCase(id) {
    selectedId = id;
    renderCaseList();
    caseDetailEl.innerHTML = '<p class="empty">Loading&hellip;</p>';
    try {
      const res = await fetch(caseUrl(id));
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      detailCase = { id, ...data };
      if (!Array.isArray(detailCase.hearings)) detailCase.hearings = [];
      renderDetail();
    } catch (e) {
      caseDetailEl.innerHTML = `<p class="empty">Failed to load case: ${escapeHtml(e.message)}</p>`;
    }
  }

  loadCases();
})();
