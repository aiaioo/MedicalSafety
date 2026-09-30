// Admin page: role toggles on the Users tab, website/section management on
// the Websites tab. Plain fetch() calls against /api/admin/* -- no build
// step, unlike the Tiptap-based editors.
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
  $$(".admin-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".admin-tab").forEach((t) => t.classList.remove("active"));
      $$(".admin-panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      $("#tab-" + tab.dataset.tab).classList.add("active");
    });
  });

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
  // Websites: rename/delete/add a section, add a website
  // ---------------------------------------------------------------------
  function wireSectionRow(row, list) {
    const sectionId = row.dataset.sectionId;
    const titleInput = $(".section-title", row);
    const saveBtn = $(".section-save", row);
    const deleteBtn = $(".section-delete", row);

    saveBtn.addEventListener("click", async () => {
      const title = titleInput.value.trim();
      if (!title) return;
      const position = Array.from(list.children).indexOf(row);
      try {
        await api(`/api/admin/section/${encodeURIComponent(sectionId)}`, "POST", { title, position });
      } catch (e) {
        window.alert("Could not save section: " + e.message);
      }
    });

    wireConfirmDelete(deleteBtn, async () => {
      try {
        await api(`/api/admin/section/${encodeURIComponent(sectionId)}`, "DELETE");
        row.remove();
      } catch (e) {
        window.alert("Could not delete section: " + e.message);
      }
    });
  }

  $$(".website-card[data-website-id]").forEach((card) => {
    const websiteId = card.dataset.websiteId;
    const list = $(".sections-list", card);
    $$(".section-row", list).forEach((row) => wireSectionRow(row, list));

    const newTitle = $(".new-section-title", card);
    const addBtn = $(".add-section-btn", card);
    addBtn.addEventListener("click", async () => {
      const title = newTitle.value.trim();
      if (!title) return;
      addBtn.disabled = true;
      try {
        const section = await api(`/api/admin/website/${encodeURIComponent(websiteId)}/sections`, "POST", { title });
        const row = document.createElement("div");
        row.className = "section-row";
        row.dataset.sectionId = section.id;
        row.innerHTML =
          '<input type="text" class="section-title" value="' + title.replace(/"/g, "&quot;") + '">' +
          '<button type="button" class="btn section-save">Save</button>' +
          '<button type="button" class="btn card-delete-btn section-delete" title="Delete section">&#10005;</button>';
        list.appendChild(row);
        wireSectionRow(row, list);
        newTitle.value = "";
      } catch (e) {
        window.alert("Could not add section: " + e.message);
      } finally {
        addBtn.disabled = false;
      }
    });
  });

  const websiteError = $("#websiteError");
  const addWebsiteBtn = $("#addWebsiteBtn");
  if (addWebsiteBtn) {
    addWebsiteBtn.addEventListener("click", async () => {
      show(websiteError, "");
      const domain = $("#newWebsiteDomain").value.trim().toLowerCase();
      const name = $("#newWebsiteName").value.trim();
      if (!domain || !name) {
        show(websiteError, "Enter both a domain and a name.");
        return;
      }
      addWebsiteBtn.disabled = true;
      try {
        await api("/api/admin/websites", "POST", { domain, name });
        window.location.reload();
      } catch (e) {
        show(websiteError, e.message);
        addWebsiteBtn.disabled = false;
      }
    });
  }
})();
