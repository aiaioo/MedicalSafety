(function () {
  const section = document.getElementById("keySection");
  if (!section) return;
  const keysUrl = section.dataset.keysUrl;
  const itemUrlBase = section.dataset.itemUrlBase; // .../key/KIND/0
  const KIND_LABELS = {cause: "Causes", case: "Cases", allegation: "Allegations", report: "Reports", source: "Documents"};
  const kindEl = document.getElementById("keyKind");
  const objectEl = document.getElementById("keyObject");
  const permEl = document.getElementById("keyPermission");
  const errorEl = document.getElementById("keyError");
  const listEl = document.getElementById("keyList");
  const emptyEl = document.getElementById("keyEmpty");
  let shareable = {};

  function itemUrl(kind, id) { return itemUrlBase.replace("KIND", kind).replace(/0$/, String(id)); }
  function showError(msg) { errorEl.textContent = msg; errorEl.style.display = msg ? "" : "none"; }

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

  function fillObjects() {
    const objs = shareable[kindEl.value] || [];
    objectEl.replaceChildren.apply(objectEl, objs.length
      ? objs.map(function (o) { return el("option", {value: o.id, textContent: o.title}); })
      : [el("option", {value: "", textContent: "You don't own any of these"})]);
  }
  kindEl.addEventListener("change", fillObjects);

  function keyRow(kind, k) {
    function copier(label, text) {
      const b = el("button", {className: "btn", type: "button", textContent: label});
      b.addEventListener("click", function () {
        navigator.clipboard.writeText(text).then(function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = label; }, 1500); });
      });
      return b;
    }
    const copyLink = copier("Copy link", new URL(k.url, window.location.href).href);
    const copy = copier("Copy key", k.key);
    const del = el("button", {className: "card-delete-btn", type: "button", textContent: "\u{1F5D1}", title: "Delete key"});
    del.addEventListener("click", function () {
      if (!del.classList.contains("confirming")) {
        del.classList.add("confirming");
        del.textContent = "Confirm?";
        del._confirmTimer = setTimeout(function () {
          del.classList.remove("confirming");
          del.textContent = "\u{1F5D1}";
        }, 3000);
        return;
      }
      clearTimeout(del._confirmTimer);
      act(del, function () { return api(itemUrl(kind, k.id), "DELETE"); });
    });
    const perm = el("select", {title: "Change permission", "aria-label": "Permission for this key"}, [
      el("option", {value: "viewer", textContent: "Can view"}),
      el("option", {value: "editor", textContent: "Can edit"}),
    ]);
    perm.value = k.permission;
    perm.addEventListener("change", function () {
      perm.disabled = true;
      showError("");
      api(itemUrl(kind, k.id), "PATCH", {permission: perm.value})
        .then(refresh)
        .catch(function (e) { showError(e.message); perm.value = k.permission; perm.disabled = false; });
    });
    return el("div", {className: "key-row" + (k.active ? "" : " inactive")}, [
      el("span", {className: "key-title", textContent: k.title, title: k.title}),
      perm,
      el("code", {className: "key-value", textContent: k.key}),
      el("a", {className: "key-link", href: k.url, textContent: "link", title: k.url}),
      el("span", {className: "collab-actions"}, [copyLink, copy, del]),
    ]);
  }

  async function act(btn, fn) {
    btn.disabled = true;
    try { await fn(); await refresh(); } catch (e) { showError(e.message); btn.disabled = false; }
  }

  document.getElementById("keyForm").addEventListener("submit", async function (ev) {
    ev.preventDefault();
    showError("");
    const btn = document.getElementById("keyBtn");
    btn.disabled = true;
    try {
      await api(keysUrl, "POST", {kind: kindEl.value, object_id: objectEl.value, permission: permEl.value});
      await refresh();
    } catch (e) { showError(e.message); }
    finally { btn.disabled = false; }
  });

  async function refresh() {
    const data = await api(keysUrl);
    shareable = data.shareable;
    const prev = objectEl.value;
    fillObjects();
    if (prev) objectEl.value = prev;
    const groups = [];
    Object.keys(KIND_LABELS).forEach(function (kind) {
      const items = data.keys[kind] || [];
      if (!items.length) return;
      groups.push(el("div", {className: "key-group"}, [el("h4", {textContent: KIND_LABELS[kind]})].concat(items.map(function (k) { return keyRow(kind, k); }))));
    });
    listEl.replaceChildren.apply(listEl, groups);
    emptyEl.style.display = groups.length ? "none" : "";
  }

  refresh().catch(function (e) { showError(e.message); });
})();
