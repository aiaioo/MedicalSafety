// "Share..." for a cause, case, allegation, report or document: a modal in which the signed-in owner (or a
// collaborator with edit access) creates secret keys for it, and sees, copies and deletes the ones it has.
// Pages call ShareKey.button(kind, id, {enabled}) for the button, or ShareKey.open(kind, id) directly. Visitors
// who got in with a key never get a working button: the server refuses them too (require_key_manager).
(function () {
  const NOUNS = {cause: "cause", case: "case", allegation: "allegation", report: "report", source: "document"};
  let overlay = null;
  let current = null; // {kind, id}
  let els = {};

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
      el("option", {value: "viewer", textContent: "Can view"}),
      el("option", {value: "editor", textContent: "Can edit"}),
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
    return el("div", {className: "key-row" + (k.active ? "" : " inactive")}, [
      el("span", {className: "collab-role", textContent: k.permission === "editor" ? "Can edit" : "Can view"}),
      el("code", {className: "key-value", textContent: k.key}),
      el("a", {className: "key-link", href: k.url, textContent: "link", title: k.url}),
      el("span", {className: "collab-actions"}, [copier("Copy link", new URL(k.url, window.location.href).href), copier("Copy key", k.key), del]),
    ]);
  }

  async function refresh() {
    const data = await api(keysUrl(current));
    els.list.replaceChildren.apply(els.list, data.keys.map(keyRow));
    els.empty.style.display = data.keys.length ? "none" : "";
  }

  function open(kind, id) {
    if (!overlay) build();
    current = {kind: kind, id: id};
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
