(function () {
  const uploadInput = document.getElementById("uploadInput");
  const uploadSubmit = document.getElementById("uploadSubmit");
  const uploadStatus = document.getElementById("uploadStatus");
  let addToSection = null; // set up with the sections, below

  // Uploads one file, giving the new document's id.
  async function uploadFile(file) {
    const body = new FormData();
    body.append("file", file);
    const res = await fetch("/api/documents/upload", { method: "POST", body });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || (res.status === 413 ? "The file is too large" : "Upload failed"));
    return data.id;
  }

  if (uploadSubmit && uploadInput) {
    uploadSubmit.addEventListener("click", () => uploadInput.click());

    uploadInput.addEventListener("change", async () => {
      const file = uploadInput.files[0];
      if (!file) return;

      uploadSubmit.disabled = true;
      uploadStatus.textContent = "Uploading…";
      uploadStatus.classList.remove("error");

      try {
        const id = await uploadFile(file);
        uploadStatus.textContent = `Uploaded as "${id}". Reloading…`;
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
  // implicit and holds every document not assigned elsewhere. The layout belongs to the workspace, and is stored on
  // its owner's account (users.document_sections): [{id, name, documents: [...]}]. The cards are rendered by the
  // server into #docCardPool and moved into their sections here.
  const sectionsEl = document.getElementById("docSections");
  const pool = document.getElementById("docCardPool");
  const addSectionBtn = document.getElementById("addSectionBtn");
  if (sectionsEl && pool) {
    const sectionsUrl = sectionsEl.dataset.sectionsUrl;
    const cards = new Map(Array.from(pool.querySelectorAll(".report-card")).map((c) => [c.dataset.docId, c]));
    let sections = [];
    let dragDocId = null;
    let dragSectionId = null;
    let orderBeforeDrag = "";
    let saveTimer = null;
    let canArrange = false; // set once the layout has loaded, so that a change can't overwrite it unseen
    const sectionOrder = () => sections.map((s) => s.id).join();

    async function putSections() {
      const res = await fetch(sectionsUrl, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sections),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
    }

    function saveSections() {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        saveTimer = null;
        putSections().catch((e) => window.alert("Could not save sections: " + e.message));
      }, 200);
    }

    // Puts a newly uploaded document in a section. The layout is read afresh first, as others working in the
    // workspace may have changed it during a long upload.
    addToSection = async (docId, sectionId) => {
      if (saveTimer) { // a change made here that is still waiting to be saved goes first
        clearTimeout(saveTimer);
        saveTimer = null;
        await putSections();
      }
      const res = await fetch(sectionsUrl);
      if (!res.ok) throw new Error(res.statusText);
      const latest = await res.json();
      const section = latest.find((s) => s.id === sectionId);
      if (!section) throw new Error("the section no longer exists");
      section.documents.push(docId);
      sections = latest;
      await putSections();
    };

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
      head.innerHTML = sectionId && canArrange
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

      if (sectionId && canArrange) {
        const sec = sections.find((s) => s.id === sectionId);
        const handle = head.querySelector(".section-handle");
        handle.addEventListener("mousedown", () => { wrap.draggable = true; });
        handle.addEventListener("mouseup", () => { wrap.draggable = false; });
        // Saved when the drag ends, wherever that is: by then the section being dragged is usually under the pointer
        // itself, so no "drop" can be counted on.
        wrap.addEventListener("dragend", () => {
          wrap.draggable = false;
          wrap.classList.remove("dragging");
          if (dragSectionId !== sectionId) return; // a card's drag ending
          dragSectionId = null;
          if (sectionOrder() !== orderBeforeDrag) saveSections();
        });
        wrap.addEventListener("dragstart", (e) => {
          if (!wrap.draggable || dragDocId) return;
          dragSectionId = sectionId;
          orderBeforeDrag = sectionOrder();
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
          card.draggable = canArrange;
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

    // A section may be let go anywhere among the sections (without this the browser animates it "snapping back").
    sectionsEl.addEventListener("dragover", (e) => { if (dragSectionId) e.preventDefault(); });
    sectionsEl.addEventListener("drop", (e) => { if (dragSectionId) e.preventDefault(); });

    if (addSectionBtn) {
      addSectionBtn.disabled = true;
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
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(res.statusText))))
      .then((server) => {
        sections = server;
        canArrange = Boolean(sectionsEl.dataset.canArrange);
        if (addSectionBtn) addSectionBtn.disabled = false;
        render();
      })
      .catch((e) => console.error("Could not load sections", e));
  }

  // ---- Files dragged in from the computer ----
  // Let go on a named section, they are uploaded into it; anywhere else on the page, into General.
  if (uploadSubmit) {
    const landing = document.querySelector(".doc-landing");
    let dropZone = null; // what is highlighted as the place the files would go
    const hasFiles = (e) => Array.from(e.dataTransfer.types).includes("Files");
    // Not while an upload is under way or a dialog is open, nor for someone who may not add documents here.
    const canDrop = () => !uploadSubmit.disabled && !uploadSubmit.hidden && !document.querySelector(".modal-overlay.open");
    const zoneAt = (target) => (target.closest && target.closest(".report-section[data-section-id]"))
      || document.querySelector(".report-section:not([data-section-id])") || landing;

    function highlight(zone) {
      if (dropZone && dropZone !== zone) dropZone.classList.remove("drop-target");
      if (zone) zone.classList.add("drop-target");
      dropZone = zone;
    }

    async function uploadDropped(files, sectionId) {
      const pdfs = files.filter((f) => /\.pdf$/i.test(f.name));
      const problems = files.filter((f) => !pdfs.includes(f)).map((f) => `${f.name}: skipped, as it is not a PDF`);
      let uploaded = 0;
      uploadSubmit.disabled = true;
      uploadStatus.classList.remove("error");
      uploadStatus.classList.add("floating"); // in view however far down the page the files were let go
      for (const [i, file] of pdfs.entries()) {
        const count = pdfs.length > 1 ? ` ${i + 1} of ${pdfs.length}` : "";
        uploadStatus.textContent = `Uploading${count}: ${file.name}…`;
        let docId;
        try {
          docId = await uploadFile(file);
        } catch (err) {
          problems.push(`${file.name}: ${err.message}`);
          continue;
        }
        uploaded++;
        if (!sectionId) continue;
        try {
          await addToSection(docId, sectionId);
        } catch (err) {
          problems.push(`${file.name}: uploaded, but into General (${err.message})`);
        }
      }
      if (problems.length) window.alert(problems.join("\n"));
      if (uploaded) {
        uploadStatus.textContent = `Uploaded ${uploaded} ${uploaded === 1 ? "document" : "documents"}. Reloading…`;
        setTimeout(() => location.reload(), 600);
      } else {
        uploadStatus.textContent = "";
        uploadStatus.classList.remove("floating");
        uploadSubmit.disabled = false;
      }
    }

    document.addEventListener("dragover", (e) => {
      if (!hasFiles(e)) return; // a card or section being moved
      e.preventDefault(); // otherwise letting go opens the file in place of this page
      const allowed = canDrop();
      e.dataTransfer.dropEffect = allowed ? "copy" : "none";
      highlight(allowed ? zoneAt(e.target) : null);
    });
    document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) highlight(null); }); // left the window
    document.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      highlight(null);
      if (canDrop()) uploadDropped(Array.from(e.dataTransfer.files), zoneAt(e.target).dataset.sectionId || null);
    });
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

// Edit a document's case associations from its card.
document.querySelectorAll(".card-cases-btn").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const split = (v) => (v ? v.split(",") : []);
    window.openCaseLinks({
      kind: "doc",
      id: btn.dataset.docId,
      causeIds: split(btn.dataset.causeIds),
      caseIds: split(btn.dataset.caseIds),
      onChange: (ids) => { btn.dataset.caseIds = ids.join(","); },
    });
  });
});
