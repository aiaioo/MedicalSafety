(function () {
  const appEl = document.getElementById("casesApp");
  if (!appEl) return;
  const casesUrl = appEl.dataset.casesUrl;
  const caseUrlBase = appEl.dataset.caseUrlBase;
  const allegationsUrlBase = appEl.dataset.allegationsUrlBase;
  const sourceDocumentsUrl = appEl.dataset.sourceDocumentsUrl;
  const annotationsPageUrl = appEl.dataset.annotationsPageUrl;

  function caseUrl(id) {
    return caseUrlBase.replace("__ID__", encodeURIComponent(id));
  }

  function sourceDocUrl(doc) {
    return `${annotationsPageUrl}?doc=${encodeURIComponent(doc.doc_id)}&type=${encodeURIComponent(doc.doc_type || "pdf")}`;
  }

  function truncateMiddle(s, max) {
    max = max || 28;
    return s.length > max ? s.slice(0, max) + "…" : s;
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

  // Grows a textarea to fit its content instead of clipping overflow text
  // behind its fixed rows="2" height. Call on every "input" so typing never
  // outgrows the box.
  function autoGrow(el) {
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }

  // scrollHeight reads 0 (or the CSS rows height) on a node that isn't in the
  // document yet, which every card here is at the point its value is first
  // set -- cards are built off-DOM, then appended by the caller. Defer to the
  // next frame, by which point the append has happened, so pre-existing long
  // text is expanded on first render instead of only once it's next edited.
  function autoGrowOnAttach(el) {
    requestAnimationFrame(() => autoGrow(el));
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

  // Flushes a pending debounced save immediately, flashing `flashEl` once it
  // lands (or right away if nothing was pending -- the request was still an
  // explicit ask for confirmation). Shared by the Enter/Ctrl/Cmd+S key
  // handler below and each card's save button.
  function flushSaveNow(flashEl, key, id, buildPartial) {
    if (!saveTimers[key]) {
      flashSaved(flashEl);
      return;
    }
    clearTimeout(saveTimers[key]);
    saveTimers[key] = null;
    savePartial(id, buildPartial())
      .then(() => flashSaved(flashEl))
      .catch((err) => setStatus("Save failed: " + err.message, true));
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
      setTimeout(() => flushSaveNow(el, key, id, buildPartial), 0);
    });
  }

  // A small disk icon pinned to the bottom-right of a card, saving it the
  // same way Ctrl/Cmd+S would (and flashing the whole card, not just a
  // field, since it's not tied to any one input).
  function buildCardSaveBtn(flashEl, key, id, buildPartial) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "card-save-btn";
    btn.title = "Save";
    btn.textContent = "\u{1F4BE}︎"; // floppy disk, text presentation so it stays monochrome like the other card icons
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      flushSaveNow(flashEl, key, id, buildPartial);
    });
    return btn;
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
  let sourceDocs = []; // [{id, type}] uploaded source documents, for the hearing document pickers

  async function loadSourceDocs() {
    try {
      const res = await fetch(sourceDocumentsUrl);
      sourceDocs = res.ok ? await res.json() : [];
    } catch (e) {
      sourceDocs = [];
    }
  }

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
    autoGrowOnAttach(summaryEl);
    summaryEl.addEventListener("input", () => {
      c.summary = summaryEl.value;
      autoGrow(summaryEl);
      scheduleSave("card:" + c.id, c.id, { name: c.name, summary: c.summary });
    });
    flushSaveOnEnter(summaryEl, "card:" + c.id, c.id, () => ({ name: c.name, summary: c.summary }));

    card.querySelector(".allegation-card-meta").textContent = caseMetaLabel(c);

    const deleteBtn = card.querySelector(".card-delete-btn");
    if (deleteBtn) wireConfirmDelete(deleteBtn, () => deleteCase(c.id));

    card.appendChild(buildCardSaveBtn(card, "card:" + c.id, c.id, () => ({ name: c.name, summary: c.summary })));

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
      const [casesRes] = await Promise.all([fetch(casesUrl), loadSourceDocs()]);
      cases = await casesRes.json();
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

  function isIsoDate(s) {
    return typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s);
  }

  // Hearings used to store a single free-text "date" field that people filled
  // with both the date and a label (e.g. "2026-03-05 First Hearing"), before
  // the two were split into separate fields. Split any such legacy value the
  // first time it's loaded, so it lands in the right field instead of just
  // failing to populate the new date picker.
  function migrateLegacyHearingDate(hearing) {
    if (isIsoDate(hearing.date) || hearing.title) return;
    const raw = hearing.date || "";
    const m = raw.match(/^(\d{4}-\d{2}-\d{2})\s*(.*)$/);
    if (m) {
      hearing.date = m[1];
      hearing.title = m[2];
    } else if (raw) {
      hearing.title = raw;
      hearing.date = "";
    }
  }

  // ---------------------------------------------------------------------
  // Per-hearing document lists ("Submitted" / "Received") -- each entry
  // links to an uploaded source document (documents/), picked from a
  // popover mirroring the evidence-item report picker in allegations.js,
  // but appending to a list instead of setting a single value.
  // ---------------------------------------------------------------------
  let closeOpenHearingDocPopover = () => {};

  function buildHearingDocList(hearing, key, label) {
    const wrap = document.createElement("div");
    wrap.className = "hearing-doc-list";
    wrap.draggable = false;

    const head = document.createElement("div");
    head.className = "hearing-doc-list-head";
    const labelEl = document.createElement("span");
    labelEl.className = "hearing-doc-list-label";
    labelEl.textContent = label;
    head.appendChild(labelEl);
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "evidence-doc-add-btn";
    addBtn.textContent = "+ Add";
    head.appendChild(addBtn);
    wrap.appendChild(head);

    const chipsEl = document.createElement("div");
    chipsEl.className = "hearing-doc-chips";
    wrap.appendChild(chipsEl);

    let popoverEl = null;

    function onOutsideClick(e) {
      if (!wrap.contains(e.target)) closePopover();
    }

    function closePopover() {
      if (popoverEl) {
        popoverEl.remove();
        popoverEl = null;
      }
      document.removeEventListener("click", onOutsideClick, true);
      closeOpenHearingDocPopover = () => {};
    }

    function renderChips() {
      chipsEl.innerHTML = "";
      const list = hearing[key] || [];
      if (!list.length) {
        const p = document.createElement("span");
        p.className = "hearing-doc-empty";
        p.textContent = "None";
        chipsEl.appendChild(p);
        return;
      }
      for (const item of list) {
        const chip = document.createElement("span");
        chip.className = "hearing-doc-chip";
        const link = document.createElement("a");
        link.className = "evidence-doc-chip";
        link.href = sourceDocUrl(item);
        link.target = "_blank";
        link.rel = "noopener";
        link.title = `Open "${item.doc_id}"`;
        link.textContent = `📄 ${truncateMiddle(item.doc_id)}`;
        link.addEventListener("click", (e) => e.stopPropagation());
        chip.appendChild(link);
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "hearing-doc-remove-btn";
        rm.title = "Remove";
        rm.textContent = "✕";
        rm.addEventListener("click", (e) => {
          e.stopPropagation();
          hearing[key] = (hearing[key] || []).filter((x) => x.id !== item.id);
          renderChips();
          saveDetail();
        });
        chip.appendChild(rm);
        chipsEl.appendChild(chip);
      }
    }
    renderChips();

    function addDoc(doc) {
      if (!Array.isArray(hearing[key])) hearing[key] = [];
      hearing[key].push({ id: genId(), doc_id: doc.id, doc_type: doc.type });
      closePopover();
      renderChips();
      saveDetail();
    }

    function buildPopover() {
      const popover = document.createElement("div");
      popover.className = "evidence-doc-popover";
      popover.draggable = false;
      popover.addEventListener("click", (e) => e.stopPropagation());

      if (!sourceDocs.length) {
        popover.innerHTML = `<p class="empty">No source documents uploaded yet.</p>`;
        return popover;
      }

      const linkedIds = new Set((hearing[key] || []).map((x) => x.doc_id));

      const filterInput = document.createElement("input");
      filterInput.type = "text";
      filterInput.className = "evidence-doc-filter";
      filterInput.placeholder = "Filter documents…";
      popover.appendChild(filterInput);

      const listEl = document.createElement("div");
      listEl.className = "evidence-doc-popover-list";
      popover.appendChild(listEl);

      function renderList() {
        const q = filterInput.value.trim().toLowerCase();
        const matches = sourceDocs.filter((d) => !q || d.id.toLowerCase().includes(q));
        listEl.innerHTML = "";
        if (!matches.length) {
          const p = document.createElement("p");
          p.className = "empty";
          p.textContent = "No matching documents.";
          listEl.appendChild(p);
        }
        for (const d of matches) {
          const btn = document.createElement("button");
          btn.type = "button";
          const already = linkedIds.has(d.id);
          btn.className = "evidence-doc-option" + (already ? " selected" : "");
          btn.title = d.id;
          btn.textContent = truncateMiddle(d.id) + (already ? " (added)" : "");
          btn.addEventListener("click", () => addDoc(d));
          listEl.appendChild(btn);
        }
      }
      renderList();
      filterInput.addEventListener("input", renderList);
      setTimeout(() => filterInput.focus(), 0);

      return popover;
    }

    function togglePopover() {
      const wasOpen = !!popoverEl;
      closeOpenHearingDocPopover();
      if (!wasOpen) {
        popoverEl = buildPopover();
        wrap.appendChild(popoverEl);
        document.addEventListener("click", onOutsideClick, true);
        closeOpenHearingDocPopover = closePopover;
      }
    }

    addBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      togglePopover();
    });

    return wrap;
  }

  function buildHearingCard(hearing) {
    const card = document.createElement("div");
    card.className = "evidence-card hearing-card";
    card.draggable = true;
    card.dataset.id = hearing.id;
    card.innerHTML = `
      <span class="drag-handle" title="Drag to reorder">⠿</span>
      <div class="hearing-fields">
        <div class="hearing-card-head">
          <input type="date" class="hearing-date-input" lang="en-GB" title="Hearing date">
          <input type="text" class="hearing-title-input" placeholder="Hearing title (e.g. First hearing)" maxlength="300">
        </div>
        <textarea class="hearing-summary-input" rows="2" placeholder="What happened at this hearing…" maxlength="10000"></textarea>
        <div class="hearing-doc-lists"></div>
      </div>
      <button type="button" class="card-delete-btn" title="Delete">✕</button>`;

    function flushHearingField(el) {
      flushSaveOnEnter(el, "detail:" + detailCase.id, detailCase.id, () => ({
        court: detailCase.court,
        case_number: detailCase.case_number,
        hearings: detailCase.hearings,
      }));
    }

    const dateEl = card.querySelector(".hearing-date-input");
    dateEl.value = isIsoDate(hearing.date) ? hearing.date : "";
    dateEl.addEventListener("input", () => {
      hearing.date = dateEl.value;
      saveDetail();
    });
    flushHearingField(dateEl);

    const titleEl = card.querySelector(".hearing-title-input");
    titleEl.value = hearing.title || "";
    titleEl.addEventListener("input", () => {
      hearing.title = titleEl.value;
      saveDetail();
    });
    flushHearingField(titleEl);

    const summaryEl = card.querySelector(".hearing-summary-input");
    summaryEl.value = hearing.summary;
    autoGrowOnAttach(summaryEl);
    summaryEl.addEventListener("input", () => {
      hearing.summary = summaryEl.value;
      autoGrow(summaryEl);
      saveDetail();
    });
    flushHearingField(summaryEl);

    const docListsEl = card.querySelector(".hearing-doc-lists");
    docListsEl.appendChild(buildHearingDocList(hearing, "submitted_docs", "Submitted documents"));
    docListsEl.appendChild(buildHearingDocList(hearing, "received_docs", "Received documents"));

    wireConfirmDelete(card.querySelector(".card-delete-btn"), () => {
      detailCase.hearings = detailCase.hearings.filter((h) => h.id !== hearing.id);
      renderHearingList();
      updateSelectedCaseMeta();
      saveDetail();
    });

    card.appendChild(
      buildCardSaveBtn(card, "detail:" + detailCase.id, detailCase.id, () => ({
        court: detailCase.court,
        case_number: detailCase.case_number,
        hearings: detailCase.hearings,
      }))
    );

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
      const hearing = { id: genId(), date: "", title: "", summary: "", submitted_docs: [], received_docs: [] };
      detailCase.hearings.push(hearing);
      renderHearingList();
      updateSelectedCaseMeta();
      saveDetail();
      const newCard = caseDetailEl.querySelector(`.hearing-card[data-id="${hearing.id}"] .hearing-title-input`);
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
      detailCase.hearings.forEach(migrateLegacyHearingDate);
      renderDetail();
    } catch (e) {
      caseDetailEl.innerHTML = `<p class="empty">Failed to load case: ${escapeHtml(e.message)}</p>`;
    }
  }

  loadCases();
})();
