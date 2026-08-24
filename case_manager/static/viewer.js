(function () {
  const appEl = document.getElementById("app");
  const docId = appEl.dataset.doc;
  const docType = appEl.dataset.type;
  const pageCount = parseInt(appEl.dataset.pageCount, 10);
  const initialPage = parseInt(appEl.dataset.page, 10);
  const infoUrl = appEl.dataset.infoUrl;
  const renderUrlBase = appEl.dataset.renderUrlBase; // contains literal "__PAGE__"
  const annotationsAllUrl = appEl.dataset.annotationsAllUrl;
  const annotationsUrlBase = appEl.dataset.annotationsUrlBase; // contains literal "__PAGE__"
  const snippetUrlBase = appEl.dataset.snippetUrlBase; // contains literal "__PAGE__"
  const snippetsAllUrl = appEl.dataset.snippetsAllUrl;
  const snippetDeleteBase = appEl.dataset.snippetDeleteBase; // contains literal "__ID__"
  const downloadUrl = appEl.dataset.downloadUrl;

  const container = document.getElementById("viewerContainer");
  const statusMsg = document.getElementById("statusMsg");
  const snippetListEl = document.getElementById("snippetList");
  const snippetsHeading = document.getElementById("snippetsHeading");
  const colorPicker = document.getElementById("colorPicker");
  const pageInput = document.getElementById("pageInput");
  const modeHint = document.getElementById("modeHint");
  const prevLink = document.getElementById("prevLink");
  const nextLink = document.getElementById("nextLink");
  const continuousToggle = document.getElementById("continuousToggle");

  const CONTINUOUS_PREF_KEY = "annotator:continuousView";

  const HINTS = {
    null: 'Click an annotation to select it, then press Delete or right-click to remove it (right-click works in any mode)',
    rect: "Drag to draw a rectangle",
    freehand: "Drag to draw a freehand line",
    snippet: "Drag to extract a rectangular snippet",
  };

  let mode = null; // null = idle/select mode; otherwise "rect" | "freehand" | "snippet"
  let continuousMode = localStorage.getItem(CONTINUOUS_PREF_KEY) === "1";
  let currentPage = initialPage; // "active" page: scrollspy-tracked in continuous mode
  let selected = null; // { controller, index } | null
  let allSnippets = [];
  let pageDims = []; // 0-indexed, {width, height} in PDF points, from /info

  const pageState = new Map(); // pageNum -> { annotations, annotationSnippetIds, dirty }
  const mounted = new Map(); // pageNum -> controller, only for currently-mounted pages

  function getState(pageNum) {
    if (!pageState.has(pageNum)) {
      pageState.set(pageNum, { annotations: [], annotationSnippetIds: [], dirty: false });
    }
    return pageState.get(pageNum);
  }

  function setStatus(msg, isError) {
    statusMsg.textContent = msg || "";
    statusMsg.style.color = isError ? "#c0392b" : "#2a7a2a";
    if (msg) {
      setTimeout(() => {
        if (statusMsg.textContent === msg) statusMsg.textContent = "";
      }, 3000);
    }
  }

  // ---- mode buttons (shared across all mounted pages) ----
  function setMode(newMode) {
    mode = newMode;
    document.querySelectorAll(".mode-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === newMode);
    });
    const cursor = newMode === null ? "default" : "crosshair";
    for (const controller of mounted.values()) {
      controller.canvas.style.cursor = cursor;
      controller.liveRect = null;
      controller.currentStroke = null;
      controller.drawing = false;
      controller.redraw();
    }
    modeHint.textContent = HINTS[newMode] || "";
  }

  document.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const isActive = btn.classList.contains("active");
      setMode(isActive ? null : btn.dataset.mode);
    });
  });

  // ---- pure geometry/drawing helpers, parametrized so every page's canvas can share them ----
  function clientToFrac(canvas, clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    let x = (clientX - rect.left) / rect.width;
    let y = (clientY - rect.top) / rect.height;
    x = Math.min(1, Math.max(0, x));
    y = Math.min(1, Math.max(0, y));
    return [x, y];
  }

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
  function hitTestAnnotations(canvas, annotations, xFrac, yFrac) {
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

  function drawSelectionHighlight(ctx, canvas, a) {
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

  function drawRect(ctx, canvas, a) {
    ctx.strokeStyle = a.color;
    ctx.lineWidth = Math.max(2, canvas.width * 0.0025);
    ctx.strokeRect(a.x * canvas.width, a.y * canvas.height, a.w * canvas.width, a.h * canvas.height);
  }

  function drawFreehand(ctx, canvas, a) {
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

  function boundsToPaddedRect(canvas, x0, y0, x1, y1, padPx) {
    const padX = padPx / canvas.width;
    const padY = padPx / canvas.height;
    const x = Math.max(0, x0 - padX);
    const y = Math.max(0, y0 - padY);
    const x1c = Math.min(1, x1 + padX);
    const y1c = Math.min(1, y1 + padY);
    return { x, y, w: x1c - x, h: y1c - y };
  }

  // ---- per-page controller: owns one page's DOM (wrap/img/canvas) and drawing state ----
  function buildController(pageNum) {
    const state = getState(pageNum);

    const wrap = document.createElement("div");
    wrap.className = "page-wrap";
    wrap.dataset.page = String(pageNum);

    const label = document.createElement("div");
    label.className = "page-label";
    label.textContent = "Page " + pageNum;

    const img = document.createElement("img");
    img.alt = `Page ${pageNum} of ${docId}`;
    img.loading = "lazy";

    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    canvas.style.cursor = mode === null ? "default" : "crosshair";

    wrap.appendChild(label);
    wrap.appendChild(img);
    wrap.appendChild(canvas);

    const controller = {
      pageNum,
      wrap,
      img,
      canvas,
      ctx,
      state,
      drawing: false,
      startFrac: null,
      currentStroke: null,
      liveRect: null,
    };

    function redraw() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (const a of state.annotations) {
        if (a.kind === "rect") drawRect(ctx, canvas, a);
        else if (a.kind === "freehand") drawFreehand(ctx, canvas, a);
      }
      if (selected && selected.controller === controller && state.annotations[selected.index]) {
        drawSelectionHighlight(ctx, canvas, state.annotations[selected.index]);
      }
      if (controller.liveRect) drawRect(ctx, canvas, controller.liveRect);
      if (controller.currentStroke) drawFreehand(ctx, canvas, controller.currentStroke);
    }
    controller.redraw = redraw;

    function layout() {
      const dims = pageDims[pageNum - 1];
      if (!dims) return;
      const targetW = Math.max(100, container.clientWidth - 40);
      const targetH = targetW * (dims.height / dims.width);
      wrap.style.width = targetW + "px";
      wrap.style.height = targetH + "px";
      canvas.style.width = targetW + "px";
      canvas.style.height = targetH + "px";
    }
    controller.layout = layout;

    img.addEventListener("load", () => {
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      layout();
      redraw();
    });
    img.src = renderUrlBase.replace("__PAGE__", pageNum) + "&dpi=150";

    async function finishRect() {
      const r = controller.liveRect;
      controller.liveRect = null;
      controller.drawing = false;
      if (!r || r.w < 0.002 || r.h < 0.002) {
        redraw();
        return;
      }
      if (mode === "rect") {
        state.annotations.push(r);
        const idx = state.annotationSnippetIds.push(null) - 1;
        state.dirty = true;
        redraw();
        const snippet = await createSnippet(controller, boundsToPaddedRect(canvas, r.x, r.y, r.x + r.w, r.y + r.h, 5));
        if (snippet && state.annotationSnippetIds.length > idx) state.annotationSnippetIds[idx] = snippet.id;
      } else if (mode === "snippet") {
        redraw();
        await createSnippet(controller, r);
      }
    }

    async function finishFreehand() {
      const s = controller.currentStroke;
      controller.currentStroke = null;
      controller.drawing = false;
      if (!s || s.points.length < 2) {
        redraw();
        return;
      }
      s.points = smoothPoints(s.points, 2);
      state.annotations.push(s);
      const idx = state.annotationSnippetIds.push(null) - 1;
      state.dirty = true;
      redraw();
      const b = freehandBoundsFrac(s);
      const snippet = await createSnippet(controller, boundsToPaddedRect(canvas, b.x0, b.y0, b.x1, b.y1, 5));
      if (snippet && state.annotationSnippetIds.length > idx) state.annotationSnippetIds[idx] = snippet.id;
    }

    canvas.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return; // left/primary button only; right-click is handled via contextmenu
      setCurrentPage(pageNum);
      if (mode === null) {
        const [x, y] = clientToFrac(canvas, e.clientX, e.clientY);
        const idx = hitTestAnnotations(canvas, state.annotations, x, y);
        selected = idx >= 0 ? { controller, index: idx } : null;
        redraw();
        return;
      }
      selected = null;
      canvas.setPointerCapture(e.pointerId);
      controller.drawing = true;
      const [x, y] = clientToFrac(canvas, e.clientX, e.clientY);
      if (mode === "rect" || mode === "snippet") {
        controller.startFrac = [x, y];
        controller.liveRect = { kind: "rect", color: mode === "snippet" ? "#2266dd" : colorPicker.value, x, y, w: 0, h: 0 };
      } else if (mode === "freehand") {
        controller.currentStroke = { kind: "freehand", color: colorPicker.value, points: [[x, y]] };
      }
    });

    canvas.addEventListener("pointermove", (e) => {
      if (!controller.drawing) {
        if (mode === null) {
          const [x, y] = clientToFrac(canvas, e.clientX, e.clientY);
          canvas.style.cursor = hitTestAnnotations(canvas, state.annotations, x, y) >= 0 ? "pointer" : "default";
        }
        return;
      }
      const [x, y] = clientToFrac(canvas, e.clientX, e.clientY);
      if (controller.liveRect) {
        controller.liveRect.x = Math.min(controller.startFrac[0], x);
        controller.liveRect.y = Math.min(controller.startFrac[1], y);
        controller.liveRect.w = Math.abs(x - controller.startFrac[0]);
        controller.liveRect.h = Math.abs(y - controller.startFrac[1]);
        redraw();
      } else if (controller.currentStroke) {
        const pts = controller.currentStroke.points;
        const last = pts[pts.length - 1];
        if (Math.hypot(x - last[0], y - last[1]) > 0.0015) {
          pts.push([x, y]);
          redraw();
        }
      }
    });

    canvas.addEventListener("pointerup", () => {
      if (!controller.drawing) return;
      if (mode === "rect" || mode === "snippet") finishRect();
      else if (mode === "freehand") finishFreehand();
    });
    canvas.addEventListener("pointercancel", () => {
      controller.liveRect = null;
      controller.currentStroke = null;
      controller.drawing = false;
      redraw();
    });

    canvas.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      setCurrentPage(pageNum);
      const [x, y] = clientToFrac(canvas, e.clientX, e.clientY);
      const idx = hitTestAnnotations(canvas, state.annotations, x, y);
      selected = idx >= 0 ? { controller, index: idx } : null;
      redraw();
      if (idx >= 0) {
        showContextMenu(e.clientX, e.clientY);
      } else {
        hideContextMenu();
      }
    });

    controller.undo = function () {
      if (!state.annotations.length) return;
      state.annotations.pop();
      const snippetId = state.annotationSnippetIds.pop();
      if (selected && selected.controller === controller) selected = null;
      state.dirty = true;
      redraw();
      if (snippetId) deleteSnippet(snippetId);
    };

    controller.clearAll = async function () {
      if (!state.annotations.length) return;
      const idsToDelete = state.annotationSnippetIds.filter(Boolean);
      state.annotations = [];
      state.annotationSnippetIds = [];
      if (selected && selected.controller === controller) selected = null;
      state.dirty = true;
      redraw();
      for (const id of idsToDelete) {
        await deleteSnippet(id);
      }
    };

    return controller;
  }

  // ---- mounting: which pages are actually rendered as DOM right now ----
  function mountPages(desiredPageNums) {
    for (const controller of mounted.values()) {
      pageObserver.unobserve(controller.wrap);
    }
    mounted.clear();
    container.innerHTML = "";
    selected = null; // ephemeral UI state; not worth preserving across a remount
    for (const p of desiredPageNums) {
      const controller = buildController(p);
      mounted.set(p, controller);
      container.appendChild(controller.wrap);
      pageObserver.observe(controller.wrap);
      controller.layout();
    }
    updateCurrentPageHighlight();
  }

  function applyMode() {
    appEl.classList.toggle("continuous", continuousMode);
    const desired = continuousMode ? Array.from({ length: pageCount }, (_, i) => i + 1) : [currentPage];
    mountPages(desired);
    if (continuousMode) {
      const controller = mounted.get(currentPage);
      if (controller) controller.wrap.scrollIntoView({ block: "start" });
    }
    renderSnippetSidebar();
    snippetsHeading.textContent = continuousMode ? "Snippets" : "Snippets on this page";
  }

  continuousToggle.checked = continuousMode;
  continuousToggle.addEventListener("change", () => {
    continuousMode = continuousToggle.checked;
    localStorage.setItem(CONTINUOUS_PREF_KEY, continuousMode ? "1" : "0");
    applyMode();
  });

  // ---- "active page": where Undo/Clear apply, and what the sidebar/page indicator track ----
  function setCurrentPage(p) {
    if (p === currentPage) return;
    currentPage = p;
    updateCurrentPageHighlight();
  }

  function updateCurrentPageHighlight() {
    pageInput.value = currentPage;
    for (const [p, controller] of mounted) {
      controller.wrap.classList.toggle("current-page", p === currentPage);
    }
    updateSnippetGroupHighlight();
  }

  function activeController() {
    if (selected) return selected.controller;
    return mounted.get(currentPage) || null;
  }

  // ---- scrollspy: track which mounted page is most visible, only matters in continuous mode ----
  const visibleRatios = new Map();
  const pageObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const p = parseInt(entry.target.dataset.page, 10);
        if (entry.isIntersecting) visibleRatios.set(p, entry.intersectionRatio);
        else visibleRatios.delete(p);
      }
      if (!continuousMode || !visibleRatios.size) return;
      let best = null,
        bestRatio = 0;
      for (const [p, ratio] of visibleRatios) {
        if (ratio > bestRatio) {
          bestRatio = ratio;
          best = p;
        }
      }
      if (best !== null) setCurrentPage(best);
    },
    { root: container, threshold: [0, 0.1, 0.25, 0.5, 0.75, 1] }
  );

  function scrollToPage(p) {
    p = Math.min(Math.max(1, p), pageCount);
    const controller = mounted.get(p);
    if (controller) {
      controller.wrap.scrollIntoView({ block: "start", behavior: "smooth" });
      setCurrentPage(p);
    }
  }

  // ---- layout: always fill the available width; height follows page aspect ratio ----
  function layoutAll() {
    for (const controller of mounted.values()) controller.layout();
  }

  // requestAnimationFrame-scheduled, not a fixed debounce, so the page tracks
  // a live window-resize drag every frame instead of waiting for it to pause
  let layoutQueued = false;
  const scheduleLayout = () => {
    if (layoutQueued) return;
    layoutQueued = true;
    requestAnimationFrame(() => {
      layoutQueued = false;
      layoutAll();
    });
  };
  window.addEventListener("resize", scheduleLayout);
  if (window.ResizeObserver) {
    new ResizeObserver(scheduleLayout).observe(container);
  }

  // ---- selection deletion (Delete/Backspace key, right-click menu) ----
  function deleteSelected() {
    if (!selected) return;
    const { controller, index } = selected;
    if (!controller.state.annotations[index]) return;
    controller.state.annotations.splice(index, 1);
    const [snippetId] = controller.state.annotationSnippetIds.splice(index, 1);
    selected = null;
    controller.state.dirty = true;
    controller.redraw();
    setStatus("Annotation deleted (click Save to persist)");
    if (snippetId) deleteSnippet(snippetId);
  }

  window.addEventListener("keydown", (e) => {
    if (e.key !== "Delete" && e.key !== "Backspace") return;
    if (!selected) return;
    const active = document.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.isContentEditable)) return;
    e.preventDefault();
    deleteSelected();
  });

  // ---- right-click context menu (shared single element; targets whatever's currently `selected`) ----
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
    deleteSelected();
    hideContextMenu();
  });
  document.addEventListener("click", (e) => {
    if (!contextMenu.contains(e.target)) hideContextMenu();
  });
  window.addEventListener("blur", hideContextMenu);
  window.addEventListener("resize", hideContextMenu);

  // ---- annotations load/save ----
  async function loadAllAnnotations() {
    try {
      const res = await fetch(annotationsAllUrl);
      const data = await res.json();
      for (const [pageKey, anns] of Object.entries(data)) {
        const p = parseInt(pageKey, 10);
        if (!Number.isFinite(p)) continue;
        const state = getState(p);
        state.annotations = Array.isArray(anns) ? anns : [];
        state.annotationSnippetIds = state.annotations.map(() => null);
        state.dirty = false;
      }
    } catch (e) {
      console.error(e);
    }
  }

  async function loadInfo() {
    try {
      const res = await fetch(infoUrl);
      const data = await res.json();
      pageDims = data.pages || [];
    } catch (e) {
      console.error(e);
    }
  }

  async function saveOnePage(pageNum) {
    const state = getState(pageNum);
    const url = annotationsUrlBase.replace("__PAGE__", pageNum);
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ annotations: state.annotations }),
    });
    if (!res.ok) throw new Error(await res.text());
    state.dirty = false;
  }

  document.getElementById("saveBtn").addEventListener("click", async () => {
    const dirtyPages = Array.from(pageState.entries())
      .filter(([, s]) => s.dirty)
      .map(([p]) => p);
    if (!dirtyPages.length) {
      setStatus("Nothing to save");
      return;
    }
    try {
      await Promise.all(dirtyPages.map(saveOnePage));
      setStatus(dirtyPages.length > 1 ? `Saved ${dirtyPages.length} pages` : "Saved");
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  });

  document.getElementById("downloadBtn").addEventListener("click", () => {
    window.location.href = downloadUrl;
  });

  document.getElementById("undoBtn").addEventListener("click", () => {
    const controller = activeController();
    if (controller) controller.undo();
  });

  const clearBtn = document.getElementById("clearBtn");
  clearBtn.addEventListener("click", async () => {
    const controller = activeController();
    if (!controller || !controller.state.annotations.length) return;
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
    await controller.clearAll();
  });

  window.addEventListener("beforeunload", (e) => {
    if (Array.from(pageState.values()).some((s) => s.dirty)) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  // ---- page navigation: full reload in single-page mode, in-page scroll in continuous mode ----
  prevLink.addEventListener("click", (e) => {
    if (!continuousMode) return;
    e.preventDefault();
    scrollToPage(currentPage - 1);
  });
  nextLink.addEventListener("click", (e) => {
    if (!continuousMode) return;
    e.preventDefault();
    scrollToPage(currentPage + 1);
  });
  pageInput.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    let p = parseInt(pageInput.value, 10);
    if (Number.isNaN(p)) return;
    p = Math.min(Math.max(1, p), pageCount);
    if (continuousMode) {
      scrollToPage(p);
    } else {
      window.location.href = `?doc=${encodeURIComponent(docId)}&type=${encodeURIComponent(docType)}&page=${p}`;
    }
  });

  // ---- snippets ----
  async function createSnippet(controller, rect) {
    setStatus("Extracting snippet…");
    try {
      const url = snippetUrlBase.replace("__PAGE__", controller.pageNum);
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x: rect.x, y: rect.y, w: rect.w, h: rect.h, annotations: controller.state.annotations }),
      });
      if (!res.ok) throw new Error(await res.text());
      const result = await res.json();
      setStatus("Snippet saved");
      await loadAllSnippets();
      return result;
    } catch (e) {
      setStatus("Snippet failed: " + e.message, true);
      return null;
    }
  }

  function buildSnippetCard(s) {
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
    return card;
  }

  function renderSnippetSidebar() {
    snippetListEl.innerHTML = "";
    if (continuousMode) {
      const byPage = new Map();
      for (const s of allSnippets) {
        if (!byPage.has(s.page)) byPage.set(s.page, []);
        byPage.get(s.page).push(s);
      }
      const pages = Array.from(byPage.keys()).sort((a, b) => a - b);
      if (!pages.length) {
        snippetListEl.innerHTML = '<p class="empty">No snippets yet. Use "Snippet" mode and drag a rectangle on any page.</p>';
        return;
      }
      for (const p of pages) {
        const group = document.createElement("div");
        group.className = "snippet-group";
        group.dataset.page = String(p);
        const header = document.createElement("div");
        header.className = "snippet-group-header";
        header.textContent = "Page " + p;
        header.addEventListener("click", () => scrollToPage(p));
        group.appendChild(header);
        const list = document.createElement("div");
        list.className = "snippet-list";
        for (const s of byPage.get(p)) list.appendChild(buildSnippetCard(s));
        group.appendChild(list);
        snippetListEl.appendChild(group);
      }
      updateSnippetGroupHighlight();
    } else {
      const pageSnippets = allSnippets.filter((s) => s.page === currentPage);
      if (!pageSnippets.length) {
        snippetListEl.innerHTML = '<p class="empty">No snippets yet. Use "Snippet" mode and drag a rectangle.</p>';
        return;
      }
      for (const s of pageSnippets) snippetListEl.appendChild(buildSnippetCard(s));
    }
  }

  function updateSnippetGroupHighlight() {
    if (!continuousMode) return;
    let matched = null;
    for (const group of snippetListEl.querySelectorAll(".snippet-group")) {
      const isCurrent = parseInt(group.dataset.page, 10) === currentPage;
      group.classList.toggle("current", isCurrent);
      if (isCurrent) matched = group;
    }
    if (matched) {
      const sidebarRect = snippetListEl.parentElement.getBoundingClientRect();
      const groupRect = matched.getBoundingClientRect();
      const inView = groupRect.top >= sidebarRect.top && groupRect.bottom <= sidebarRect.bottom;
      if (!inView) matched.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }

  async function loadAllSnippets() {
    try {
      const res = await fetch(snippetsAllUrl);
      allSnippets = await res.json();
      renderSnippetSidebar();
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
      loadAllSnippets();
    }
  }

  // ---- boot ----
  setMode(null);
  (async () => {
    await Promise.all([loadInfo(), loadAllAnnotations()]);
    applyMode();
    loadAllSnippets();
  })();
})();
