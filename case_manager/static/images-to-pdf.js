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
  const MAX_PAGE_PT = 842;  // longest page side, in PDF points
  let items = [];           // [{file, base: canvas, thumb: canvas, rot: quarter turns clockwise}] in page order
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
      const rotL = rotateButton("↺", "Rotate left", () => { it.rot = (it.rot + 3) % 4; refreshThumb(it); render(); });
      const rotR = rotateButton("↻", "Rotate right", () => { it.rot = (it.rot + 1) % 4; refreshThumb(it); render(); });
      const mvL = moveButton("◀", "Move earlier", i === 0, () => move(it, -1));
      const mvR = moveButton("▶", "Move later", i === items.length - 1, () => move(it, 1));
      cell.append(it.thumb, rm, num, rotL, rotR, mvL, mvR);
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

  // Tap-friendly alternative to drag-and-drop (HTML5 drag doesn't work on touch screens).
  function moveButton(label, title, atEnd, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "image-thumb-move " + (label === "◀" ? "left" : "right");
    b.textContent = label;
    b.title = title;
    b.disabled = busy || atEnd;
    b.addEventListener("click", onClick);
    return b;
  }

  function move(it, delta) {
    const from = items.indexOf(it);
    items.splice(from, 1);
    items.splice(from + delta, 0, it);
    render();
  }

  function rotateButton(label, title, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "image-thumb-rotate " + (label === "↺" ? "left" : "right");
    b.textContent = label;
    b.title = title;
    b.disabled = busy;
    b.addEventListener("click", onClick);
    return b;
  }

  // Redraw the displayed thumbnail from the unrotated base canvas.
  function refreshThumb(it) {
    const swap = it.rot % 2 === 1;
    const c = document.createElement("canvas");
    c.width = swap ? it.base.height : it.base.width;
    c.height = swap ? it.base.width : it.base.height;
    const ctx = c.getContext("2d");
    ctx.translate(c.width / 2, c.height / 2);
    ctx.rotate(it.rot * Math.PI / 2);
    ctx.drawImage(it.base, -it.base.width / 2, -it.base.height / 2);
    it.thumb = c;
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
        items.push({ file, base: c, thumb: c, rot: 0 });
      } catch (e) {
        failed.push(file.name);
      }
    }
    setStatus(failed.length ? `Could not read: ${failed.join(", ")}` : "", failed.length > 0);
    render();
  }

  // ---- PDF assembly ----
  const dv = (u8) => new DataView(u8.buffer, u8.byteOffset, u8.byteLength);

  // EXIF orientation (1-8) from an APP1 segment payload, or 1.
  function exifOrientation(u8, start, end) {
    try {
      if (end - start < 14 || String.fromCharCode(...u8.subarray(start, start + 4)) !== "Exif") return 1;
      const t = start + 6, v = dv(u8);
      const le = u8[t] === 0x49;
      let ifd = t + v.getUint32(t + 4, le);
      const n = v.getUint16(ifd, le);
      for (let k = 0; k < n; k++) {
        const e = ifd + 2 + k * 12;
        if (v.getUint16(e, le) === 0x0112) return v.getUint16(e + 8, le) || 1;
      }
    } catch (e) { /* malformed EXIF: treat as upright */ }
    return 1;
  }

  // Walk a JPEG's header segments. Returns null when it can't be embedded as-is (not a JPEG, arithmetic /
  // lossless / 12-bit coding), else {w, h, comps, adobe, orient, parts, length}: parts is the original bytes.
  function parseJpeg(u8) {
    if (u8.length < 4 || u8[0] !== 0xFF || u8[1] !== 0xD8) return null;
    let i = 2, w = 0, h = 0, comps = 0, adobe = false, orient = 1;
    while (i + 4 <= u8.length) {
      if (u8[i] !== 0xFF) return null;
      const m = u8[i + 1];
      if (m === 0xFF) { i++; continue; }  // fill byte
      const len = (u8[i + 2] << 8) | u8[i + 3];
      const end = i + 2 + len;
      if (len < 2 || end > u8.length) return null;
      if (m === 0xDA) {                    // start of scan: the rest is entropy-coded data
        if (!w) return null;
        return { w, h, comps, adobe, orient, parts: [u8], length: u8.length };
      }
      if (m === 0xE1 && orient === 1) orient = exifOrientation(u8, i + 4, end);
      const isSof = m >= 0xC0 && m <= 0xCF && m !== 0xC4 && m !== 0xC8 && m !== 0xCC;
      if (isSof) {
        if (m > 0xC2 || u8[i + 4] !== 8) return null;  // only 8-bit baseline/extended/progressive Huffman
        h = (u8[i + 5] << 8) | u8[i + 6];
        w = (u8[i + 7] << 8) | u8[i + 8];
        comps = u8[i + 9];
      }
      if (m === 0xEE) adobe = true;
      i = end;
    }
    return null;
  }

  // Quarter turns clockwise that display an EXIF orientation upright, or null if it needs mirroring.
  const EXIF_TURNS = { 1: 0, 3: 2, 6: 1, 8: 3 };

  async function deflate(bytes) {
    const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("deflate"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }

  // Lossless fallback: decoded pixels (EXIF already applied by the browser), Flate-compressed, alpha as an SMask.
  async function rawImage(file) {
    const bmp = await decode(file);
    const w = bmp.width, h = bmp.height;
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const ctx = c.getContext("2d");
    ctx.drawImage(bmp, 0, 0);
    bmp.close();
    const rgba = ctx.getImageData(0, 0, w, h).data;
    c.width = c.height = 0;
    const px = w * h, rgb = new Uint8Array(px * 3);
    let alpha = null;
    for (let k = 0, o = 0; k < px; k++, o += 3) {
      rgb[o] = rgba[k * 4]; rgb[o + 1] = rgba[k * 4 + 1]; rgb[o + 2] = rgba[k * 4 + 2];
      if (rgba[k * 4 + 3] !== 255) {
        if (!alpha) { alpha = new Uint8Array(px).fill(255); }
        alpha[k] = rgba[k * 4 + 3];
      }
    }
    return { w, h, turns: 0, rgb: await deflate(rgb), alpha: alpha && await deflate(alpha) };
  }

  async function prepare(file) {
    const jpeg = parseJpeg(new Uint8Array(await file.arrayBuffer()));
    if (jpeg && EXIF_TURNS[jpeg.orient] !== undefined && [1, 3, 4].includes(jpeg.comps)) {
      return { ...jpeg, turns: EXIF_TURNS[jpeg.orient], jpeg: true };
    }
    return rawImage(file);
  }

  async function buildPdf(list) {
    const enc = new TextEncoder();
    const parts = [];
    const offsets = [];
    let len = 0;
    const push = (b) => { if (typeof b === "string") b = enc.encode(b); parts.push(b); len += b.length; };
    const obj = (n, head, stream) => {
      offsets[n] = len;
      push(`${n} 0 obj\n${head}\n`);
      if (stream) { push("stream\n"); for (const p of [].concat(stream)) push(p); push("\nendstream\n"); }
      push("endobj\n");
    };
    const n = list.length;
    let next = 3 + n;  // objects 3..2+n are the pages; content/image streams follow
    push("%PDF-1.4\n%\xE2\xE3\xCF\xD3\n");
    obj(1, "<< /Type /Catalog /Pages 2 0 R >>");
    obj(2, `<< /Type /Pages /Count ${n} /Kids [${list.map((_, i) => `${3 + i} 0 R`).join(" ")}] >>`);
    for (let i = 0; i < n; i++) {
      const img = await prepare(list[i].file);
      const k = Math.min(1, MAX_PAGE_PT / Math.max(img.w, img.h));
      const pw = +(img.w * k).toFixed(2), ph = +(img.h * k).toFixed(2);
      const rotate = ((img.turns + list[i].rot) % 4) * 90;
      const content = enc.encode(`q ${pw} 0 0 ${ph} 0 0 cm /Im0 Do Q`);
      const contentId = next++, imageId = next++;
      obj(3 + i, `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pw} ${ph}] /Rotate ${rotate} /Resources << /XObject << /Im0 ${imageId} 0 R >> >> /Contents ${contentId} 0 R >>`);
      obj(contentId, `<< /Length ${content.length} >>`, content);
      if (img.jpeg) {
        const cs = img.comps === 1 ? "/DeviceGray" : img.comps === 4 ? "/DeviceCMYK" : "/DeviceRGB";
        const decode = img.comps === 4 && img.adobe ? " /Decode [1 0 1 0 1 0 1 0]" : "";  // Adobe CMYK is stored inverted
        obj(imageId, `<< /Type /XObject /Subtype /Image /Width ${img.w} /Height ${img.h} /ColorSpace ${cs}${decode} /BitsPerComponent 8 /Filter /DCTDecode /Length ${img.length} >>`, img.parts);
      } else {
        let smask = "";
        if (img.alpha) {
          const maskId = next++;
          smask = ` /SMask ${maskId} 0 R`;
          obj(maskId, `<< /Type /XObject /Subtype /Image /Width ${img.w} /Height ${img.h} /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length ${img.alpha.length} >>`, img.alpha);
        }
        obj(imageId, `<< /Type /XObject /Subtype /Image /Width ${img.w} /Height ${img.h} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode${smask} /Length ${img.rgb.length} >>`, img.rgb);
      }
    }
    const total = next;
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
      const pdf = await buildPdf(items);
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
