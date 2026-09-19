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
