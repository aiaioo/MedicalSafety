(function () {
  const appEl = document.getElementById("allegationsApp");
  if (!appEl) return;
  const caseId = appEl.dataset.case;
  const caseUrl = appEl.dataset.caseUrl;

  const titleInput = document.getElementById("titleInput");
  const saveBtn = document.getElementById("saveBtn");
  const saveStatusEl = document.getElementById("saveStatus");

  const allegationListEl = document.getElementById("allegationList");
  const allegationListEmpty = document.getElementById("allegationListEmpty");
  const allegationDetailEl = document.getElementById("allegationDetail");
  const addAllegationBtn = document.getElementById("addAllegationBtn");

  // -------------------------------------------------------------------
  // Small helpers shared with the other pages
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

  // -------------------------------------------------------------------
  // Case editor (only present once a case is open)
  // -------------------------------------------------------------------
  if (!caseId || !allegationListEl) return;

  let caseData = null;
  let selectedId = null;
  let dirty = false;
  let saving = false;
  let saveAgainAfter = false;
  let autosaveTimer = null;

  function setStatus(text, isError) {
    saveStatusEl.textContent = text || "";
    saveStatusEl.style.color = isError ? "#c0392b" : "#8a92a5";
  }

  function markDirty() {
    dirty = true;
    setStatus("Saving…");
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => {
      saveCase().catch((e) => setStatus("Save failed: " + e.message, true));
    }, 1200);
  }

  async function saveCase() {
    if (saving) {
      saveAgainAfter = true;
      return;
    }
    saving = true;
    if (autosaveTimer) {
      clearTimeout(autosaveTimer);
      autosaveTimer = null;
    }
    const payload = {
      name: titleInput.value.trim() || "Untitled case",
      allegations: caseData.allegations,
    };
    try {
      const res = await fetch(caseUrl, {
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
        await saveCase();
      }
    }
  }

  saveBtn.addEventListener("click", async () => {
    try {
      await saveCase();
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveCase().catch((err) => setStatus("Save failed: " + err.message, true));
    }
  });
  titleInput.addEventListener("input", markDirty);

  window.addEventListener("beforeunload", () => {
    if (!dirty || !caseData) return;
    try {
      const blob = new Blob(
        [JSON.stringify({ name: titleInput.value.trim() || "Untitled case", allegations: caseData.allegations })],
        { type: "application/json" }
      );
      navigator.sendBeacon(caseUrl, blob);
    } catch (err) {
      // best effort only
    }
  });

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
  // Allegation list (left column)
  // ---------------------------------------------------------------------
  function evidenceCountLabel(allegation) {
    const inc = allegation.inculpatory.length;
    const exc = allegation.exculpatory.length;
    return `${inc} inculpatory · ${exc} exculpatory`;
  }

  function updateAllegationCardMeta(id) {
    const allegation = caseData.allegations.find((a) => a.id === id);
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
      markDirty();
    });

    const summaryEl = card.querySelector(".allegation-summary-input");
    summaryEl.value = allegation.description;
    summaryEl.addEventListener("input", () => {
      allegation.description = summaryEl.value;
      markDirty();
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
    allegationListEl.innerHTML = "";
    allegationListEmpty.style.display = caseData.allegations.length ? "none" : "block";
    for (const allegation of caseData.allegations) {
      allegationListEl.appendChild(buildAllegationCard(allegation));
    }
  }

  function reorderAllegationsFromDom() {
    const ids = [...allegationListEl.querySelectorAll(".allegation-card")].map((el) => el.dataset.id);
    caseData.allegations.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
    markDirty();
  }
  enableDragReorder(allegationListEl, ".allegation-card", reorderAllegationsFromDom);

  function selectAllegation(id) {
    selectedId = id;
    renderAllegationList();
    renderDetail();
  }

  function addAllegation() {
    const allegation = { id: genId(), title: "", description: "", inculpatory: [], exculpatory: [] };
    caseData.allegations.push(allegation);
    selectedId = allegation.id;
    renderAllegationList();
    renderDetail();
    markDirty();
    const card = allegationListEl.querySelector(`.allegation-card[data-id="${allegation.id}"]`);
    if (card) setTimeout(() => card.querySelector(".allegation-title-input").focus(), 0);
  }
  addAllegationBtn.addEventListener("click", addAllegation);

  function deleteAllegation(id) {
    caseData.allegations = caseData.allegations.filter((a) => a.id !== id);
    if (selectedId === id) {
      selectedId = caseData.allegations.length ? caseData.allegations[0].id : null;
    }
    renderAllegationList();
    renderDetail();
    markDirty();
  }

  // ---------------------------------------------------------------------
  // Evidence sublists (right pane, one allegation at a time)
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
      markDirty();
    });

    wireConfirmDelete(card.querySelector(".card-delete-btn"), () => {
      allegation[kind] = allegation[kind].filter((e) => e.id !== item.id);
      renderDetail();
      updateAllegationCardMeta(allegation.id);
      markDirty();
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
      markDirty();
    }
    enableDragReorder(listEl, ".evidence-card", reorderFromDom);

    col.querySelector(".add-evidence-btn").addEventListener("click", () => {
      const item = { id: genId(), text: "" };
      allegation[kind].push(item);
      renderDetail();
      updateAllegationCardMeta(allegation.id);
      markDirty();
      const newCard = allegationDetailEl.querySelector(
        `.evidence-column.evidence-${kind} .evidence-card[data-id="${item.id}"] textarea`
      );
      if (newCard) setTimeout(() => newCard.focus(), 0);
    });

    return col;
  }

  function renderDetail() {
    const allegation = caseData.allegations.find((a) => a.id === selectedId);
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
  }

  // ---------------------------------------------------------------------
  // Initial load
  // ---------------------------------------------------------------------
  (async function loadCase() {
    try {
      const res = await fetch(caseUrl);
      if (!res.ok) throw new Error(await res.text());
      caseData = await res.json();
      if (!Array.isArray(caseData.allegations)) caseData.allegations = [];
      titleInput.value = caseData.name || "";
      selectedId = caseData.allegations.length ? caseData.allegations[0].id : null;
      renderAllegationList();
      renderDetail();
    } catch (e) {
      setStatus("Failed to load: " + e.message, true);
    }
  })();
})();
