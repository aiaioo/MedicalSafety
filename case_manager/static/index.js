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

  const uploadForm = document.getElementById("uploadForm");
  const uploadInput = document.getElementById("uploadInput");
  const uploadSubmit = document.getElementById("uploadSubmit");
  const uploadStatus = document.getElementById("uploadStatus");

  if (uploadForm) {
    uploadForm.addEventListener("submit", async (e) => {
      e.preventDefault();
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
      }
    });
  }
})();
