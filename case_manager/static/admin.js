// Admin page: role toggles on the Users tab, cause title templates. Plain
// fetch() calls against /api/admin/* -- no build step, unlike the Tiptap-based editors.
(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  async function api(url, method, body) {
    const opts = { method: method || "GET" };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  function show(el, msg) {
    el.textContent = msg || "";
    el.style.display = msg ? "" : "none";
  }

  // Same click-to-arm delete confirmation used on the reports/allegations pages.
  function wireConfirmDelete(btn, onConfirm) {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      if (!btn.classList.contains("confirming")) {
        btn.classList.add("confirming");
        btn.dataset.label = btn.textContent;
        btn.textContent = "Confirm?";
        btn._confirmTimer = setTimeout(() => {
          btn.classList.remove("confirming");
          btn.textContent = btn.dataset.label;
        }, 3000);
        return;
      }
      clearTimeout(btn._confirmTimer);
      onConfirm();
    });
  }

  // ---------------------------------------------------------------------
  // Tabs
  // ---------------------------------------------------------------------
  // The URL hash (e.g. /admin#titles) selects a tab, so other pages can link
  // straight to one; clicking a tab keeps the hash in step.
  function showTab(name) {
    const tab = $('.admin-tab[data-tab="' + name + '"]');
    if (!tab) return false;
    $$(".admin-tab").forEach((t) => t.classList.remove("active"));
    $$(".admin-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("#tab-" + name).classList.add("active");
    return true;
  }
  $$(".admin-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      showTab(tab.dataset.tab);
      history.replaceState(null, "", "#" + tab.dataset.tab);
    });
  });
  showTab(location.hash.slice(1));
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

  // ---------------------------------------------------------------------
  // Users: disk usage formatting + role toggles
  // ---------------------------------------------------------------------
  function fmtBytes(n) {
    if (n === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return n.toFixed(i === 0 ? 0 : 1) + " " + units[i];
  }

  $$("#usersTableBody tr").forEach((row) => {
    const cell = $$("td", row)[7];
    const raw = cell.dataset.diskBytes;
    cell.textContent = raw === "" ? "n/a" : fmtBytes(Number(raw));
  });

  const rolesError = $("#rolesError");
  $$("#usersTableBody tr").forEach((row) => {
    const userId = row.dataset.userId;
    const adminBox = $(".role-admin", row);
    const ccBox = $(".role-content-creator", row);
    [adminBox, ccBox].forEach((box) => {
      box.addEventListener("change", async () => {
        show(rolesError, "");
        const prev = { admin: adminBox.checked, cc: ccBox.checked };
        try {
          await api(`/api/admin/user/${encodeURIComponent(userId)}/roles`, "PUT", {
            is_admin: adminBox.checked,
            is_content_creator: ccBox.checked,
          });
        } catch (e) {
          adminBox.checked = box === adminBox ? !prev.admin : prev.admin;
          ccBox.checked = box === ccBox ? !prev.cc : prev.cc;
          show(rolesError, e.message);
        }
      });
    });
  });

  // ---------------------------------------------------------------------
  // Cause title templates
  // ---------------------------------------------------------------------
  const templateListEl = $("#titleTemplateList");
  if (templateListEl) {
    const editors = new Map(); // template card -> rich-text editor
    $$(".template-body", templateListEl).forEach((el) => {
      const card = el.closest("[data-template-id]");
      editors.set(card, window.createTitleTemplateEditor(el, el.dataset.body, () => saveTemplate(card)));
    });
    const newEditor = window.createTitleTemplateEditor($("#newTemplateBody"), "", () => $("#addTemplateBtn").click());
    async function saveTemplate(card) {
      const status = $(".role-save-status", card);
      try {
        await api("/api/cause-title-template/" + encodeURIComponent(card.dataset.templateId), "PUT",
          { name: $(".template-name", card).value, body: editors.get(card).getBody() });
        status.textContent = "Saved";
      } catch (err) {
        status.textContent = err.message;
      }
    }
    templateListEl.addEventListener("click", (e) => {
      const card = e.target.closest("[data-template-id]");
      if (card && e.target.classList.contains("template-save")) saveTemplate(card);
    });
    $$(".template-delete", templateListEl).forEach((btn) => {
      wireConfirmDelete(btn, async () => {
        const card = btn.closest("[data-template-id]");
        try {
          await api("/api/cause-title-template/" + encodeURIComponent(card.dataset.templateId), "DELETE");
          card.remove();
        } catch (err) {
          $(".role-save-status", card).textContent = err.message;
        }
      });
    });
    $("#addTemplateBtn").addEventListener("click", async () => {
      const errEl = $("#templateError");
      try {
        await api("/api/cause-title-templates", "POST",
          { name: $("#newTemplateName").value, body: newEditor.getBody() });
        show(errEl, "");
        location.reload();
      } catch (err) {
        show(errEl, err.message);
      }
    });
  }
})();
