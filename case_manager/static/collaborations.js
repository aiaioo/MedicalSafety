(function () {
  const appEl = document.getElementById("collabApp");
  if (!appEl) return;
  const listUrl = appEl.dataset.listUrl;
  const captchaUrl = appEl.dataset.captchaUrl;
  const seenUrl = appEl.dataset.seenUrl;
  const itemUrlBase = appEl.dataset.itemUrlBase; // ends in /0
  const openUrls = JSON.parse(appEl.dataset.openUrls); // where each kind opens; cases, reports and documents take the id on the end
  const KIND_LABELS = {cause: "Causes", case: "Cases", allegation: "Allegations", report: "Reports", source: "Documents"};
  const KINDS = Object.keys(KIND_LABELS);

  const form = document.getElementById("inviteForm");
  const emailEl = document.getElementById("inviteEmail");
  const captchaImg = document.getElementById("captchaImg");
  const captchaAnswer = document.getElementById("captchaAnswer");
  const errorEl = document.getElementById("inviteError");
  const okEl = document.getElementById("inviteOk");
  const listEl = document.getElementById("collabList");
  const emptyEl = document.getElementById("collabEmpty");
  let captchaId = "";
  let shareable = {};

  function itemUrl(id, suffix) { return itemUrlBase.replace(/0$/, String(id)) + (suffix || ""); }

  // Causes and allegations have no page of their own: their list pages show everything shared with the user.
  function openUrl(kind, id) {
    const base = openUrls[kind];
    return /[=]$/.test(base) ? base + encodeURIComponent(id) : base;
  }

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

  async function loadCaptcha() {
    captchaAnswer.value = "";
    try {
      const c = await api(captchaUrl);
      captchaId = c.id;
      captchaImg.src = c.image;
    } catch (e) { showError(e.message); }
  }

  function showError(msg) { okEl.style.display = "none"; errorEl.textContent = msg; errorEl.style.display = msg ? "" : "none"; }

  document.getElementById("captchaRefresh").addEventListener("click", loadCaptcha);

  form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    showError("");
    const btn = document.getElementById("inviteBtn");
    btn.disabled = true;
    try {
      await api(listUrl, "POST", {email: emailEl.value, captcha_id: captchaId, captcha_answer: captchaAnswer.value});
      okEl.textContent = "Invitation sent to " + emailEl.value.trim() + ".";
      okEl.style.display = "";
      errorEl.style.display = "none";
      emailEl.value = "";
      await refresh();
    } catch (e) {
      showError(e.message);
    } finally {
      btn.disabled = false;
      loadCaptcha(); // each challenge works once, win or lose
    }
  });

  function fmtTime(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleString();
  }

  function accessList(title, shared) {
    const box = el("div", {className: "collab-access"}, [el("h4", {textContent: title})]);
    let any = false;
    KINDS.forEach(function (kind) {
      const items = shared[kind] || [];
      if (!items.length) return;
      any = true;
      const ul = el("ul");
      items.forEach(function (o) {
        const note = o.direct ? o.role : o.role + ", inherited from a parent";
        ul.appendChild(el("li", {}, [el("a", {href: openUrl(kind, o.id), textContent: o.title}), " ", el("span", {className: "collab-role", textContent: "(" + note + ")"})]));
      });
      box.appendChild(el("div", {}, [el("span", {className: "kind", textContent: KIND_LABELS[kind]}), ul]));
    });
    if (!any) box.appendChild(el("p", {className: "empty", textContent: "Nothing."}));
    return box;
  }

  function editor(card, onDone) {
    const wrap = el("div", {className: "collab-access"});
    const selects = {}; // kind -> [{id, select}]
    const byKind = {};
    KINDS.forEach(function (kind) {
      byKind[kind] = {};
      (card.shared_by_me[kind] || []).forEach(function (o) { byKind[kind][o.id] = o; });
      const objs = shareable[kind] || [];
      if (!objs.length) return;
      wrap.appendChild(el("h4", {textContent: KIND_LABELS[kind]}));
      selects[kind] = [];
      objs.forEach(function (o) {
        const sel = el("select", {}, [
          el("option", {value: "", textContent: "No access"}),
          el("option", {value: "viewer", textContent: "Can view"}),
          el("option", {value: "editor", textContent: "Can edit"}),
        ]);
        const cur = byKind[kind][o.id];
        sel.value = (cur && cur.direct) || "";
        selects[kind].push({id: o.id, select: sel});
        const row = el("div", {className: "collab-edit-row"}, [el("span", {className: "title", textContent: o.title, title: o.title}), sel]);
        if (cur && cur.role !== cur.direct) {
          row.insertBefore(el("span", {className: "collab-role", textContent: "inherits: " + cur.role}), sel);
        }
        wrap.appendChild(row);
      });
    });
    if (!Object.keys(selects).length) wrap.appendChild(el("p", {className: "empty", textContent: "You don't own anything to share yet."}));
    const err = el("p", {className: "error-message", style: "display:none"});
    const save = el("button", {className: "btn", type: "button", textContent: "Save access"});
    const cancel = el("button", {className: "btn", type: "button", textContent: "Cancel"});
    cancel.addEventListener("click", function () { onDone(false); });
    save.addEventListener("click", async function () {
      const body = {};
      Object.keys(selects).forEach(function (kind) {
        body[kind] = {};
        selects[kind].forEach(function (s) { if (s.select.value) body[kind][s.id] = s.select.value; });
      });
      save.disabled = true;
      try {
        await api(itemUrl(card.id, "/access"), "PUT", body);
        onDone(true);
      } catch (e) {
        err.textContent = e.message; err.style.display = ""; save.disabled = false;
      }
    });
    wrap.appendChild(err);
    wrap.appendChild(el("div", {className: "collab-actions"}, [save, cancel]));
    return wrap;
  }

  function renderCard(card) {
    const kindClass = card.status;
    const head = el("div", {className: "collab-card-head"}, [
      el("span", {className: "collab-email", textContent: card.email}),
      el("span", {className: "collab-status " + kindClass, textContent: card.status_label}),
      el("span", {className: "collab-when", textContent: fmtTime(card.at)}),
    ]);
    const root = el("div", {className: "collab-card" + (card.is_new ? " is-new" : "")}, [head]);

    const actions = el("div", {className: "collab-actions"});
    if (card.status === "received") {
      const accept = el("button", {className: "btn", type: "button", textContent: "Accept"});
      accept.addEventListener("click", function () { act(accept, itemUrl(card.id, "/accept"), "POST"); });
      const decline = el("button", {className: "btn", type: "button", textContent: "Decline"});
      decline.addEventListener("click", function () { act(decline, itemUrl(card.id), "DELETE"); });
      actions.append(accept, decline);
    } else if (card.status === "sent") {
      const withdraw = el("button", {className: "btn", type: "button", textContent: "Withdraw invitation"});
      withdraw.addEventListener("click", function () { act(withdraw, itemUrl(card.id), "DELETE"); });
      actions.appendChild(withdraw);
    } else {
      const body = el("div");
      const show = function () {
        body.replaceChildren(
          accessList("You share with them", card.shared_by_me),
          accessList("They share with you", card.shared_with_me)
        );
      };
      show();
      root.appendChild(body);
      const del = el("button", {className: "collab-delete", type: "button", title: "Remove collaborator", "aria-label": "Remove collaborator " + card.email});
      del.innerHTML = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4h11"/><path d="M6 4V2.5h4V4"/><path d="M3.8 4l.7 9.5h7L12.2 4"/><path d="M6.7 6.5v5M9.3 6.5v5"/></svg>';
      del.addEventListener("click", function () {
        if (confirm("Remove " + card.email + " as a collaborator?\n\nEverything you have shared with them, and everything they have shared with you, will be withdrawn.")) {
          act(del, itemUrl(card.id), "DELETE");
        }
      });
      head.appendChild(del);
      const edit = el("button", {className: "btn", type: "button", textContent: "Edit access"});
      edit.addEventListener("click", function () {
        actions.style.display = "none";
        body.replaceChildren(editor(card, function (saved) { if (saved) refresh(); else { actions.style.display = ""; show(); } }));
      });
      actions.appendChild(edit);
    }
    root.appendChild(actions);
    return root;
  }

  async function act(btn, url, method) {
    btn.disabled = true;
    try { await api(url, method); await refresh(); }
    catch (e) { showError(e.message); btn.disabled = false; }
  }

  async function refresh() {
    const data = await api(listUrl);
    shareable = data.shareable;
    // Invitations you've received go in their own section above the invite form.
    const received = data.collaborations.filter(function (c) { return c.status === "received"; });
    const others = data.collaborations.filter(function (c) { return c.status !== "received"; });
    document.getElementById("receivedList").replaceChildren.apply(document.getElementById("receivedList"), received.map(renderCard));
    document.getElementById("receivedSection").style.display = received.length ? "" : "none";
    listEl.replaceChildren.apply(listEl, others.map(renderCard));
    emptyEl.style.display = others.length ? "none" : "";
  }

  // Once the page has shown any accepted invitations, they stop counting as notifications.
  refresh().then(function () { return api(seenUrl, "POST"); }).catch(function (e) { showError(e.message); });
  loadCaptcha();
})();
