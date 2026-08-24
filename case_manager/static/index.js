(function () {
  const container = document.getElementById("indexReportGrid");
  const emptyHint = document.getElementById("indexReportsEmpty");
  if (!container) return;
  const reportsUrl = container.dataset.reportsUrl;

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function fmtDate(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  function renderReportCard(r) {
    const card = document.createElement("a");
    card.className = "report-card";
    card.href = `/document?report=${encodeURIComponent(r.id)}`;
    const sourceLabel = r.source_doc ? `${r.source_doc} (${r.source_type})` : "No source document";
    card.innerHTML = `
      <div class="report-card-name">${escapeHtml(r.name || r.id)}</div>
      <div class="report-card-meta">${escapeHtml(sourceLabel)}</div>
      <div class="report-card-meta">Updated ${escapeHtml(fmtDate(r.updated_at))}</div>`;
    container.appendChild(card);
  }

  fetch(reportsUrl)
    .then((res) => res.json())
    .then((list) => {
      container.innerHTML = "";
      if (emptyHint) emptyHint.style.display = list.length ? "none" : "block";
      for (const r of list) renderReportCard(r);
    })
    .catch(() => {
      if (emptyHint) {
        emptyHint.textContent = "Failed to load documents.";
        emptyHint.style.display = "block";
      }
    });

  const newDocBtn = document.getElementById("newDocBtn");
  const newDocModal = document.getElementById("newDocModal");
  const newDocName = document.getElementById("newDocName");
  const newDocSource = document.getElementById("newDocSource");
  const newDocError = document.getElementById("newDocError");
  const newDocCancel = document.getElementById("newDocCancel");
  const newDocCreate = document.getElementById("newDocCreate");

  function openModal(el) {
    el.classList.add("open");
  }
  function closeModal(el) {
    el.classList.remove("open");
  }

  if (newDocBtn && newDocModal) {
    newDocBtn.addEventListener("click", () => {
      newDocName.value = "";
      newDocSource.value = "";
      newDocError.style.display = "none";
      openModal(newDocModal);
      setTimeout(() => newDocName.focus(), 0);
    });
    newDocCancel.addEventListener("click", () => closeModal(newDocModal));
    newDocModal.addEventListener("click", (e) => {
      if (e.target === newDocModal) closeModal(newDocModal);
    });

    async function createDocument() {
      const name = newDocName.value.trim();
      if (!name) {
        newDocError.textContent = "Please enter a name for the document.";
        newDocError.style.display = "block";
        return;
      }
      let source_doc = "";
      let source_type = "pdf";
      if (newDocSource.value) {
        [source_doc, source_type] = newDocSource.value.split("|");
      }
      newDocCreate.disabled = true;
      try {
        const res = await fetch(reportsUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, source_doc, source_type }),
        });
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        window.location.href = `/document?report=${encodeURIComponent(data.id)}`;
      } catch (err) {
        newDocError.textContent = "Could not create document: " + err.message;
        newDocError.style.display = "block";
        newDocCreate.disabled = false;
      }
    }
    newDocCreate.addEventListener("click", createDocument);
    newDocName.addEventListener("keydown", (e) => {
      if (e.key === "Enter") createDocument();
    });
  }

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
})();
