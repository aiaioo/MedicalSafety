// Administrator's editing view of the public page (templates/public.html,
// admin_edit): the page as the public sees it, with sections that can be
// added (pop-up), edited, deleted and dragged into order, and article cards
// that can be dragged between sections. Plain fetch() against /api/admin/*
// -- no build step.
(function () {
  const urls = window.PA_URLS;
  const data = JSON.parse(document.getElementById("paData").textContent);
  const websiteId = data.websiteId;
  let sections = data.layout.sections;
  let unplaced = data.layout.unplaced;
  const sectionsEl = document.getElementById("paSections");
  const legalEl = document.getElementById("paLegal");
  const trayEl = document.getElementById("paTray");
  const trayGrid = document.getElementById("paTrayGrid");

  const fill = (tpl, map) => Object.keys(map).reduce((u, k) => u.replace(k, encodeURIComponent(map[k])), tpl);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  async function api(url, method, body) {
    const res = await fetch(url, {
      method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(json.error || res.statusText);
    return json;
  }
  const fail = (what) => (e) => window.alert("Could not " + what + ": " + e.message);

  // ---- pop-up form --------------------------------------------------
  const modal = document.getElementById("paModal");
  const modalFields = document.getElementById("paModalFields");
  const modalError = document.getElementById("paModalError");
  let modalSubmit = null;
  // fields: [{name, label, value, multiline}]; onSubmit(values) may throw.
  function openForm(title, fields, okLabel, onSubmit) {
    document.getElementById("paModalTitle").textContent = title;
    document.getElementById("paModalOk").textContent = okLabel;
    modalFields.innerHTML = "";
    modalError.style.display = "none";
    for (const f of fields) {
      const label = el("label", "modal-field", f.label);
      const input = el(f.multiline ? "textarea" : "input");
      if (f.multiline) input.rows = 3;
      input.name = f.name;
      input.value = f.value || "";
      if (f.placeholder) input.placeholder = f.placeholder;
      label.appendChild(input);
      modalFields.appendChild(label);
    }
    modalSubmit = async () => {
      const values = {};
      modalFields.querySelectorAll("input, textarea").forEach((i) => { values[i.name] = i.value.trim(); });
      try {
        await onSubmit(values);
        modal.classList.remove("open");
      } catch (e) {
        modalError.textContent = e.message;
        modalError.style.display = "block";
      }
    };
    modal.classList.add("open");
    const first = modalFields.querySelector("input, textarea");
    if (first) first.focus();
  }
  document.getElementById("paModalOk").addEventListener("click", () => modalSubmit && modalSubmit());
  document.getElementById("paModalCancel").addEventListener("click", () => modal.classList.remove("open"));
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.remove("open"); });
  modalFields.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT") { e.preventDefault(); modalSubmit(); }
  });

  // ---- drag state ---------------------------------------------------
  let drag = null; // {kind: "article", id, from} or {kind: "section", id}

  function wireConfirmDelete(btn, onConfirm) {
    btn.addEventListener("click", () => {
      if (!btn.classList.contains("confirming")) {
        btn.classList.add("confirming");
        btn.textContent = "Confirm?";
        btn._t = setTimeout(() => { btn.classList.remove("confirming"); btn.innerHTML = "&#10005;"; }, 3000);
        return;
      }
      clearTimeout(btn._t);
      onConfirm();
    });
  }

  // Same markup as the public page's cards, as a div so a click doesn't navigate.
  function makeCard(a, fromSectionId) {
    const card = el("div", "article-card pa-card");
    card.draggable = true;
    if (a.thumbnail_image_id) {
      const img = el("img", "thumbnail");
      img.draggable = false;
      img.alt = "";
      img.src = data.imageBase.replace("__A__", encodeURIComponent(a.id)).replace("__I__", encodeURIComponent(a.thumbnail_image_id));
      card.appendChild(img);
    }
    const body = el("div", "card-body");
    if (!a.published) body.appendChild(el("span", "draft", "Draft"));
    body.appendChild(el("div", "title", a.title));
    if (a.summary) body.appendChild(el("p", "summary", a.summary));
    body.appendChild(el("div", "meta", "Updated " + (a.updated_at || "").slice(0, 10)));
    card.appendChild(body);
    card.addEventListener("dragstart", (e) => {
      drag = { kind: "article", id: a.id, from: fromSectionId };
      card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "copyMove";
      e.dataTransfer.setData("text/plain", a.id);
      e.stopPropagation();
    });
    card.addEventListener("dragend", () => { drag = null; card.classList.remove("dragging"); });
    return card;
  }

  async function dropArticle(toSectionId, copy) {
    const { id, from } = drag;
    if (from === toSectionId) return;
    const keepFrom = copy && toSectionId;
    try {
      await api(fill(urls.move, { __A__: id }), "POST", { to: toSectionId, from: keepFrom ? null : from });
    } catch (e) { return fail("move the article")(e); }
    let article;
    for (const s of sections) {
      const i = s.articles.findIndex((x) => x.id === id);
      if (i < 0) continue;
      article = article || s.articles[i];
      if (s.id === from && !keepFrom) s.articles.splice(i, 1);
    }
    const ui = unplaced.findIndex((x) => x.id === id);
    if (ui >= 0) { article = unplaced[ui]; unplaced.splice(ui, 1); }
    if (toSectionId) {
      const target = sections.find((s) => s.id === toSectionId);
      if (!target.articles.some((x) => x.id === id)) target.articles.push(article);
    } else if (!sections.some((s) => s.articles.some((x) => x.id === id))) {
      article.published = false; // the server unpublishes an article left in no section
      unplaced.unshift(article);
    }
    render();
  }

  function dropZone(node, toSectionId) {
    node.addEventListener("dragover", (e) => {
      if (!drag || drag.kind !== "article") return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = e.altKey ? "copy" : "move";
      node.classList.add("drop-target");
    });
    node.addEventListener("dragleave", (e) => { if (!node.contains(e.relatedTarget)) node.classList.remove("drop-target"); });
    node.addEventListener("drop", (e) => {
      if (!drag || drag.kind !== "article") return;
      e.preventDefault();
      e.stopPropagation();
      node.classList.remove("drop-target");
      dropArticle(toSectionId, e.altKey);
    });
  }

  function editSection(sec) {
    openForm("Edit section", [
      { name: "title", label: "Title", value: sec.title },
      { name: "description", label: "Description", value: sec.description, multiline: true },
    ], "Save", async (v) => {
      if (!v.title) throw new Error("A section title is required");
      await api(fill(urls.section, { __S__: sec.id }), "POST", {
        title: v.title, description: v.description, position: sections.indexOf(sec),
      });
      sec.title = v.title;
      sec.description = v.description;
      render();
    });
  }

  function appendCards(container, sec) {
    for (const a of sec.articles) container.appendChild(makeCard(a, sec.id));
    if (!sec.articles.length) container.appendChild(el("div", "pa-empty", "Drag articles here."));
  }

  function buildSection(sec) {
    const wrap = el("section", "pa-section public-section");
    const head = el("div", "pa-section-head");
    head.innerHTML = '<span class="pa-handle" title="Drag to reorder">&#8942;&#8942;</span><h2></h2>' +
      '<button type="button" class="pa-edit-link">Edit</button>' +
      '<button type="button" class="card-delete-btn" title="Delete section (its articles become unplaced here)">&#10005;</button>';
    head.querySelector("h2").textContent = sec.title;
    head.querySelector(".pa-edit-link").addEventListener("click", () => editSection(sec));
    wrap.appendChild(head);
    if (sec.description) wrap.appendChild(el("p", "public-section-description", sec.description));
    const grid = el("div", "article-card-grid");
    appendCards(grid, sec);
    wrap.appendChild(grid);
    dropZone(wrap, sec.id);

    wireConfirmDelete(head.querySelector(".card-delete-btn"), async () => {
      try { await api(fill(urls.section, { __S__: sec.id }), "DELETE"); } catch (e) { return fail("delete the section")(e); }
      window.location.reload();
    });
    const handle = head.querySelector(".pa-handle");
    handle.addEventListener("mousedown", () => { wrap.draggable = true; });
    handle.addEventListener("mouseup", () => { if (!drag) wrap.draggable = false; });
    wrap.addEventListener("dragend", () => { wrap.draggable = false; wrap.classList.remove("dragging"); if (drag && drag.kind === "section") drag = null; });
    wrap.addEventListener("dragstart", (e) => {
      if (!wrap.draggable || drag) return;
      drag = { kind: "section", id: sec.id };
      wrap.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", sec.id);
    });
    wrap.addEventListener("dragover", (e) => {
      if (!drag || drag.kind !== "section" || drag.id === sec.id) return;
      e.preventDefault();
      const r = wrap.getBoundingClientRect();
      const before = e.clientY < r.top + r.height / 2;
      const from = sections.findIndex((s) => s.id === drag.id);
      let to = sections.indexOf(sec) + (before ? 0 : 1);
      if (from < to) to--;
      if (from === to) return;
      const [moved] = sections.splice(from, 1);
      sections.splice(to, 0, moved);
      sectionsEl.insertBefore(sectionsEl.querySelector(".pa-section.dragging"), before ? wrap : wrap.nextSibling);
    });
    wrap.addEventListener("drop", (e) => {
      if (!drag || drag.kind !== "section") return;
      e.preventDefault();
      api(fill(urls.order, { __W__: websiteId }), "PUT", {
        ids: sections.filter((s) => s.kind !== "legal_tools").map((s) => s.id),
      }).catch(fail("save the section order"));
    });
    return wrap;
  }

  // The top-right sign-in panel, as on the public page (inert here), with
  // its own title/description and articles.
  function renderLegal() {
    const sec = sections.find((s) => s.kind === "legal_tools");
    legalEl.innerHTML = "";
    if (!sec) { legalEl.style.display = "none"; return; }
    const h = el("h2", null, sec.title);
    const edit = el("button", "pa-edit-link", "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => editSection(sec));
    h.appendChild(edit);
    legalEl.appendChild(h);
    if (sec.description) legalEl.appendChild(el("p", "legal-tools-description", sec.description));
    legalEl.appendChild(el("p", "legal-tools-description", "(Sign-in form appears here for visitors.)"));
    const list = el("div", "legal-tools-articles");
    for (const a of sec.articles) {
      const row = el("div", "legal-tools-article pa-card", a.title + (a.published ? "" : " (draft)"));
      row.draggable = true;
      row.addEventListener("dragstart", (e) => {
        drag = { kind: "article", id: a.id, from: sec.id };
        e.dataTransfer.effectAllowed = "copyMove";
        e.dataTransfer.setData("text/plain", a.id);
      });
      row.addEventListener("dragend", () => { drag = null; });
      list.appendChild(row);
    }
    // Articles can't be dropped here (it is only for signing in), but any
    // already placed can still be dragged out.
    if (sec.articles.length) legalEl.appendChild(list);
  }

  function render() {
    sectionsEl.innerHTML = "";
    for (const s of sections.filter((x) => x.kind !== "legal_tools")) sectionsEl.appendChild(buildSection(s));
    if (!sectionsEl.children.length) sectionsEl.appendChild(el("p", "pa-empty", "No sections yet. Use + Add section."));
    renderLegal();
    trayGrid.innerHTML = "";
    for (const a of unplaced) trayGrid.appendChild(makeCard(a, null));
    if (!unplaced.length) trayGrid.appendChild(el("div", "pa-empty", "Every article is on this website."));
  }
  dropZone(trayEl, null);

  // ---- controls -----------------------------------------------------
  document.getElementById("paSite").addEventListener("change", (e) => {
    window.location.href = urls.self + "?site=" + encodeURIComponent(e.target.value);
  });

  document.getElementById("paAddSection").addEventListener("click", () => {
    openForm("Add a section", [
      { name: "title", label: "Title" },
      { name: "description", label: "Description", multiline: true },
    ], "Add", async (v) => {
      if (!v.title) throw new Error("A section title is required");
      const s = await api(fill(urls.sections, { __W__: websiteId }), "POST", { title: v.title, description: v.description });
      sections.push({ id: s.id, title: s.title, description: s.description, kind: "section", articles: [] });
      render();
    });
  });

  document.getElementById("paAddWebsite").addEventListener("click", () => {
    openForm("Add a website", [
      { name: "domain", label: "Domain", placeholder: "example.com" },
      { name: "name", label: "Name" },
      { name: "tagline", label: "Tagline (shown under the name)" },
    ], "Add", async (v) => {
      if (!v.domain || !v.name) throw new Error("Enter both a domain and a name.");
      const w = await api(urls.websites, "POST", { domain: v.domain.toLowerCase(), name: v.name, tagline: v.tagline });
      window.location.href = urls.self + "?site=" + encodeURIComponent(w.id);
    });
  });

  document.getElementById("paEditSite").addEventListener("click", () => {
    openForm("Edit website", [
      { name: "name", label: "Name", value: document.getElementById("paName").textContent },
      { name: "tagline", label: "Tagline", value: document.getElementById("paTagline").textContent },
    ], "Save", async (v) => {
      if (!v.name) throw new Error("A website name is required");
      await api(fill(urls.website, { __W__: websiteId }), "POST", { name: v.name, tagline: v.tagline });
      document.getElementById("paName").textContent = v.name;
      document.getElementById("paTagline").textContent = v.tagline;
      const opt = document.querySelector('#paSite option[value="' + CSS.escape(websiteId) + '"]');
      if (opt) opt.textContent = v.name + opt.textContent.slice(opt.textContent.lastIndexOf(" ("));
    });
  });

  render();
})();
