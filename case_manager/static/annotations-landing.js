(function () {
  const uploadInput = document.getElementById("uploadInput");
  const uploadSubmit = document.getElementById("uploadSubmit");
  const uploadStatus = document.getElementById("uploadStatus");

  if (uploadSubmit && uploadInput) {
    uploadSubmit.addEventListener("click", () => uploadInput.click());

    uploadInput.addEventListener("change", async () => {
      const file = uploadInput.files[0];
      if (!file) return;

      uploadSubmit.disabled = true;
      uploadStatus.textContent = "Uploading…";
      uploadStatus.classList.remove("error");

      try {
        const body = new FormData();
        body.append("file", file);
        const res = await fetch("/api/documents/upload", { method: "POST", body });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Upload failed");
        uploadStatus.textContent = `Uploaded as "${data.id}". Reloading…`;
        setTimeout(() => location.reload(), 600);
      } catch (err) {
        uploadStatus.textContent = err.message;
        uploadStatus.classList.add("error");
        uploadSubmit.disabled = false;
        uploadInput.value = "";
      }
    });
  }

  // Same click-to-arm confirmation as the reports/allegations cards: first
  // click shows a red "Confirm?" for 3s, second click within that window
  // actually deletes.
  function wireConfirmDelete(btn, onConfirm) {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
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


  // ---- Landing page sections ----
  // Same idea as the reports page: users organise document cards into named, draggable sections. "General" is
  // implicit and holds every document not assigned elsewhere. The layout is stored on the user's account
  // (users.document_sections): [{id, name, documents: [...]}]. The cards are rendered by the server into
  // #docCardPool and moved into their sections here.
  const sectionsEl = document.getElementById("docSections");
  const pool = document.getElementById("docCardPool");
  const addSectionBtn = document.getElementById("addSectionBtn");
  if (sectionsEl && pool) {
    const sectionsUrl = sectionsEl.dataset.sectionsUrl;
    const cards = new Map(Array.from(pool.querySelectorAll(".report-card")).map((c) => [c.dataset.docId, c]));
    let sections = [];
    let dragDocId = null;
    let dragSectionId = null;
    let saveTimer = null;

    function saveSections() {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        fetch(sectionsUrl, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(sections),
        }).catch((e) => console.error("Could not save sections", e));
      }, 200);
    }

    function moveDocToSection(docId, sectionId) {
      for (const sec of sections) sec.documents = sec.documents.filter((id) => id !== docId);
      if (sectionId) sections.find((s) => s.id === sectionId).documents.push(docId);
      saveSections();
      render();
    }

    function buildSection(title, sectionId) {
      const wrap = document.createElement("section");
      wrap.className = "report-section";
      if (sectionId) wrap.dataset.sectionId = sectionId;
      const head = document.createElement("div");
      head.className = "report-section-head";
      head.innerHTML = sectionId
        ? '<span class="section-handle" title="Drag to reorder">&#8942;&#8942;</span><h3 class="section-title"></h3><button type="button" class="section-rename" title="Rename section">Rename</button><button type="button" class="section-delete card-delete-btn" title="Delete section (documents move to General)">✕</button>'
        : '<h3 class="section-title"></h3>';
      head.querySelector(".section-title").textContent = title;
      const grid = document.createElement("div");
      grid.className = "report-grid report-section-grid";
      wrap.append(head, grid);

      // Drop zone for document cards.
      wrap.addEventListener("dragover", (e) => {
        if (!dragDocId) return;
        e.preventDefault();
        wrap.classList.add("drop-target");
      });
      wrap.addEventListener("dragleave", (e) => {
        if (!wrap.contains(e.relatedTarget)) wrap.classList.remove("drop-target");
      });
      wrap.addEventListener("drop", (e) => {
        if (!dragDocId) return;
        e.preventDefault();
        wrap.classList.remove("drop-target");
        moveDocToSection(dragDocId, sectionId || null);
      });

      if (sectionId) {
        const sec = sections.find((s) => s.id === sectionId);
        const handle = head.querySelector(".section-handle");
        handle.addEventListener("mousedown", () => { wrap.draggable = true; });
        wrap.addEventListener("dragend", () => { wrap.draggable = false; wrap.classList.remove("dragging"); dragSectionId = null; });
        wrap.addEventListener("dragstart", (e) => {
          if (!wrap.draggable || dragDocId) return;
          dragSectionId = sectionId;
          wrap.classList.add("dragging");
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", sectionId);
        });
        wrap.addEventListener("dragover", (e) => {
          if (!dragSectionId || dragSectionId === sectionId) return;
          e.preventDefault();
          const rect = wrap.getBoundingClientRect();
          const before = e.clientY < rect.top + rect.height / 2;
          const from = sections.findIndex((s) => s.id === dragSectionId);
          let to = sections.findIndex((s) => s.id === sectionId) + (before ? 0 : 1);
          if (from < to) to--;
          if (from === to) return;
          const [moved] = sections.splice(from, 1);
          sections.splice(to, 0, moved);
          const dragged = sectionsEl.querySelector(".report-section.dragging");
          sectionsEl.insertBefore(dragged, before ? wrap : wrap.nextSibling);
        });
        wrap.addEventListener("drop", (e) => { if (dragSectionId) { e.preventDefault(); saveSections(); } });
        head.querySelector(".section-rename").addEventListener("click", () => {
          const name = window.prompt("Section name", sec.name);
          if (name && name.trim()) { sec.name = name.trim().slice(0, 80); saveSections(); render(); }
        });
        wireConfirmDelete(head.querySelector(".section-delete"), () => {
          sections = sections.filter((s) => s.id !== sectionId);
          saveSections();
          render();
        });
      }
      sectionsEl.appendChild(wrap);
      return grid;
    }

    function render() {
      // Park the cards back in the pool so clearing the sections doesn't discard them.
      cards.forEach((card) => pool.appendChild(card));
      sectionsEl.innerHTML = "";
      const placed = new Set();
      const cardsFor = (ids) => ids.filter((id) => cards.has(id) && !placed.has(id) && placed.add(id));
      const sectionIds = sections.map((sec) => cardsFor(sec.documents));
      const general = Array.from(cards.keys()).filter((id) => !placed.has(id));
      const fill = (grid, ids) => {
        for (const id of ids) {
          const card = cards.get(id);
          grid.appendChild(card);
          card.draggable = true;
        }
      };
      // General is only titled when sections exist, but is always a valid drop target for moving cards back.
      fill(buildSection("General", null), general);
      if (!sections.length) sectionsEl.firstChild.querySelector(".report-section-head").style.display = "none";
      sections.forEach((sec, i) => fill(buildSection(sec.name, sec.id), sectionIds[i]));
    }

    cards.forEach((card, id) => {
      card.addEventListener("dragstart", (e) => {
        dragDocId = id;
        card.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", id);
        e.stopPropagation();
      });
      card.addEventListener("dragend", () => { dragDocId = null; card.classList.remove("dragging"); });
    });

    if (addSectionBtn) {
      addSectionBtn.addEventListener("click", () => {
        const name = window.prompt("New section name");
        if (!name || !name.trim()) return;
        sections.push({ id: "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), name: name.trim().slice(0, 80), documents: [] });
        saveSections();
        render();
      });
    }

    render();
    fetch(sectionsUrl)
      .then((res) => (res.ok ? res.json() : []))
      .then((server) => { sections = server; render(); })
      .catch(() => {});
  }

  document.querySelectorAll(".doc-card-delete").forEach((btn) => {
    wireConfirmDelete(btn, async () => {
      const docId = btn.dataset.docId;
      try {
        const res = await fetch(`/api/document/${encodeURIComponent(docId)}`, { method: "DELETE" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || "Delete failed");
        location.reload();
      } catch (err) {
        window.alert("Could not delete document: " + err.message);
        clearTimeout(btn._confirmTimer);
        btn.classList.remove("confirming");
        btn.textContent = "✕";
      }
    });
  });
})();
