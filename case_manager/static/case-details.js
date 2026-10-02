// Case details page: court / case number / court location, the case's
// parties (two drag-orderable columns) and the cause title generated from a
// template. Saves go to the same /api/allegation-case/<id> endpoint as the
// cases workspace, sending only the fields this page owns.
(function () {
  const appEl = document.getElementById("caseDetailsApp");
  if (!appEl) return;
  const caseUrl = appEl.dataset.caseUrlBase.replace("__ID__", encodeURIComponent(new URLSearchParams(location.search).get("case") || ""));
  const templatesUrl = appEl.dataset.templatesUrl;
  const bodyEl = document.getElementById("caseDetailsBody");
  const saveStatusEl = document.getElementById("saveStatus");

  const CASE_ROLES = ["Complainant", "Plaintiff", "Appellant", "Defendant", "Petitioner", "Claimant", "Respondent"];
  const SIDES = [
    { side: "complainant", heading: "Complainants", add: "+ Add complainant" },
    { side: "respondent", heading: "Respondents", add: "+ Add respondent" },
  ];

  let data = null;
  let templates = [];
  let saveTimer = null;

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function setStatus(text, isError) {
    saveStatusEl.textContent = text || "";
    saveStatusEl.classList.toggle("error", !!isError);
  }

  function canEdit() {
    return data.role === "editor" || data.role === "owner";
  }

  // -------------------------------------------------------------------
  // Saving
  // -------------------------------------------------------------------
  function scheduleSave(flashEl) {
    setStatus("Unsaved changes…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => save(flashEl), 600);
  }

  function flash(el) {
    if (!el) return;
    el.classList.remove("save-flash");
    void el.offsetWidth;
    el.classList.add("save-flash");
  }

  const saveNow = (flashEl) => save(flashEl);

  async function save(flashEl) {
    clearTimeout(saveTimer);
    setStatus("Saving…");
    try {
      const res = await fetch(caseUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          court: data.court,
          case_number: data.case_number,
          court_location: data.court_location,
          case_role: data.case_role,
          party_in_person: data.party_in_person,
          parties: data.parties,
          cause_title_template_id: data.cause_title_template_id,
          cause_title_font: data.cause_title_font,
          cause_title_font_size: data.cause_title_font_size,
          cause_title_one_line_parties: data.cause_title_one_line_parties,
          cause_title_doc: data.cause_title_doc,
        }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
      const saved = await res.json();
      // Blank parties are dropped server-side; their ids are fresh each save.
      data.parties = data.parties.filter((p) => p.name.trim());
      data.cause_title_font = saved.cause_title_font;
      data.cause_title_font_size = saved.cause_title_font_size;
      setStatus("Saved");
      flash(flashEl);
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  }

  window.addEventListener("beforeunload", () => {
    if (saveTimer) save();
  });

  // -------------------------------------------------------------------
  // Cause title
  // -------------------------------------------------------------------
  const generateCauseDoc = () => window.CauseTitle.generate(data, templates);

  let titleEditor = null;

  // Until the user edits the title by hand it follows the template and the
  // case's details; after that their version is kept (data.cause_title_doc)
  // until they clear it.
  function renderCauseTitle() {
    if (!titleEditor) return;
    titleEditor.setBaseStyle(data.cause_title_font, data.cause_title_font_size);
    if (!data.cause_title_doc) titleEditor.setBody(JSON.stringify(generateCauseDoc()));
    const edited = !!data.cause_title_doc;
    const resetBtn = bodyEl.querySelector("#resetCauseTitleBtn");
    resetBtn.disabled = !edited || !canEdit();
    // The option only shapes the generated title, so on an edited one it asks first.
    const oneLine = bodyEl.querySelector("#oneLinePartiesInput");
    oneLine.disabled = !canEdit();
    oneLine.title = edited ? "Changing this discards your changes to the cause title" : "Show \"<first party> and Ors.\" instead of listing every party";
    bodyEl.querySelector("#causeTitleHint").textContent = edited ? "Edited by hand" : "Generated from the template";
  }

  function onCauseTitleEdited() {
    data.cause_title_doc = JSON.parse(titleEditor.getBody());
    renderCauseTitle();
    scheduleSave();
  }

  function resetCauseTitle() {
    if (!data.cause_title_doc || !confirm("Discard your changes to the cause title and start again from the template?")) return;
    data.cause_title_doc = null;
    renderCauseTitle();
    scheduleSave();
  }

  // -------------------------------------------------------------------
  // Parties: cards in two columns, drag to reorder within a column
  // -------------------------------------------------------------------
  function dragAfterElement(container, y) {
    let closest = { offset: Number.NEGATIVE_INFINITY, element: null };
    for (const el of container.querySelectorAll(".party-card:not(.dragging)")) {
      const box = el.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) closest = { offset, element: el };
    }
    return closest.element;
  }

  function enableDragReorder(listEl, onDrop) {
    let dragging = null;
    listEl.addEventListener("dragstart", (e) => {
      const card = e.target.closest(".party-card");
      if (!card) return;
      dragging = card;
      setTimeout(() => card.classList.add("dragging"), 0);
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.dataset.id);
    });
    listEl.addEventListener("dragover", (e) => {
      if (!dragging) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const after = dragAfterElement(listEl, e.clientY);
      if (after == null) listEl.appendChild(dragging);
      else listEl.insertBefore(dragging, after);
    });
    listEl.addEventListener("dragend", () => {
      if (dragging) dragging.classList.remove("dragging");
      dragging = null;
      onDrop();
    });
  }

  function buildPartyCard(party) {
    const card = document.createElement("div");
    card.className = "party-card";
    card.draggable = true;
    card.dataset.id = party.id;
    card.innerHTML = `<span class="drag-handle" title="Drag to reorder">&#8942;&#8942;</span>
      <input type="text" class="party-name-input" maxlength="300" placeholder="Party name">
      <button type="button" class="btn card-delete-btn" title="Remove party">&#10005;</button>`;
    const input = card.querySelector("input");
    input.value = party.name;
    input.addEventListener("input", () => {
      party.name = input.value;
      renderCauseTitle();
      scheduleSave();
    });
    // Dragging from inside the text field would otherwise start a drag
    // instead of selecting text.
    input.addEventListener("mousedown", () => { card.draggable = false; });
    input.addEventListener("blur", () => { card.draggable = true; });
    card.querySelector(".card-delete-btn").addEventListener("click", () => {
      data.parties = data.parties.filter((p) => p !== party);
      card.remove();
      renderCauseTitle();
      scheduleSave();
    });
    return card;
  }

  // The DOM order within each column is the order; rebuild data.parties from it.
  function reorderPartiesFromDom() {
    const byId = new Map(data.parties.map((p) => [p.id, p]));
    data.parties = SIDES.flatMap(({ side }) =>
      [...bodyEl.querySelectorAll(`.party-list[data-side="${side}"] .party-card`)].map((c) => byId.get(c.dataset.id)));
    renderCauseTitle();
    scheduleSave();
  }

  // -------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------
  function render() {
    document.getElementById("caseTitle").textContent = data.name || "Case details";
    document.title = `Case details – ${data.name || ""}`;
    bodyEl.innerHTML = `
      <div class="case-identity-fields">
        <label class="modal-field">Court
          <input type="text" id="courtInput" maxlength="200" placeholder="e.g. Bangalore Rural and Urban 1st Additional District Consumer Dispute Redressal Commission">
        </label>
        <label class="modal-field">Court location
          <input type="text" id="courtLocationInput" maxlength="200" placeholder="e.g. Bangalore, Karnataka">
        </label>
        <label class="modal-field">Case number
          <input type="text" id="caseNumberInput" maxlength="100" placeholder="e.g. DC/AB4/525/CC/101/2025">
        </label>
        <label class="modal-field">Case role
          <select id="caseRoleInput">
            ${CASE_ROLES.map((r) => `<option value="${r}">${r}</option>`).join("")}
          </select>
        </label>
        <label class="toggle-label"><input type="checkbox" id="partyInPersonInput"> Is party in person</label>
      </div>
      <div class="allegations-column-head"><h2>Cause title</h2></div>
      <div class="cause-title-controls">
        <label>Template <select id="templateSelect"></select></label>
        <label class="toggle-label"><input type="checkbox" id="oneLinePartiesInput"> Short party names</label>
      </div>
      <div class="cause-title-actions">
        <span class="hint" id="causeTitleHint"></span>
      </div>
      <div class="cause-title-editor" id="causeTitleEditor"></div>
      <div class="cause-title-actions cause-title-actions-below">
        <button type="button" class="btn" id="saveCauseTitleBtn" title="Save (Ctrl+S / Cmd+S)">Save</button>
        <button type="button" class="btn" id="resetCauseTitleBtn" title="Discard your edits and regenerate the title from the template">Clear changes</button>
      </div>
      <div class="parties-columns" id="partiesColumns"></div>`;

    for (const [id, key] of [["courtInput", "court"], ["courtLocationInput", "court_location"], ["caseNumberInput", "case_number"]]) {
      const input = bodyEl.querySelector("#" + id);
      input.value = data[key];
      input.addEventListener("input", () => {
        data[key] = input.value;
        renderCauseTitle();
        scheduleSave();
      });
    }

    const roleSelect = bodyEl.querySelector("#caseRoleInput");
    roleSelect.value = data.case_role || CASE_ROLES[0];
    roleSelect.addEventListener("change", () => {
      data.case_role = roleSelect.value;
      scheduleSave(roleSelect);
    });

    const inPersonInput = bodyEl.querySelector("#partyInPersonInput");
    inPersonInput.checked = !!data.party_in_person;
    inPersonInput.addEventListener("change", () => {
      data.party_in_person = inPersonInput.checked;
      scheduleSave();
    });

    const templateSelect = bodyEl.querySelector("#templateSelect");
    templateSelect.innerHTML = templates.map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name)}</option>`).join("");
    const current = window.CauseTitle.currentTemplate(data, templates);
    if (current) templateSelect.value = current.id;
    templateSelect.addEventListener("change", () => {
      data.cause_title_template_id = templateSelect.value;
      renderCauseTitle();
      scheduleSave(templateSelect);
    });

    const oneLineInput = bodyEl.querySelector("#oneLinePartiesInput");
    oneLineInput.checked = !!data.cause_title_one_line_parties;
    oneLineInput.addEventListener("change", () => {
      if (data.cause_title_doc) {
        if (!confirm("This regenerates the cause title from the template and discards your changes to it. Continue?")) {
          oneLineInput.checked = !oneLineInput.checked;
          return;
        }
        data.cause_title_doc = null;
      }
      data.cause_title_one_line_parties = oneLineInput.checked;
      renderCauseTitle();
      scheduleSave();
    });

    const columnsEl = bodyEl.querySelector("#partiesColumns");
    for (const { side, heading, add } of SIDES) {
      const col = document.createElement("section");
      col.className = "parties-column";
      col.innerHTML = `<div class="allegations-column-head"><h2>${heading}</h2>
        <button type="button" class="btn add-party-btn">${add}</button></div>
        <div class="party-list" data-side="${side}"></div>`;
      const listEl = col.querySelector(".party-list");
      data.parties.filter((p) => p.side === side).forEach((p) => listEl.appendChild(buildPartyCard(p)));
      col.querySelector(".add-party-btn").addEventListener("click", () => {
        const party = { id: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now() + Math.random())).replace(/-/g, "").slice(0, 12), side, name: "" };
        data.parties.push(party);
        const card = buildPartyCard(party);
        listEl.appendChild(card);
        card.querySelector("input").focus();
      });
      enableDragReorder(listEl, reorderPartiesFromDom);
      columnsEl.appendChild(col);
    }

    const editorEl = bodyEl.querySelector("#causeTitleEditor");
    titleEditor = window.createTitleTemplateEditor(editorEl, JSON.stringify(data.cause_title_doc || generateCauseDoc()),
      () => { if (canEdit()) saveNow(editorEl); }, () => { if (canEdit()) onCauseTitleEdited(); });
    bodyEl.querySelector("#resetCauseTitleBtn").addEventListener("click", resetCauseTitle);
    bodyEl.querySelector("#saveCauseTitleBtn").addEventListener("click", () => saveNow(editorEl));
    renderCauseTitle();
    if (!canEdit()) {
      window.ReadOnlyLock.lock(bodyEl);
      titleEditor.setEditable(false);
    }
  }

  // The report editor sends people here (with ?notice=no-cause-title) when
  // they ask to insert a cause title that has not been generated yet.
  function showNotice() {
    const params = new URLSearchParams(location.search);
    if (params.get("notice") !== "no-cause-title") return;
    const el = document.createElement("div");
    el.className = "flash-notice";
    el.textContent = "No cause title has been generated for this case yet. Fill in the court, case number and parties, choose a template, and then go back to your report to insert it.";
    bodyEl.parentNode.insertBefore(el, bodyEl);
    params.delete("notice");
    const qs = params.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  }

  async function load() {
    try {
      const [caseRes, templatesRes] = await Promise.all([fetch(caseUrl), fetch(templatesUrl)]);
      if (!caseRes.ok) throw new Error((await caseRes.json().catch(() => ({}))).error || caseRes.statusText);
      data = await caseRes.json();
      data.parties = Array.isArray(data.parties) ? data.parties : [];
      data.cause_title_doc = data.cause_title_doc ? JSON.parse(data.cause_title_doc) : null;
      templates = templatesRes.ok ? await templatesRes.json() : [];
      render();
      showNotice();
    } catch (e) {
      bodyEl.innerHTML = `<p class="empty">Failed to load case: ${escapeHtml(e.message)}</p>`;
    }
  }

  load();
})();
