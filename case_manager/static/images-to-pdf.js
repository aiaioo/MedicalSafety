// "Create from images": pick images, reorder them by dragging, save them as one PDF uploaded through the
// normal document upload. Images are sensitive, so they only ever live in this tab's memory: File objects are
// read in place (never copied to disk or to a blob: URL), thumbnails are canvases, and the PDF is assembled in
// memory. Closing the dialog drops every reference.
(function () {
  const openBtn = document.getElementById("imagesOpen");
  const modal = document.getElementById("imagesModal");
  if (!openBtn || !modal) return;
  const grid = document.getElementById("imagesGrid");
  const emptyHint = document.getElementById("imagesEmpty");
  const input = document.getElementById("imagesInput");
  const addBtn = document.getElementById("imagesAdd");
  const nameInput = document.getElementById("imagesName");
  const saveBtn = document.getElementById("imagesSave");
  const closeBtn = document.getElementById("imagesClose");
  const status = document.getElementById("imagesStatus");

  const THUMB = 100;
  const MAX_PX = 3000;      // longest side after re-encoding
  const MAX_PAGE_PT = 842;  // longest page side, in PDF points
  let items = [];           // [{file, thumb: canvas}] in page order
  let busy = false;
  let dragItem = null;

  function setStatus(msg, isError) {
    status.textContent = msg || "";
    status.classList.toggle("error", !!isError);
  }

  function render() {
    grid.replaceChildren();
    items.forEach((it, i) => {
      const cell = document.createElement("div");
      cell.className = "image-thumb";
      cell.draggable = !busy;
      cell.title = it.file.name;
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "image-thumb-remove";
      rm.textContent = "✕";
      rm.title = "Remove";
      rm.disabled = busy;
      rm.addEventListener("click", () => { items.splice(items.indexOf(it), 1); render(); });
      const num = document.createElement("span");
      num.className = "image-thumb-num";
      num.textContent = i + 1;
      cell.append(it.thumb, rm, num);
      cell.addEventListener("dragstart", (e) => {
        dragItem = it;
        cell.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", "");  // required by Firefox
      });
      cell.addEventListener("dragend", () => { dragItem = null; cell.classList.remove("dragging"); });
      cell.addEventListener("dragover", (e) => {
        if (!dragItem || dragItem === it) return;
        e.preventDefault();
        const r = cell.getBoundingClientRect();
        const after = e.clientX > r.left + r.width / 2;
        const from = items.indexOf(dragItem);
        let to = items.indexOf(it) + (after ? 1 : 0);
        if (from < to) to--;
        if (from === to) return;
        items.splice(from, 1);
        items.splice(to, 0, dragItem);
        render();
        // render() rebuilt the cells; restore the dragged look on the new one.
        const idx = items.indexOf(dragItem);
        grid.children[idx].classList.add("dragging");
      });
      cell.addEventListener("drop", (e) => e.preventDefault());
      grid.appendChild(cell);
    });
    emptyHint.hidden = items.length > 0;
    saveBtn.disabled = busy || items.length === 0;
    addBtn.disabled = busy;
  }

  async function decode(file) {
    return createImageBitmap(file, { imageOrientation: "from-image" });
  }

  async function addFiles(files) {
    const failed = [];
    for (const file of files) {
      try {
        const bmp = await decode(file);
        const s = Math.min(THUMB / bmp.width, THUMB / bmp.height, 1);
        const c = document.createElement("canvas");
        c.width = Math.max(1, Math.round(bmp.width * s));
        c.height = Math.max(1, Math.round(bmp.height * s));
        c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
        bmp.close();
        items.push({ file, thumb: c });
      } catch (e) {
        failed.push(file.name);
      }
    }
    setStatus(failed.length ? `Could not read: ${failed.join(", ")}` : "", failed.length > 0);
    render();
  }

  // ---- PDF assembly (JPEG pages, DCTDecode) ----
  async function toJpeg(file) {
    const bmp = await decode(file);
    const s = Math.min(1, MAX_PX / Math.max(bmp.width, bmp.height));
    const w = Math.max(1, Math.round(bmp.width * s));
    const h = Math.max(1, Math.round(bmp.height * s));
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, w, h);
    ctx.drawImage(bmp, 0, 0, w, h);
    bmp.close();
    const blob = await new Promise((res) => c.toBlob(res, "image/jpeg", 0.92));
    c.width = c.height = 0;
    if (!blob) throw new Error(`Could not encode ${file.name}`);
    return { w, h, bytes: new Uint8Array(await blob.arrayBuffer()) };
  }

  async function buildPdf(files) {
    const enc = new TextEncoder();
    const parts = [];
    const offsets = [];
    let len = 0;
    const push = (b) => { if (typeof b === "string") b = enc.encode(b); parts.push(b); len += b.length; };
    const obj = (n, head, stream) => {
      offsets[n] = len;
      push(`${n} 0 obj\n${head}\n`);
      if (stream) { push("stream\n"); push(stream); push("\nendstream\n"); }
      push("endobj\n");
    };
    const n = files.length;
    push("%PDF-1.4\n%\xE2\xE3\xCF\xD3\n");
    obj(1, "<< /Type /Catalog /Pages 2 0 R >>");
    obj(2, `<< /Type /Pages /Count ${n} /Kids [${files.map((_, i) => `${3 + i * 3} 0 R`).join(" ")}] >>`);
    for (let i = 0; i < n; i++) {
      const img = await toJpeg(files[i]);
      const k = Math.min(1, MAX_PAGE_PT / Math.max(img.w, img.h));
      const pw = +(img.w * k).toFixed(2), ph = +(img.h * k).toFixed(2);
      const page = 3 + i * 3;
      obj(page, `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pw} ${ph}] /Resources << /XObject << /Im0 ${page + 2} 0 R >> >> /Contents ${page + 1} 0 R >>`);
      const content = enc.encode(`q ${pw} 0 0 ${ph} 0 0 cm /Im0 Do Q`);
      obj(page + 1, `<< /Length ${content.length} >>`, content);
      obj(page + 2, `<< /Type /XObject /Subtype /Image /Width ${img.w} /Height ${img.h} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${img.bytes.length} >>`, img.bytes);
      img.bytes = null;
    }
    const total = 3 + n * 3;
    const xref = len;
    let x = `xref\n0 ${total}\n0000000000 65535 f \n`;
    for (let i = 1; i < total; i++) x += `${String(offsets[i]).padStart(10, "0")} 00000 n \n`;
    push(x + `trailer\n<< /Size ${total} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);
    return new Blob(parts, { type: "application/pdf" });
  }

  async function save() {
    const name = nameInput.value.trim();
    if (!name) { setStatus("Enter a document name.", true); nameInput.focus(); return; }
    busy = true;
    render();
    nameInput.disabled = true;
    try {
      setStatus("Building PDF…");
      const pdf = await buildPdf(items.map((it) => it.file));
      setStatus("Uploading…");
      const body = new FormData();
      body.append("file", new File([pdf], name.replace(/[\\/]/g, "-") + ".pdf", { type: "application/pdf" }));
      const res = await fetch("/api/documents/upload", { method: "POST", body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Upload failed");
      setStatus(`Saved as "${data.id}". Reloading…`);
      items = [];
      setTimeout(() => location.reload(), 600);
    } catch (err) {
      setStatus(err.message, true);
      busy = false;
      nameInput.disabled = false;
      render();
    }
  }

  function open() {
    modal.classList.add("open");
    render();
  }

  function close() {
    if (busy) return;
    modal.classList.remove("open");
    items = [];            // drop every File and thumbnail reference
    dragItem = null;
    grid.replaceChildren();
    input.value = "";
    nameInput.value = "";
    setStatus("");
  }

  openBtn.addEventListener("click", open);
  addBtn.addEventListener("click", () => input.click());
  input.addEventListener("change", async () => {
    const files = Array.from(input.files);
    input.value = "";  // clear the picker's own file list immediately
    await addFiles(files);
  });
  saveBtn.addEventListener("click", save);
  closeBtn.addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal.classList.contains("open")) close();
  });
})();
