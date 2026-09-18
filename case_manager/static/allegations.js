(function () {
  const appEl = document.getElementById("allegationsApp");
  if (!appEl) return;
  const allegationsUrl = appEl.dataset.allegationsUrl;
  const allegationUrlBase = appEl.dataset.allegationUrlBase;
  const allegationsOrderUrl = appEl.dataset.allegationsOrderUrl;
  const casesUrl = appEl.dataset.casesUrl;
  const casesPageUrl = appEl.dataset.casesPageUrl;
  const reportsUrl = appEl.dataset.reportsUrl;
  const documentsPageUrl = appEl.dataset.documentsPageUrl;
  const filterCaseId = appEl.dataset.filterCase || "";

  function reportUrl(reportId) {
    return `${documentsPageUrl}?report=${encodeURIComponent(reportId)}`;
  }

  function truncateMiddle(s, max) {
    max = max || 28;
    return s.length > max ? s.slice(0, max) + "…" : s;
  }

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
  let reports = []; // [{id, name, ...}] reports (from reports.html), for evidence-item linking
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
    return `${inc} inculpatory · ${exc} exculpatory`;
  }

  function linkedCasesLabel(allegation) {
    const links = allegation.case_ids || [];
    return links.length ? links.map(caseName).join(", ") : "None";
  }

  function updateAllegationCardMeta(id) {
    const allegation = allegations.find((a) => a.id === id);
    const card = allegationListEl.querySelector(`.allegation-card[data-id="${id}"]`);
    if (allegation && card) {
      card.querySelector(".allegation-card-meta").textContent = evidenceCountLabel(allegation);
      const summaryEl = card.querySelector(".case-links-summary");
      if (summaryEl) summaryEl.textContent = linkedCasesLabel(allegation);
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
      <div class="allegation-card-meta"></div>
      <div class="case-links-inline"></div>`;

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
    card.querySelector(".case-links-inline").appendChild(buildCaseLinksInline(allegation));

    card.addEventListener("click", (e) => {
      if (e.target.closest("input, textarea, button, .case-links-popover")) return;
      selectAllegation(allegation.id);
    });

    wireConfirmDelete(card.querySelector(".card-delete-btn"), () => deleteAllegation(allegation.id));

    return card;
  }

  function renderAllegationList() {
    closeOpenCaseLinksPopover();
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
      <div class="evidence-card-body">
        <textarea rows="2" placeholder="Describe this evidence…" maxlength="10000"></textarea>
      </div>
      <button type="button" class="card-delete-btn" title="Delete">✕</button>`;

    const bodyEl = card.querySelector(".evidence-card-body");
    const textEl = bodyEl.querySelector("textarea");
    textEl.value = item.text;
    textEl.addEventListener("input", () => {
      item.text = textEl.value;
      scheduleSave(allegation.id);
    });

    bodyEl.appendChild(buildEvidenceReportControl(allegation, item));

    wireConfirmDelete(card.querySelector(".card-delete-btn"), () => {
      allegation[kind] = allegation[kind].filter((e) => e.id !== item.id);
      renderDetail();
      updateAllegationCardMeta(allegation.id);
      scheduleSave(allegation.id);
    });

    return card;
  }

  function reportName(report) {
    return report.name || report.id;
  }

  // -------------------------------------------------------------------
  // Evidence-card report link — each evidence item can associate with at
  // most one report (from reports.html); clicking the
  // resulting chip opens it in the report editor.
  // -------------------------------------------------------------------
  function buildEvidenceReportControl(allegation, item) {
    const wrap = document.createElement("div");
    wrap.className = "evidence-doc-control";
    wrap.draggable = false;

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
      closeOpenDocPopover = () => {};
    }

    function pick(reportId) {
      item.report_id = reportId;
      closePopover();
      renderControl();
      scheduleSave(allegation.id);
    }

    function buildPopover() {
      const popover = document.createElement("div");
      popover.className = "evidence-doc-popover";
      popover.draggable = false;
      popover.addEventListener("click", (e) => e.stopPropagation());

      if (!reports.length) {
        popover.innerHTML = `<p class="empty">No reports yet. Create one in the <a href="${documentsPageUrl}">Reports</a> workspace.</p>`;
        return popover;
      }

      const filterInput = document.createElement("input");
      filterInput.type = "text";
      filterInput.className = "evidence-doc-filter";
      filterInput.placeholder = "Filter reports…";
      popover.appendChild(filterInput);

      const listEl = document.createElement("div");
      listEl.className = "evidence-doc-popover-list";
      popover.appendChild(listEl);

      function renderList() {
        const q = filterInput.value.trim().toLowerCase();
        const matches = reports.filter((r) => !q || reportName(r).toLowerCase().includes(q));
        listEl.innerHTML = "";
        const noneBtn = document.createElement("button");
        noneBtn.type = "button";
        noneBtn.className = "evidence-doc-option evidence-doc-option-none";
        noneBtn.textContent = "— No report —";
        noneBtn.addEventListener("click", () => pick(""));
        listEl.appendChild(noneBtn);
        if (!matches.length) {
          const p = document.createElement("p");
          p.className = "empty";
          p.textContent = "No matching reports.";
          listEl.appendChild(p);
        }
        for (const r of matches) {
          const btn = document.createElement("button");
          btn.type = "button";
          const isSelected = item.report_id === r.id;
          btn.className = "evidence-doc-option" + (isSelected ? " selected" : "");
          btn.title = reportName(r);
          btn.textContent = truncateMiddle(reportName(r));
          btn.addEventListener("click", () => pick(r.id));
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
      closeOpenDocPopover();
      closeOpenCaseLinksPopover();
      if (!wasOpen) {
        popoverEl = buildPopover();
        wrap.appendChild(popoverEl);
        document.addEventListener("click", onOutsideClick, true);
        closeOpenDocPopover = closePopover;
      }
    }

    function renderControl() {
      wrap.querySelectorAll(".evidence-doc-chip, .evidence-doc-add-btn, .evidence-doc-edit-btn").forEach((el) => el.remove());
      const report = item.report_id ? reports.find((r) => r.id === item.report_id) : null;
      // A linked report can be missing (deleted elsewhere) even though the
      // id is still on the item until the next save round-trips through
      // sanitize_evidence_list; fall back to the raw id so the chip still
      // shows something instead of silently disappearing.
      if (item.report_id) {
        const label = report ? reportName(report) : item.report_id;
        const link = document.createElement("a");
        link.className = "evidence-doc-chip";
        link.href = reportUrl(item.report_id);
        link.title = `Open "${label}"`;
        link.textContent = `📄 ${truncateMiddle(label)}`;
        link.addEventListener("click", (e) => e.stopPropagation());
        wrap.prepend(link);

        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "evidence-doc-edit-btn";
        editBtn.title = "Change linked report";
        editBtn.textContent = "Change";
        editBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          togglePopover();
        });
        wrap.insertBefore(editBtn, popoverEl);
      } else {
        const addBtn = document.createElement("button");
        addBtn.type = "button";
        addBtn.className = "evidence-doc-add-btn";
        addBtn.textContent = "+ Link report";
        addBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          togglePopover();
        });
        wrap.prepend(addBtn);
      }
    }

    renderControl();
    return wrap;
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
      const item = { id: genId(), text: "", report_id: "" };
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
  // Linked cases picker — a small popover anchored to the "Linked cases"
  // toggle inside each allegation card, so linking/unlinking a case never
  // requires opening the detail pane.
  // ---------------------------------------------------------------------
  let closeOpenCaseLinksPopover = () => {};
  let closeOpenDocPopover = () => {};

  function buildCaseLinksInline(allegation) {
    const wrap = document.createElement("div");
    wrap.className = "case-links-inline-inner";
    wrap.draggable = false;

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "case-links-toggle";
    toggle.innerHTML = `Linked cases: <span class="case-links-summary">${escapeHtml(linkedCasesLabel(allegation))}</span>`;

    const popover = document.createElement("div");
    popover.className = "case-links-popover";
    popover.hidden = true;
    popover.draggable = false;

    function renderPopoverBody() {
      if (!cases.length) {
        popover.innerHTML = `<p class="empty">No cases yet. Create one in the <a href="${casesPageUrl}">Cases</a> workspace.</p>`;
        return;
      }
      const linked = new Set(allegation.case_ids || []);
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
          const set = new Set(allegation.case_ids || []);
          if (cb.checked) set.add(cb.value);
          else set.delete(cb.value);
          allegation.case_ids = [...set];
          toggle.querySelector(".case-links-summary").textContent = linkedCasesLabel(allegation);
          scheduleSave(allegation.id);
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
      closeOpenDocPopover();
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

  function renderDetail() {
    closeOpenDocPopover();
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
  }

  // ---------------------------------------------------------------------
  // Initial load
  // ---------------------------------------------------------------------
  (async function init() {
    try {
      const [allegationsRes, casesRes, reportsRes] = await Promise.all([
        fetch(allegationsUrl),
        fetch(casesUrl),
        fetch(reportsUrl),
      ]);
      if (!allegationsRes.ok) throw new Error(await allegationsRes.text());
      allegations = await allegationsRes.json();
      cases = casesRes.ok ? await casesRes.json() : [];
      reports = reportsRes.ok ? await reportsRes.json() : [];
      renderFilterBar();
      renderAllegationList();
      renderDetail();
    } catch (e) {
      setStatus("Failed to load: " + e.message, true);
    }
  })();
})();
