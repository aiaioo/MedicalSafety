(function () {
  const appEl = document.getElementById("app");
  const docId = appEl.dataset.doc;
  const docType = appEl.dataset.type;
  const pageCount = parseInt(appEl.dataset.pageCount, 10);
  const renderUrl = appEl.dataset.renderUrl;
  const annotationsUrl = appEl.dataset.annotationsUrl;
  const snippetUrl = appEl.dataset.snippetUrl;
  const snippetsListUrl = appEl.dataset.snippetsListUrl;
  const snippetDeleteBase = appEl.dataset.snippetDeleteBase; // contains literal "__ID__"
  const downloadUrl = appEl.dataset.downloadUrl;

  const img = document.getElementById("pageImage");
  const canvas = document.getElementById("overlay");
  const ctx = canvas.getContext("2d");
  const pageWrap = document.getElementById("pageWrap");
  const container = document.getElementById("viewerContainer");
  const statusMsg = document.getElementById("statusMsg");
  const snippetListEl = document.getElementById("snippetList");
  const colorPicker = document.getElementById("colorPicker");
  const pageInput = document.getElementById("pageInput");
  const modeHint = document.getElementById("modeHint");

  const HINTS = {
    null: 'Click an annotation to select it, then press Delete or right-click to remove it (right-click works in any mode)',
    rect: "Drag to draw a rectangle",
    freehand: "Drag to draw a freehand line",
    snippet: "Drag to extract a rectangular snippet",
  };

  let mode = null; // null = idle/select mode; otherwise "rect" | "freehand" | "snippet"
  let annotations = []; // {kind:'rect', color, x,y,w,h} | {kind:'freehand', color, points:[[x,y],...]}
  let annotationSnippetIds = []; // parallel to annotations: id of its auto-created snippet, or null
  let drawing = false;
  let startFrac = null;
  let currentStroke = null;
  let liveRect = null;
  let dirty = false;
  let selectedIndex = null;

  function setStatus(msg, isError) {
    statusMsg.textContent = msg || "";
    statusMsg.style.color = isError ? "#c0392b" : "#2a7a2a";
    if (msg) {
      setTimeout(() => {
        if (statusMsg.textContent === msg) statusMsg.textContent = "";
      }, 3000);
    }
  }

  // ---- mode buttons ----
  function setMode(newMode) {
    mode = newMode;
    document.querySelectorAll(".mode-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === newMode);
    });
    canvas.style.cursor = newMode === null ? "default" : "crosshair";
    modeHint.textContent = HINTS[newMode] || "";
    // a mode switch mid-draw shouldn't leave orphaned in-progress shapes
    liveRect = null;
    currentStroke = null;
    drawing = false;
    redraw();
  }

  document.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const isActive = btn.classList.contains("active");
      setMode(isActive ? null : btn.dataset.mode);
    });
  });

  setMode(null);

  // ---- layout: always fill the available width; height follows proportionally ----
  function layout() {
    if (!img.naturalWidth) return;
    const targetW = Math.max(100, container.clientWidth - 40);
    const targetH = targetW * (img.naturalHeight / img.naturalWidth);
    pageWrap.style.width = targetW + "px";
    pageWrap.style.height = targetH + "px";
    canvas.style.width = targetW + "px";
    canvas.style.height = targetH + "px";
  }

  img.addEventListener("load", () => {
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    layout();
    redraw();
  });

  // requestAnimationFrame-scheduled, not a fixed debounce, so the page tracks
  // a live window-resize drag every frame instead of waiting for it to pause
  let layoutQueued = false;
  const scheduleLayout = () => {
    if (layoutQueued) return;
    layoutQueued = true;
    requestAnimationFrame(() => {
      layoutQueued = false;
      layout();
    });
  };
  window.addEventListener("resize", scheduleLayout);
  if (window.ResizeObserver) {
    new ResizeObserver(scheduleLayout).observe(container);
  }

  img.src = renderUrl;

  // ---- coordinate helpers ----
  function clientToFrac(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    let x = (clientX - rect.left) / rect.width;
    let y = (clientY - rect.top) / rect.height;
    x = Math.min(1, Math.max(0, x));
    y = Math.min(1, Math.max(0, y));
    return [x, y];
  }

  // ---- hit testing for selection ----
  function distToSegment(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const lenSq = dx * dx + dy * dy;
    let t = lenSq === 0 ? 0 : ((px - x1) * dx + (py - y1) * dy) / lenSq;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }

  function freehandBoundsFrac(a) {
    let x0 = Infinity,
      y0 = Infinity,
      x1 = -Infinity,
      y1 = -Infinity;
    for (const [px, py] of a.points) {
      if (px < x0) x0 = px;
      if (py < y0) y0 = py;
      if (px > x1) x1 = px;
      if (py > y1) y1 = py;
    }
    return { x0, y0, x1, y1 };
  }

  // returns the index of the topmost annotation under (xFrac, yFrac), or -1
  function hitTestAnnotations(xFrac, yFrac) {
    const tol = Math.max(10, canvas.width * 0.008);
    const px = xFrac * canvas.width;
    const py = yFrac * canvas.height;
    for (let i = annotations.length - 1; i >= 0; i--) {
      const a = annotations[i];
      if (a.kind === "rect") {
        const rx = a.x * canvas.width;
        const ry = a.y * canvas.height;
        const rw = a.w * canvas.width;
        const rh = a.h * canvas.height;
        const corners = [
          [rx, ry],
          [rx + rw, ry],
          [rx + rw, ry + rh],
          [rx, ry + rh],
        ];
        let onEdge = false;
        for (let c = 0; c < 4; c++) {
          const [x1, y1] = corners[c];
          const [x2, y2] = corners[(c + 1) % 4];
          if (distToSegment(px, py, x1, y1, x2, y2) <= tol) {
            onEdge = true;
            break;
          }
        }
        if (onEdge) return i;
      } else if (a.kind === "freehand") {
        const pts = a.points;
        for (let j = 0; j < pts.length - 1; j++) {
          const x1 = pts[j][0] * canvas.width;
          const y1 = pts[j][1] * canvas.height;
          const x2 = pts[j + 1][0] * canvas.width;
          const y2 = pts[j + 1][1] * canvas.height;
          if (distToSegment(px, py, x1, y1, x2, y2) <= tol) return i;
        }
      }
    }
    return -1;
  }

  function drawSelectionHighlight(a) {
    ctx.save();
    ctx.strokeStyle = "#2266dd";
    ctx.setLineDash([7, 5]);
    ctx.lineWidth = 2;
    const pad = 6;
    if (a.kind === "rect") {
      const rx = a.x * canvas.width;
      const ry = a.y * canvas.height;
      ctx.strokeRect(rx - pad, ry - pad, a.w * canvas.width + pad * 2, a.h * canvas.height + pad * 2);
    } else if (a.kind === "freehand") {
      const b = freehandBoundsFrac(a);
      const rx = b.x0 * canvas.width;
      const ry = b.y0 * canvas.height;
      ctx.strokeRect(rx - pad, ry - pad, (b.x1 - b.x0) * canvas.width + pad * 2, (b.y1 - b.y0) * canvas.height + pad * 2);
    }
    ctx.restore();
  }

  // ---- drawing primitives ----
  function drawRect(a) {
    ctx.strokeStyle = a.color;
    ctx.lineWidth = Math.max(2, canvas.width * 0.0025);
    ctx.strokeRect(a.x * canvas.width, a.y * canvas.height, a.w * canvas.width, a.h * canvas.height);
  }

  function drawFreehand(a) {
    const pts = a.points;
    if (pts.length < 2) return;
    ctx.strokeStyle = a.color;
    ctx.lineWidth = Math.max(2, canvas.width * 0.0025);
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    const p0x = pts[0][0] * canvas.width;
    const p0y = pts[0][1] * canvas.height;
    ctx.moveTo(p0x, p0y);
    for (let i = 1; i < pts.length - 1; i++) {
      const cx = pts[i][0] * canvas.width;
      const cy = pts[i][1] * canvas.height;
      const nx = pts[i + 1][0] * canvas.width;
      const ny = pts[i + 1][1] * canvas.height;
      const mx = (cx + nx) / 2;
      const my = (cy + ny) / 2;
      ctx.quadraticCurveTo(cx, cy, mx, my);
    }
    const last = pts[pts.length - 1];
    ctx.lineTo(last[0] * canvas.width, last[1] * canvas.height);
    ctx.stroke();
  }

  function redraw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const a of annotations) {
      if (a.kind === "rect") drawRect(a);
      else if (a.kind === "freehand") drawFreehand(a);
    }
    if (selectedIndex !== null && annotations[selectedIndex]) {
      drawSelectionHighlight(annotations[selectedIndex]);
    }
    if (liveRect) drawRect(liveRect);
    if (currentStroke) drawFreehand(currentStroke);
  }

  // ---- light smoothing applied to a finished freehand stroke ----
  function smoothPoints(points, windowSize) {
    if (points.length <= 2) return points;
    const out = [];
    for (let i = 0; i < points.length; i++) {
      let sx = 0,
        sy = 0,
        n = 0;
      for (let j = Math.max(0, i - windowSize); j <= Math.min(points.length - 1, i + windowSize); j++) {
        sx += points[j][0];
        sy += points[j][1];
        n++;
      }
      out.push([sx / n, sy / n]);
    }
    return out;
  }

  function boundsToPaddedRect(x0, y0, x1, y1, padPx) {
    const padX = padPx / canvas.width;
    const padY = padPx / canvas.height;
    const x = Math.max(0, x0 - padX);
    const y = Math.max(0, y0 - padY);
    const x1c = Math.min(1, x1 + padX);
    const y1c = Math.min(1, y1 + padY);
    return { x, y, w: x1c - x, h: y1c - y };
  }

  // ---- pointer events ----
  canvas.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return; // left/primary button only; right-click is handled via contextmenu
    if (mode === null) {
      const [x, y] = clientToFrac(e.clientX, e.clientY);
      const idx = hitTestAnnotations(x, y);
      selectedIndex = idx >= 0 ? idx : null;
      redraw();
      return;
    }
    selectedIndex = null;
    canvas.setPointerCapture(e.pointerId);
    drawing = true;
    const [x, y] = clientToFrac(e.clientX, e.clientY);
    if (mode === "rect" || mode === "snippet") {
      startFrac = [x, y];
      liveRect = { kind: "rect", color: mode === "snippet" ? "#2266dd" : colorPicker.value, x, y, w: 0, h: 0 };
    } else if (mode === "freehand") {
      currentStroke = { kind: "freehand", color: colorPicker.value, points: [[x, y]] };
    }
  });

  canvas.addEventListener("pointermove", (e) => {
    if (!drawing) {
      if (mode === null) {
        const [x, y] = clientToFrac(e.clientX, e.clientY);
        canvas.style.cursor = hitTestAnnotations(x, y) >= 0 ? "pointer" : "default";
      }
      return;
    }
    const [x, y] = clientToFrac(e.clientX, e.clientY);
    if (liveRect) {
      liveRect.x = Math.min(startFrac[0], x);
      liveRect.y = Math.min(startFrac[1], y);
      liveRect.w = Math.abs(x - startFrac[0]);
      liveRect.h = Math.abs(y - startFrac[1]);
      redraw();
    } else if (currentStroke) {
      const pts = currentStroke.points;
      const last = pts[pts.length - 1];
      if (Math.hypot(x - last[0], y - last[1]) > 0.0015) {
        pts.push([x, y]);
        redraw();
      }
    }
  });

  async function finishRect() {
    const r = liveRect;
    liveRect = null;
    drawing = false;
    if (!r || r.w < 0.002 || r.h < 0.002) {
      redraw();
      return;
    }
    if (mode === "rect") {
      annotations.push(r);
      const idx = annotationSnippetIds.push(null) - 1;
      dirty = true;
      redraw();
      const snippet = await createSnippet(boundsToPaddedRect(r.x, r.y, r.x + r.w, r.y + r.h, 5));
      if (snippet && annotationSnippetIds.length > idx) annotationSnippetIds[idx] = snippet.id;
    } else if (mode === "snippet") {
      redraw();
      await createSnippet(r);
    }
  }

  async function finishFreehand() {
    const s = currentStroke;
    currentStroke = null;
    drawing = false;
    if (!s || s.points.length < 2) {
      redraw();
      return;
    }
    s.points = smoothPoints(s.points, 2);
    annotations.push(s);
    const idx = annotationSnippetIds.push(null) - 1;
    dirty = true;
    redraw();
    const b = freehandBoundsFrac(s);
    const snippet = await createSnippet(boundsToPaddedRect(b.x0, b.y0, b.x1, b.y1, 5));
    if (snippet && annotationSnippetIds.length > idx) annotationSnippetIds[idx] = snippet.id;
  }

  canvas.addEventListener("pointerup", () => {
    if (!drawing) return;
    if (mode === "rect" || mode === "snippet") finishRect();
    else if (mode === "freehand") finishFreehand();
  });
  canvas.addEventListener("pointercancel", () => {
    liveRect = null;
    currentStroke = null;
    drawing = false;
    redraw();
  });

  // ---- selection deletion (Delete/Backspace key, right-click menu) ----
  function deleteSelectedAnnotation() {
    if (selectedIndex === null || !annotations[selectedIndex]) return;
    annotations.splice(selectedIndex, 1);
    const [snippetId] = annotationSnippetIds.splice(selectedIndex, 1);
    selectedIndex = null;
    dirty = true;
    redraw();
    setStatus("Annotation deleted (click Save to persist)");
    if (snippetId) deleteSnippet(snippetId);
  }

  window.addEventListener("keydown", (e) => {
    if (e.key !== "Delete" && e.key !== "Backspace") return;
    if (selectedIndex === null) return;
    const active = document.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.isContentEditable)) return;
    e.preventDefault();
    deleteSelectedAnnotation();
  });

  // ---- right-click context menu ----
  const contextMenu = document.createElement("div");
  contextMenu.className = "context-menu";
  contextMenu.innerHTML = '<button type="button" class="context-menu-delete">Delete annotation</button>';
  document.body.appendChild(contextMenu);

  function showContextMenu(clientX, clientY) {
    contextMenu.style.left = clientX + "px";
    contextMenu.style.top = clientY + "px";
    contextMenu.classList.add("open");
  }
  function hideContextMenu() {
    contextMenu.classList.remove("open");
  }

  contextMenu.querySelector(".context-menu-delete").addEventListener("click", () => {
    deleteSelectedAnnotation();
    hideContextMenu();
  });
  document.addEventListener("click", (e) => {
    if (!contextMenu.contains(e.target)) hideContextMenu();
  });
  window.addEventListener("blur", hideContextMenu);
  window.addEventListener("resize", hideContextMenu);

  canvas.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    const [x, y] = clientToFrac(e.clientX, e.clientY);
    const idx = hitTestAnnotations(x, y);
    selectedIndex = idx >= 0 ? idx : null;
    redraw();
    if (idx >= 0) {
      showContextMenu(e.clientX, e.clientY);
    } else {
      hideContextMenu();
    }
  });

  // ---- annotations load/save ----
  async function loadAnnotations() {
    try {
      const res = await fetch(annotationsUrl);
      annotations = await res.json();
      annotationSnippetIds = annotations.map(() => null);
      selectedIndex = null;
      redraw();
    } catch (e) {
      console.error(e);
    }
  }

  document.getElementById("saveBtn").addEventListener("click", async () => {
    try {
      const res = await fetch(annotationsUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ annotations }),
      });
      if (!res.ok) throw new Error(await res.text());
      dirty = false;
      setStatus("Saved");
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  });

  document.getElementById("downloadBtn").addEventListener("click", () => {
    window.location.href = downloadUrl;
  });

  document.getElementById("undoBtn").addEventListener("click", () => {
    if (!annotations.length) return;
    annotations.pop();
    const snippetId = annotationSnippetIds.pop();
    selectedIndex = null;
    dirty = true;
    redraw();
    if (snippetId) deleteSnippet(snippetId);
  });

  const clearBtn = document.getElementById("clearBtn");
  clearBtn.addEventListener("click", async () => {
    if (!annotations.length) return;
    if (!clearBtn.classList.contains("confirming")) {
      clearBtn.classList.add("confirming");
      clearBtn.textContent = "Confirm clear?";
      clearBtn._confirmTimer = setTimeout(() => {
        clearBtn.classList.remove("confirming");
        clearBtn.textContent = "Clear";
      }, 3000);
      return;
    }
    clearTimeout(clearBtn._confirmTimer);
    clearBtn.classList.remove("confirming");
    clearBtn.textContent = "Clear";
    const idsToDelete = annotationSnippetIds.filter(Boolean);
    annotations = [];
    annotationSnippetIds = [];
    selectedIndex = null;
    dirty = true;
    redraw();
    for (const id of idsToDelete) {
      await deleteSnippet(id);
    }
  });

  window.addEventListener("beforeunload", (e) => {
    if (dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  // ---- page jump ----
  pageInput.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    let p = parseInt(pageInput.value, 10);
    if (Number.isNaN(p)) return;
    p = Math.min(Math.max(1, p), pageCount);
    window.location.href = `?doc=${encodeURIComponent(docId)}&type=${encodeURIComponent(docType)}&page=${p}`;
  });

  // ---- snippets ----
  async function createSnippet(rect) {
    setStatus("Extracting snippet…");
    try {
      const res = await fetch(snippetUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x: rect.x, y: rect.y, w: rect.w, h: rect.h, annotations }),
      });
      if (!res.ok) throw new Error(await res.text());
      const result = await res.json();
      setStatus("Snippet saved");
      await loadSnippets();
      return result;
    } catch (e) {
      setStatus("Snippet failed: " + e.message, true);
      return null;
    }
  }

  async function loadSnippets() {
    try {
      const res = await fetch(snippetsListUrl);
      const list = await res.json();
      snippetListEl.innerHTML = "";
      if (!list.length) {
        snippetListEl.innerHTML = '<p class="empty">No snippets yet. Use "Snippet" mode and drag a rectangle.</p>';
        return;
      }
      for (const s of list) {
        const card = document.createElement("div");
        card.className = "snippet-card";
        card.innerHTML = `
          <a href="${s.url}" target="_blank" rel="noopener"><img src="${s.url}" alt="Snippet from page ${s.page}"></a>
          <div class="snippet-actions">
            <a href="${s.url}" download>Download</a>
            <button type="button" class="del-snippet">Delete</button>
          </div>`;
        const delBtn = card.querySelector(".del-snippet");
        delBtn.addEventListener("click", () => {
          if (!delBtn.classList.contains("confirming")) {
            delBtn.classList.add("confirming");
            delBtn.textContent = "Confirm?";
            delBtn._confirmTimer = setTimeout(() => {
              delBtn.classList.remove("confirming");
              delBtn.textContent = "Delete";
            }, 3000);
            return;
          }
          clearTimeout(delBtn._confirmTimer);
          deleteSnippet(s.id);
        });
        snippetListEl.appendChild(card);
      }
    } catch (e) {
      console.error(e);
      setStatus("Failed to load snippets: " + e.message, true);
    }
  }

  async function deleteSnippet(id) {
    try {
      const res = await fetch(snippetDeleteBase.replace("__ID__", encodeURIComponent(id)), { method: "DELETE" });
      if (!res.ok) throw new Error(await res.text());
      setStatus("Snippet deleted");
    } catch (e) {
      setStatus("Delete failed: " + e.message, true);
    } finally {
      loadSnippets();
    }
  }

  loadAnnotations();
  loadSnippets();
})();
