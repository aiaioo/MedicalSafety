(function () {
  const picker = document.getElementById("causePicker");
  if (!picker) return;
  let current = picker.value;

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await errorText(res));
    return res.json();
  }

  async function errorText(res) {
    try { return (await res.json()).error || res.statusText; } catch (e) { return res.statusText; }
  }

  const makeDefault = (id) => post(picker.dataset.url, { cause_id: id });

  // ---- Cases page only: create / rename / delete through a popup ----
  const modal = document.getElementById("causeModal");
  if (modal) {
    const causesUrl = picker.dataset.causesUrl;
    const causeUrl = (id) => picker.dataset.causeUrlBase.replace("__ID__", encodeURIComponent(id));
    const form = document.getElementById("causeForm");
    const heading = document.getElementById("causeModalTitle");
    const input = document.getElementById("causeTitleInput");
    const errorEl = document.getElementById("causeModalError");
    const saveBtn = document.getElementById("causeSave");
    let editingId = null; // null = creating

    function openModal(id) {
      editingId = id;
      heading.textContent = id ? "Rename cause" : "New cause";
      saveBtn.textContent = id ? "Save" : "Create";
      input.value = id ? picker.selectedOptions[0].textContent : "";
      errorEl.hidden = true;
      modal.classList.add("open");
      input.focus();
      input.select();
    }
    function closeModal() {
      modal.classList.remove("open");
      picker.value = current; // undo a "New cause…" selection
    }

    document.getElementById("causeCancel").addEventListener("click", closeModal);
    modal.addEventListener("mousedown", (e) => { if (e.target === modal) closeModal(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modal.classList.contains("open")) closeModal(); });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const title = input.value.trim();
      if (!title) return;
      saveBtn.disabled = true;
      try {
        if (editingId) {
          await post(causeUrl(editingId), { title });
        } else {
          const created = await post(causesUrl, { title });
          await makeDefault(created.id);
        }
        location.reload();
      } catch (err) {
        errorEl.textContent = err.message;
        errorEl.hidden = false;
        saveBtn.disabled = false;
      }
    });

    document.getElementById("renameCauseBtn").addEventListener("click", () => { if (current) openModal(current); });
    document.getElementById("deleteCauseBtn").addEventListener("click", async () => {
      if (!current) return;
      if (!confirm(`Delete the cause "${picker.selectedOptions[0].textContent}"?`)) return;
      try {
        const res = await fetch(causeUrl(current), { method: "DELETE" });
        if (!res.ok) throw new Error(await errorText(res));
        location.reload();
      } catch (err) {
        alert("Could not delete cause: " + err.message);
      }
    });

    picker.addEventListener("change", (e) => {
      if (picker.value !== "__new__") return;
      e.stopImmediatePropagation();
      openModal(null);
    }, true);
  }

  picker.addEventListener("change", async () => {
    try {
      await makeDefault(picker.value);
      current = picker.value;
      if (picker.dataset.reload === "1") location.reload();
    } catch (e) {
      console.error(e);
      picker.value = current;
    }
  });
})();
