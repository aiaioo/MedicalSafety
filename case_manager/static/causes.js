(function () {
  const appEl = document.getElementById("causesApp");
  if (!appEl) return;
  const causesUrl = appEl.dataset.causesUrl;
  const causeUrlBase = appEl.dataset.causeUrlBase;
  const casesUrl = appEl.dataset.casesUrl;
  const casesPageUrl = appEl.dataset.casesPageUrl;

  function causeUrl(id) {
    return causeUrlBase.replace("__ID__", encodeURIComponent(id));
  }

  const saveStatusEl = document.getElementById("saveStatus");
  const addCauseBtn = document.getElementById("addCauseBtn");
  const causeListEl = document.getElementById("causeList");
  const causeListEmpty = document.getElementById("causeListEmpty");
  const causeDetailEl = document.getElementById("causeDetail");

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

  // Grows a textarea to fit its content instead of clipping overflow text
  // behind its fixed rows="2" height. Call on every "input" so typing never
  // outgrows the box.
  function autoGrow(el) {
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }

  // scrollHeight reads 0 on a node that isn't in the document yet, which
  // every card here is at the point its value is first set -- cards are
  // built off-DOM, then appended by the caller. Defer to the next frame so
  // pre-existing long text is expanded on first render.
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

  // Debounced partial saves, keyed independently so a card edit (title/
  // description) and a detail-pane edit (goals) for the same cause never
  // clobber each other's in-flight timer.
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
    const res = await fetch(causeUrl(id), {
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

  function flushSaveOnEnter(el, key, id, buildPartial) {
    el.addEventListener("keydown", (e) => {
      if (!isSaveShortcut(e)) return;
      if (e.key !== "Enter") e.preventDefault();
      setTimeout(() => flushSaveNow(el, key, id, buildPartial), 0);
    });
  }

  // A small disk icon pinned to the bottom-right of a card, saving it the
  // same way Ctrl/Cmd+S would.
  function buildCardSaveBtn(flashEl, key, id, buildPartial) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "card-save-btn";
    btn.title = "Save";
    btn.textContent = "\u{1F4BE}︎"; // floppy disk, text presentation so it stays monochrome
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      flushSaveNow(flashEl, key, id, buildPartial);
    });
    return btn;
  }

  // ---------------------------------------------------------------------
  // Drag-to-reorder (mirrors static/cases.js) -- used for the goal list
  // within a cause's detail pane.
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
  // Cause list (left column)
  // ---------------------------------------------------------------------
  let causes = [];
  let selectedId = null;
  let cases = []; // [{id, name, ...}] for the goal case-link picker

  async function loadCasesForPicker() {
    try {
      const res = await fetch(casesUrl);
      cases = res.ok ? await res.json() : [];
    } catch (e) {
      cases = [];
    }
  }

  function causeMetaLabel(c) {
    return `${c.goal_count} goal${c.goal_count === 1 ? "" : "s"}`;
  }

  function buildCauseCard(c) {
    const card = document.createElement("div");
    card.className = "allegation-card cause-card" + (c.id === selectedId ? " selected" : "");
    card.dataset.id = c.id;
    card.innerHTML = `
      <div class="allegation-card-head">
        <input type="text" class="allegation-title-input" placeholder="Cause title" maxlength="300">
        ${c.goal_count === 0 ? '<button type="button" class="card-delete-btn" title="Delete cause">✕</button>' : ""}
      </div>
      <textarea class="allegation-summary-input" rows="2" placeholder="Describe this cause…" maxlength="10000"></textarea>
      <div class="allegation-card-meta"></div>`;

    const titleEl = card.querySelector(".allegation-title-input");
    titleEl.value = c.title;
    titleEl.addEventListener("input", () => {
      c.title = titleEl.value;
      if (selectedId === c.id) {
        const h2 = causeDetailEl.querySelector(".allegation-detail-head h2");
        if (h2) h2.textContent = c.title || "Untitled cause";
      }
      scheduleSave("card:" + c.id, c.id, { title: c.title, description: c.description });
    });
    flushSaveOnEnter(titleEl, "card:" + c.id, c.id, () => ({ title: c.title, description: c.description }));

    const descEl = card.querySelector(".allegation-summary-input");
    descEl.value = c.description;
    autoGrowOnAttach(descEl);
    descEl.addEventListener("input", () => {
      c.description = descEl.value;
      autoGrow(descEl);
      scheduleSave("card:" + c.id, c.id, { title: c.title, description: c.description });
    });
    flushSaveOnEnter(descEl, "card:" + c.id, c.id, () => ({ title: c.title, description: c.description }));

    card.querySelector(".allegation-card-meta").textContent = causeMetaLabel(c);

    const deleteBtn = card.querySelector(".card-delete-btn");
    if (deleteBtn) wireConfirmDelete(deleteBtn, () => deleteCause(c.id));

    card.appendChild(buildCardSaveBtn(card, "card:" + c.id, c.id, () => ({ title: c.title, description: c.description })));

    card.addEventListener("click", (e) => {
      if (e.target.closest("input, textarea, button")) return;
      selectCause(c.id);
    });

    return card;
  }

  function deleteCause(id) {
    fetch(causeUrl(id), { method: "DELETE" })
      .then((res) => {
        if (!res.ok) throw new Error(res.statusText);
        causes = causes.filter((c) => c.id !== id);
        if (selectedId === id) {
          selectedId = null;
          detailCause = null;
          renderDetail();
        }
        renderCauseList();
      })
      .catch((e) => setStatus("Could not delete cause: " + e.message, true));
  }

  function renderCauseList() {
    causeListEl.innerHTML = "";
    causeListEmpty.style.display = causes.length ? "none" : "block";
    for (const c of causes) causeListEl.appendChild(buildCauseCard(c));
  }

  async function loadCauses() {
    try {
      const [causesRes] = await Promise.all([fetch(causesUrl), loadCasesForPicker()]);
      causes = await causesRes.json();
      renderCauseList();
    } catch (e) {
      console.error(e);
    }
  }

  addCauseBtn.addEventListener("click", async () => {
    addCauseBtn.disabled = true;
    try {
      const res = await fetch(causesUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "Untitled cause" }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      causes.unshift({
        id: data.id,
        title: data.title,
        description: data.description,
        goal_count: 0,
        created_at: data.created_at,
        updated_at: data.updated_at,
      });
      renderCauseList();
      selectCause(data.id);
      const card = causeListEl.querySelector(`.cause-card[data-id="${data.id}"]`);
      if (card) setTimeout(() => card.querySelector(".allegation-title-input").focus(), 0);
    } catch (e) {
      setStatus("Could not create cause: " + e.message, true);
    } finally {
      addCauseBtn.disabled = false;
    }
  });

  // ---------------------------------------------------------------------
  // Cause detail (right pane): the cause's goals
  // ---------------------------------------------------------------------
  let detailCause = null; // full record for the selected cause, incl. goals

  // Only sends `goals` -- title/description are edited (and saved) from the
  // card in the left list instead, under the independent "card:" key, so
  // this must never echo back detailCause's (possibly stale) copy of them.
  function saveDetail() {
    if (!detailCause) return;
    scheduleSave("detail:" + detailCause.id, detailCause.id, { goals: detailCause.goals });
  }

  function updateSelectedCauseMeta() {
    const c = causes.find((x) => x.id === selectedId);
    if (!c) return;
    c.goal_count = detailCause.goals.length;
    const card = causeListEl.querySelector(`.cause-card[data-id="${c.id}"]`);
    if (!card) return;
    card.querySelector(".allegation-card-meta").textContent = causeMetaLabel(c);
    const head = card.querySelector(".allegation-card-head");
    const existingBtn = head.querySelector(".card-delete-btn");
    if (c.goal_count === 0 && !existingBtn) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "card-delete-btn";
      btn.title = "Delete cause";
      btn.textContent = "✕";
      head.appendChild(btn);
      wireConfirmDelete(btn, () => deleteCause(c.id));
    } else if (c.goal_count !== 0 && existingBtn) {
      existingBtn.remove();
    }
  }

  // ---------------------------------------------------------------------
  // Linked-cases picker for a goal -- a checkbox popover anchored to the
  // "Linked cases" toggle inside each goal card (mirrors static/
  // allegations.js's case-links-inline component).
  // ---------------------------------------------------------------------
  let closeOpenCaseLinksPopover = () => {};

  function caseName(id) {
    const c = cases.find((x) => x.id === id);
    return c ? c.name || id : id;
  }

  function linkedCasesLabel(goal) {
    const links = goal.case_ids || [];
    return links.length ? links.map(caseName).join(", ") : "None";
  }

  function syncGoalDeleteButton(goal, card) {
    const existingBtn = card.querySelector(".card-delete-btn");
    if ((goal.case_ids || []).length === 0 && !existingBtn) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "card-delete-btn";
      btn.title = "Delete goal";
      btn.textContent = "✕";
      const saveBtn = card.querySelector(".card-save-btn");
      card.insertBefore(btn, saveBtn || null);
      wireConfirmDelete(btn, () => {
        detailCause.goals = detailCause.goals.filter((g) => g.id !== goal.id);
        renderGoalList();
        updateSelectedCauseMeta();
        saveDetail();
      });
    } else if ((goal.case_ids || []).length !== 0 && existingBtn) {
      existingBtn.remove();
    }
  }

  function buildGoalCaseLinks(goal, card) {
    const wrap = document.createElement("div");
    wrap.className = "case-links-inline-inner";
    wrap.draggable = false;

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "case-links-toggle";
    toggle.innerHTML = `Linked cases: <span class="case-links-summary">${escapeHtml(linkedCasesLabel(goal))}</span>`;

    const popover = document.createElement("div");
    popover.className = "case-links-popover";
    popover.hidden = true;
    popover.draggable = false;

    function renderPopoverBody() {
      if (!cases.length) {
        popover.innerHTML = `<p class="empty">No cases yet. Create one in the <a href="${casesPageUrl}">Cases</a> workspace.</p>`;
        return;
      }
      const linked = new Set(goal.case_ids || []);
      popover.innerHTML = cases
        .map(
          (c) => `
        <label class="case-link-row">
          <input type="checkbox" value="${escapeHtml(c.id)}" ${linked.has(c.id) ? "checked" : ""}>
          ${escapeHtml(c.name || c.id)}
        </label>`
        )
        .join("");
      popover.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
        cb.addEventListener("change", () => {
          const set = new Set(goal.case_ids || []);
          if (cb.checked) set.add(cb.value);
          else set.delete(cb.value);
          goal.case_ids = [...set];
          toggle.querySelector(".case-links-summary").textContent = linkedCasesLabel(goal);
          syncGoalDeleteButton(goal, card);
          saveDetail();
        });
      });
    }
    renderPopoverBody();

    function onOutsideClick(e) {
      if (!wrap.contains(e.target)) closePopover();
    }

    function closePopover() {
      popover.hidden = true;
      document.removeEventListener("click", onOutsideClick, true);
      closeOpenCaseLinksPopover = () => {};
    }

    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasOpen = !popover.hidden;
      closeOpenCaseLinksPopover();
      if (!wasOpen) {
        popover.hidden = false;
        document.addEventListener("click", onOutsideClick, true);
        closeOpenCaseLinksPopover = closePopover;
      }
    });
    popover.addEventListener("click", (e) => e.stopPropagation());

    wrap.appendChild(toggle);
    wrap.appendChild(popover);
    return wrap;
  }

  // ---------------------------------------------------------------------
  // Goal cards
  // ---------------------------------------------------------------------
  function buildGoalCard(goal) {
    const card = document.createElement("div");
    card.className = "evidence-card goal-card";
    card.draggable = true;
    card.dataset.id = goal.id;
    card.innerHTML = `
      <span class="drag-handle" title="Drag to reorder">⠿</span>
      <div class="goal-fields">
        <input type="text" class="goal-title-input" placeholder="Goal title" maxlength="300">
        <textarea class="goal-description-input" rows="2" placeholder="Describe this goal…" maxlength="10000"></textarea>
        <div class="case-links-inline"></div>
      </div>
      ${(goal.case_ids || []).length === 0 ? '<button type="button" class="card-delete-btn" title="Delete goal">✕</button>' : ""}`;

    function flushGoalField(el) {
      flushSaveOnEnter(el, "detail:" + detailCause.id, detailCause.id, () => ({ goals: detailCause.goals }));
    }

    const titleEl = card.querySelector(".goal-title-input");
    titleEl.value = goal.title || "";
    titleEl.addEventListener("input", () => {
      goal.title = titleEl.value;
      saveDetail();
    });
    flushGoalField(titleEl);

    const descEl = card.querySelector(".goal-description-input");
    descEl.value = goal.description || "";
    autoGrowOnAttach(descEl);
    descEl.addEventListener("input", () => {
      goal.description = descEl.value;
      autoGrow(descEl);
      saveDetail();
    });
    flushGoalField(descEl);

    card.querySelector(".case-links-inline").appendChild(buildGoalCaseLinks(goal, card));

    const deleteBtn = card.querySelector(".card-delete-btn");
    if (deleteBtn) {
      wireConfirmDelete(deleteBtn, () => {
        detailCause.goals = detailCause.goals.filter((g) => g.id !== goal.id);
        renderGoalList();
        updateSelectedCauseMeta();
        saveDetail();
      });
    }

    card.appendChild(
      buildCardSaveBtn(card, "detail:" + detailCause.id, detailCause.id, () => ({ goals: detailCause.goals }))
    );

    return card;
  }

  function renderGoalList() {
    closeOpenCaseLinksPopover();
    const listEl = causeDetailEl.querySelector(".goal-list");
    const emptyEl = causeDetailEl.querySelector(".goal-empty");
    if (!listEl) return;
    listEl.innerHTML = "";
    emptyEl.style.display = detailCause.goals.length ? "none" : "block";
    for (const g of detailCause.goals) listEl.appendChild(buildGoalCard(g));
  }

  function reorderGoalsFromDom() {
    const listEl = causeDetailEl.querySelector(".goal-list");
    const ids = [...listEl.querySelectorAll(".goal-card")].map((el) => el.dataset.id);
    detailCause.goals.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
    saveDetail();
  }

  function renderDetail() {
    if (!detailCause) {
      causeDetailEl.innerHTML = '<p class="empty">Select or add a cause on the left to see its goals.</p>';
      return;
    }
    causeDetailEl.innerHTML = `
      <div class="allegation-detail-head">
        <h2>${escapeHtml(detailCause.title || "Untitled cause")}</h2>
      </div>
      <div class="allegations-column-head">
        <h2>Goals</h2>
        <button type="button" class="btn" id="addGoalBtn">+ Add goal</button>
      </div>
      <p class="empty goal-empty" style="display:none">No goals yet.</p>
      <div class="evidence-list goal-list"></div>`;

    causeDetailEl.querySelector("#addGoalBtn").addEventListener("click", () => {
      const goal = { id: genId(), title: "", description: "", case_ids: [] };
      detailCause.goals.push(goal);
      renderGoalList();
      updateSelectedCauseMeta();
      saveDetail();
      const newCard = causeDetailEl.querySelector(`.goal-card[data-id="${goal.id}"] .goal-title-input`);
      if (newCard) setTimeout(() => newCard.focus(), 0);
    });

    renderGoalList();
    enableDragReorder(causeDetailEl.querySelector(".goal-list"), ".goal-card", reorderGoalsFromDom);
  }

  async function selectCause(id) {
    selectedId = id;
    renderCauseList();
    causeDetailEl.innerHTML = '<p class="empty">Loading&hellip;</p>';
    try {
      const res = await fetch(causeUrl(id));
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      detailCause = { id, ...data };
      if (!Array.isArray(detailCause.goals)) detailCause.goals = [];
      renderDetail();
    } catch (e) {
      causeDetailEl.innerHTML = `<p class="empty">Failed to load cause: ${escapeHtml(e.message)}</p>`;
    }
  }

  loadCauses();
})();
