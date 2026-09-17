(function () {
  const uploadInput = document.getElementById("uploadInput");
  const uploadSubmit = document.getElementById("uploadSubmit");
  const uploadStatus = document.getElementById("uploadStatus");
  if (!uploadSubmit || !uploadInput) return;

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
})();
