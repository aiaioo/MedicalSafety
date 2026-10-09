// "Share..." for a cause, case, allegation, report or document: a modal in which the signed-in owner (or a
// collaborator with edit access) creates secret keys for it, and sees, copies and deletes the ones it has.
// Pages call ShareKey.button(kind, id, {enabled}) for the button, or ShareKey.open(kind, id) directly. Visitors
// who got in with a key never get a working button: the server refuses them too (require_key_manager).
(function () {
  const NOUNS = {cause: "cause", case: "case", allegation: "allegation", report: "report", source: "document"};
  const PERMISSION_LABELS = {viewer: "Can view", creator: "Can view and create", editor: "Can edit"};
  let overlay = null;
  let current = null; // {kind, id}
  let els = {};
  let editableReports = []; // reports under the current object that the user may let a link edit

  function el(tag, opts, children) {
    const e = document.createElement(tag);
    Object.assign(e, opts || {});
    (children || []).forEach(function (c) { e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return e;
  }

  async function api(url, method, body) {
    const r = await fetch(url, {
      method: method || "GET",
      headers: body ? {"Content-Type": "application/json"} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (!r.ok) throw new Error(data.error || "Something went wrong (" + r.status + ").");
    return data;
  }

  const keysUrl = function (c) { return "/api/keys/" + encodeURIComponent(c.kind) + "/" + encodeURIComponent(c.id); };
  const itemUrl = function (kind, keyId) { return "/api/key/" + encodeURIComponent(kind) + "/" + keyId; };

  function showError(msg) { els.error.textContent = msg; els.error.style.display = msg ? "" : "none"; }

  function build() {
    els.title = el("h3");
    els.perm = el("select", {id: "shareKeyPermission"}, [
      el("option", {value: "viewer", textContent: PERMISSION_LABELS.viewer}),
      el("option", {value: "editor", textContent: PERMISSION_LABELS.editor}),
    ]);
    els.create = el("button", {type: "button", className: "btn", textContent: "Create key"});
    els.error = el("p", {className: "error-message", style: "display:none"});
    els.list = el("div");
    els.empty = el("p", {className: "empty", textContent: "No keys yet."});
    const close = el("button", {type: "button", textContent: "Close"});
    const form = el("div", {className: "key-form"}, [
      el("label", {className: "modal-field"}, ["Permission", els.perm]),
      els.create,
    ]);
    overlay = el("div", {className: "modal-overlay", id: "shareKeyModal"}, [
      el("div", {className: "modal modal-wide"}, [
        els.title,
        el("p", {className: "modal-hint", textContent: "Anyone with a key can open this (and everything beneath it) without an account, after solving a captcha. Delete a key to stop sharing."}),
        form, els.error, els.empty, els.list,
        el("div", {className: "modal-actions"}, [close]),
      ]),
    ]);
    document.body.appendChild(overlay);
    close.addEventListener("click", function () { overlay.classList.remove("open"); });
    overlay.addEventListener("mousedown", function (e) { if (e.target === overlay) overlay.classList.remove("open"); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") overlay.classList.remove("open"); });
    els.create.addEventListener("click", async function () {
      showError("");
      els.create.disabled = true;
      try {
        await api(keysUrl(current), "POST", {permission: els.perm.value});
        await refresh();
      } catch (e) { showError(e.message); }
      finally { els.create.disabled = false; }
    });
  }

  function keyRow(k) {
    function copier(label, text) {
      const b = el("button", {className: "btn", type: "button", textContent: label});
      b.addEventListener("click", function () {
        navigator.clipboard.writeText(text).then(function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = label; }, 1500); });
      });
      return b;
    }
    const del = el("button", {className: "card-delete-btn", type: "button", textContent: "\u{1F5D1}", title: "Delete key"});
    del.addEventListener("click", function () {
      if (!del.classList.contains("confirming")) {
        del.classList.add("confirming");
        del.textContent = "Confirm?";
        del._confirmTimer = setTimeout(function () { del.classList.remove("confirming"); del.textContent = "\u{1F5D1}"; }, 3000);
        return;
      }
      clearTimeout(del._confirmTimer);
      del.disabled = true;
      api(itemUrl(current.kind, k.id), "DELETE").then(refresh).catch(function (e) { showError(e.message); del.disabled = false; });
    });
    const row = el("div", {className: "key-row" + (k.active ? "" : " inactive")}, [
      el("span", {className: "collab-role", textContent: PERMISSION_LABELS[k.permission] || "Can view"}),
      el("code", {className: "key-value", textContent: k.key}),
      el("a", {className: "key-link", href: k.url, textContent: "link", title: k.url}),
      el("span", {className: "collab-actions"}, [copier("Copy link", new URL(k.url, window.location.href).href), copier("Copy key", k.key), del]),
    ]);
    if (canPickReports(k.permission)) row.appendChild(keyReportsEditor(k));
    return row;
  }

  // A link can let its holders edit particular reports beneath its object (and the documents those reports draw on)
  // even though the link itself only views. Only reports the user can edit are offered.
  function canPickReports(permission) { return current.kind !== "report" && current.kind !== "source" && permission === "viewer"; }

  function reportChecklist(selected) {
    const box = el("div", {className: "key-report-list"});
    if (!editableReports.length) {
      box.appendChild(el("p", {className: "empty", textContent: "No reports under this that you can edit."}));
      return box;
    }
    editableReports.forEach(function (r) {
      const cb = el("input", {type: "checkbox", value: r.id, checked: selected.indexOf(r.id) >= 0});
      box.appendChild(el("label", {className: "key-report-option"}, [cb, " " + r.name]));
    });
    return box;
  }

  function checkedReports(container) {
    return Array.prototype.map.call(container.querySelectorAll("input:checked"), function (cb) { return cb.value; });
  }

  // Ticking or unticking a report saves straight away and flashes the box green; if the save fails the tick is undone.
  function keyReportsEditor(k) {
    const list = reportChecklist(k.edit_reports || []);
    const summary = el("summary");
    const box = el("details", {className: "key-edit-reports", open: (k.edit_reports || []).length > 0}, [summary, list]);
    function describe() {
      const n = (k.edit_reports || []).length;
      summary.textContent = n ? "Can also edit " + n + " report" + (n === 1 ? "" : "s") + " (and the documents they draw on)"
                              : "Also allow editing particular reports\u2026";
    }
    describe();
    list.addEventListener("change", async function (e) {
      const cb = e.target;
      const wanted = checkedReports(list);
      list.querySelectorAll("input").forEach(function (i) { i.disabled = true; });
      try {
        await api(itemUrl(current.kind, k.id) + "/reports", "PUT", {edit_reports: wanted});
        k.edit_reports = wanted;
        describe();
        showError("");
        box.classList.remove("save-flash");
        void box.offsetWidth; // restart the animation
        box.classList.add("save-flash");
      } catch (err) {
        cb.checked = !cb.checked;
        showError(err.message);
      } finally {
        list.querySelectorAll("input").forEach(function (i) { i.disabled = false; });
      }
    });
    return box;
  }

  async function refresh() {
    const data = await api(keysUrl(current));
    editableReports = data.editable_reports || [];
    els.list.replaceChildren.apply(els.list, data.keys.map(keyRow));
    els.empty.style.display = data.keys.length ? "none" : "";
  }

  function open(kind, id) {
    if (!overlay) build();
    current = {kind: kind, id: id};
    // A link can only allow editing a report or document: anyone may hold a link, so nothing wider.
    const editOption = els.perm.querySelector('option[value="editor"]');
    editableReports = [];
    editOption.hidden = editOption.disabled = kind !== "report" && kind !== "source";
    if (editOption.disabled && els.perm.value === "editor") els.perm.value = "viewer";
    els.title.textContent = "Share this " + (NOUNS[kind] || "item");
    showError("");
    els.list.replaceChildren();
    overlay.classList.add("open");
    refresh().catch(function (e) { showError(e.message); });
  }

  // A "Share..." button. `enabled` is false for someone who may not manage keys (view-only access, or a key
  // visitor); the button then stays but is disabled.
  function button(kind, id, opts) {
    const enabled = !(opts && opts.enabled === false) && !document.body.dataset.guest;
    const b = el("button", {type: "button", className: "btn share-btn", textContent: "Share…"});
    if (!enabled) {
      b.disabled = true;
      b.title = "Only the owner, or a collaborator with edit access, can share this";
    } else {
      b.title = "Share this " + (NOUNS[kind] || "item") + " with a secret key";
      b.addEventListener("click", function () { open(kind, id); });
    }
    return b;
  }

  window.ShareKey = {open: open, button: button};
})();
