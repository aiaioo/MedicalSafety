// Webpage ("article") editor for content creators -- the same Tiptap setup
// static/src/editor.js uses for reports, minus pagination/margins/page
// numbers (a webpage scrolls continuously, it's never printed), plus a
// link mark and an "insert image" upload button reports don't have.
import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { TextStyleKit } from "@tiptap/extension-text-style";
import { Highlight } from "@tiptap/extension-highlight";
import { TextAlign } from "@tiptap/extension-text-align";
import { Link } from "@tiptap/extension-link";
import { setupLinkAndImage } from "./linkImage.js";
import { ResizableImage, setSelectedImageAlign, isImageSelected } from "./resizableImage.js";

(function () {
  const appEl = document.getElementById("articleApp");
  const articleId = appEl.dataset.article;
  const articlesUrl = appEl.dataset.articlesUrl;
  const articleUrl = appEl.dataset.articleUrl;
  const imageUploadUrl = appEl.dataset.imageUploadUrl;

  const editorEl = document.getElementById("editor");
  const titleInput = document.getElementById("titleInput");
  const saveBtn = document.getElementById("saveBtn");
  const saveStatusEl = document.getElementById("saveStatus");
  const summaryInput = document.getElementById("summaryInput");
  const publishedInput = document.getElementById("publishedInput");
  const thumbnailPicker = document.getElementById("thumbnailPicker");
  let thumbnailImageId = "";

  const fileMenuBtn = document.getElementById("fileMenuBtn");
  const fileMenuDropdown = document.getElementById("fileMenuDropdown");
  const newDocBtn = document.getElementById("newDocBtn");
  const openDocBtn = document.getElementById("openDocBtn");

  const newDocModal = document.getElementById("newDocModal");
  const newDocName = document.getElementById("newDocName");
  const newDocError = document.getElementById("newDocError");
  const newDocCancel = document.getElementById("newDocCancel");
  const newDocCreate = document.getElementById("newDocCreate");

  const openDocModal = document.getElementById("openDocModal");
  const openDocList = document.getElementById("openDocList");
  const openDocCancel = document.getElementById("openDocCancel");

  const articleListLanding = document.getElementById("articleListLanding");
  const landingEmptyHint = document.getElementById("landingEmptyHint");

  let dirty = false;
  let saving = false;
  let saveAgainAfter = false;
  let autosaveTimer = null;
  let loadingArticle = false;

  function setStatus(text, isError) {
    saveStatusEl.textContent = text || "";
    saveStatusEl.style.color = isError ? "#c0392b" : "#8a92a5";
  }

  function flashSaved(el) {
    el.classList.remove("save-flash");
    void el.offsetWidth;
    el.classList.add("save-flash");
    el.addEventListener("animationend", () => el.classList.remove("save-flash"), { once: true });
  }

  function fmtDate(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ---------------------------------------------------------------------
  // File menu + landing list
  // ---------------------------------------------------------------------
  fileMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    fileMenuDropdown.classList.toggle("open");
  });
  document.addEventListener("click", () => fileMenuDropdown.classList.remove("open"));

  function openModal(el) {
    el.classList.add("open");
  }
  function closeModal(el) {
    el.classList.remove("open");
  }

  newDocBtn.addEventListener("click", () => {
    newDocName.value = "";
    newDocError.style.display = "none";
    openModal(newDocModal);
    setTimeout(() => newDocName.focus(), 0);
  });
  newDocCancel.addEventListener("click", () => closeModal(newDocModal));
  newDocModal.addEventListener("click", (e) => {
    if (e.target === newDocModal) closeModal(newDocModal);
  });

  async function createArticle() {
    const title = newDocName.value.trim();
    if (!title) {
      newDocError.textContent = "Please enter a title for the webpage.";
      newDocError.style.display = "block";
      return;
    }
    newDocCreate.disabled = true;
    try {
      const res = await fetch(articlesUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || res.statusText);
      window.location.href = `/articles?article=${encodeURIComponent(data.id)}`;
    } catch (e) {
      newDocError.textContent = "Could not create webpage: " + e.message;
      newDocError.style.display = "block";
      newDocCreate.disabled = false;
    }
  }
  newDocCreate.addEventListener("click", createArticle);
  newDocName.addEventListener("keydown", (e) => {
    if (e.key === "Enter") createArticle();
  });

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

  function statusLabel(a) {
    const places = (a.sections || []).map((s) => `${s.website_name} › ${s.section_title}`).join(", ");
    if (a.published && places) return `Published • ${places}`;
    if (places) return `Draft • ${places}`;
    return "Draft • no section yet";
  }

  function wireCardDelete(card, deleteBtn, a, onDeleted) {
    deleteBtn.addEventListener("click", (e) => e.preventDefault());
    wireConfirmDelete(deleteBtn, async () => {
      try {
        const res = await fetch(`/api/article/${encodeURIComponent(a.id)}`, { method: "DELETE" });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
        card.remove();
        if (onDeleted) onDeleted();
      } catch (err) {
        window.alert("Could not delete webpage: " + err.message);
      }
    });
  }

  // `withThumbnail`: the main landing grid looks like a public listing card
  // (thumbnail + summary, see templates/public.html); the compact "Open a
  // webpage" modal list just shows the title and status.
  function renderArticleCard(a, container, onDeleted, withThumbnail) {
    const card = document.createElement("a");
    card.href = `/articles?article=${encodeURIComponent(a.id)}`;
    if (withThumbnail) {
      card.className = "article-card";
      const thumbnail = a.thumbnail_image_id
        ? `<img class="thumbnail" src="/media/article-images/${encodeURIComponent(a.id)}/${encodeURIComponent(a.thumbnail_image_id)}?thumb=1" alt="">`
        : "";
      card.innerHTML = `
        <button type="button" class="article-card-delete card-delete-btn" title="Delete webpage">✕</button>
        ${thumbnail}
        <div class="card-body">
          <div class="title">${escapeHtml(a.title || a.id)}</div>
          ${a.summary ? `<p class="summary">${escapeHtml(a.summary)}</p>` : ""}
          <div class="meta">${escapeHtml(statusLabel(a))}</div>
          <div class="meta">Updated ${escapeHtml(fmtDate(a.updated_at))}</div>
        </div>`;
      wireCardDelete(card, card.querySelector(".article-card-delete"), a, onDeleted);
    } else {
      card.className = "report-card";
      card.innerHTML = `
        <button type="button" class="report-card-delete card-delete-btn" title="Delete webpage">✕</button>
        <div class="report-card-name">${escapeHtml(a.title || a.id)}</div>
        <div class="report-card-meta">${escapeHtml(statusLabel(a))}</div>
        <div class="report-card-meta">Updated ${escapeHtml(fmtDate(a.updated_at))}</div>`;
      wireCardDelete(card, card.querySelector(".report-card-delete"), a, onDeleted);
    }
    container.appendChild(card);
  }

  async function fetchArticles() {
    const res = await fetch(articlesUrl);
    return res.json();
  }

  openDocBtn.addEventListener("click", async () => {
    openDocList.innerHTML = '<p class="empty">Loading&hellip;</p>';
    openModal(openDocModal);
    try {
      const list = await fetchArticles();
      openDocList.innerHTML = "";
      if (!list.length) {
        openDocList.innerHTML = '<p class="empty">No webpages yet.</p>';
        return;
      }
      for (const a of list) {
        renderArticleCard(a, openDocList, () => {
          if (!openDocList.querySelector(".report-card")) {
            openDocList.innerHTML = '<p class="empty">No webpages yet.</p>';
          }
        }, false);
      }
    } catch (e) {
      openDocList.innerHTML = '<p class="empty">Failed to load webpages.</p>';
    }
  });
  openDocCancel.addEventListener("click", () => closeModal(openDocModal));
  openDocModal.addEventListener("click", (e) => {
    if (e.target === openDocModal) closeModal(openDocModal);
  });

  async function loadLanding() {
    if (!articleListLanding) return;
    try {
      const list = await fetchArticles();
      articleListLanding.innerHTML = "";
      landingEmptyHint.style.display = list.length ? "none" : "block";
      for (const a of list) {
        renderArticleCard(a, articleListLanding, () => {
          landingEmptyHint.style.display = articleListLanding.querySelector(".article-card") ? "none" : "block";
        }, true);
      }
    } catch (e) {
      console.error(e);
    }
  }

  if (!articleId) {
    loadLanding();
    return; // nothing else to wire up until a webpage is open
  }

  // ---------------------------------------------------------------------
  // Tiptap editor
  // ---------------------------------------------------------------------
  const editor = new Editor({
    element: editorEl,
    extensions: [
      StarterKit,
      TextStyleKit.configure({ backgroundColor: false, lineHeight: false }),
      Highlight.configure({ multicolor: true }),
      TextAlign.configure({ types: ["heading", "paragraph"] }),
      Link.configure({ openOnClick: false, autolink: false }),
      ResizableImage,
    ],
    content: "",
    editorProps: {
      attributes: { class: "tiptap-content" },
    },
    onUpdate: () => markDirty(),
  });

  editor.view.dom.setAttribute("data-placeholder", "Start writing your webpage.");

  // Tab/Shift+Tab sink/lift within a list, same as the report editor.
  editorEl.addEventListener(
    "keydown",
    (e) => {
      if (e.key !== "Tab") return;
      e.preventDefault();
      if (!e.shiftKey && editor.can().sinkListItem("listItem")) editor.chain().focus().sinkListItem("listItem").run();
      else if (e.shiftKey && editor.can().liftListItem("listItem")) editor.chain().focus().liftListItem("listItem").run();
    },
    true
  );

  const undoBtn = document.getElementById("undoBtn");
  const redoBtn = document.getElementById("redoBtn");
  undoBtn.addEventListener("click", () => editor.chain().focus().undo().run());
  redoBtn.addEventListener("click", () => editor.chain().focus().redo().run());
  function syncUndoRedoButtons() {
    undoBtn.disabled = !editor.can().undo();
    redoBtn.disabled = !editor.can().redo();
  }
  editor.on("transaction", syncUndoRedoButtons);
  syncUndoRedoButtons();

  document.getElementById("insertOrderedListBtn").addEventListener("click", () => {
    editor.chain().focus().toggleOrderedList().run();
  });
  document.getElementById("insertUnorderedListBtn").addEventListener("click", () => {
    editor.chain().focus().toggleBulletList().run();
  });

  // ---------------------------------------------------------------------
  // Link + image
  // ---------------------------------------------------------------------
  setupLinkAndImage({
    editor, imageUploadUrl, setStatus, onChange: markDirty, onImageUploaded: loadThumbnailChoices,
  });

  // ---------------------------------------------------------------------
  // Thumbnail picker: choose one of the article's own uploaded images.
  // ---------------------------------------------------------------------
  function renderThumbnailPicker(images) {
    thumbnailPicker.innerHTML = "";
    if (!images.length) {
      thumbnailPicker.innerHTML = '<p class="empty">Insert an image into the webpage to choose a thumbnail.</p>';
      return;
    }
    const none = document.createElement("button");
    none.type = "button";
    none.className = "thumbnail-option" + (thumbnailImageId ? "" : " selected");
    none.textContent = "None";
    none.title = "No thumbnail";
    none.addEventListener("click", () => {
      thumbnailImageId = "";
      renderThumbnailPicker(images);
      markDirty();
    });
    thumbnailPicker.appendChild(none);
    for (const img of images) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "thumbnail-option" + (img.id === thumbnailImageId ? " selected" : "");
      btn.innerHTML = `<img src="${img.url}" alt="">`;
      btn.addEventListener("click", () => {
        thumbnailImageId = img.id;
        renderThumbnailPicker(images);
        markDirty();
      });
      thumbnailPicker.appendChild(btn);
    }
  }

  async function loadThumbnailChoices() {
    try {
      const res = await fetch(`/api/article/${encodeURIComponent(articleId)}/images`);
      renderThumbnailPicker(await res.json());
    } catch (e) {
      console.error(e);
    }
  }

  // ---------------------------------------------------------------------
  // Formatting toolbar (same behaviour as the report editor's)
  // ---------------------------------------------------------------------
  const TEXT_ALIGNS = { justifyLeft: "left", justifyCenter: "center", justifyRight: "right", justifyFull: "justify" };
  const MARK_TOGGLES = { bold: "bold", italic: "italic", underline: "underline" };

  document.querySelectorAll(".fmt-btn[data-cmd]").forEach((btn) => {
    const cmd = btn.dataset.cmd;
    btn.addEventListener("click", () => {
      if (TEXT_ALIGNS[cmd] && isImageSelected(editor)) {
        setSelectedImageAlign(editor, TEXT_ALIGNS[cmd] === "justify" ? "left" : TEXT_ALIGNS[cmd]);
      } else if (TEXT_ALIGNS[cmd]) {
        editor.chain().focus().setTextAlign(TEXT_ALIGNS[cmd]).run();
      } else if (MARK_TOGGLES[cmd]) {
        editor.chain().focus().toggleMark(MARK_TOGGLES[cmd]).run();
      }
    });
  });

  const fontFamilySelect = document.getElementById("fontFamilySelect");
  fontFamilySelect.addEventListener("change", () => {
    editor.chain().focus().setFontFamily(fontFamilySelect.value).run();
  });

  const fontSizeSelect = document.getElementById("fontSizeSelect");
  fontSizeSelect.addEventListener("change", () => {
    editor.chain().focus().setFontSize(fontSizeSelect.value + "pt").run();
  });

  function selectedBlockContentRange(state) {
    const { $from, $to } = state.selection;
    let start = null;
    let end = null;
    state.doc.nodesBetween($from.pos, $to.pos, (node, pos) => {
      if (!node.isTextblock) return;
      const s = pos + 1;
      const e = pos + node.nodeSize - 1;
      start = start === null ? s : Math.min(start, s);
      end = end === null ? e : Math.max(end, e);
    });
    return start === null ? null : { from: start, to: end };
  }

  const blockFormatSelect = document.getElementById("blockFormatSelect");
  blockFormatSelect.addEventListener("change", () => {
    const val = blockFormatSelect.value;
    const { from: cursorFrom, to: cursorTo } = editor.state.selection;
    const blockRange = selectedBlockContentRange(editor.state);
    let chain = editor.chain().focus();
    chain = val === "P" ? chain.setParagraph() : chain.setHeading({ level: Number(val.slice(1)) });
    if (blockRange) {
      chain = chain.setTextSelection(blockRange).unsetFontSize().setTextSelection({ from: cursorFrom, to: cursorTo });
    }
    chain.run();
  });

  function mountColorPopover(name, onPick) {
    const btn = document.getElementById(name + "ColorBtn");
    const popover = document.getElementById(name + "ColorPopover");
    const input = document.getElementById(name + "ColorInput");
    const swatch = document.getElementById(name + "ColorSwatch");
    const setOpen = (open) => {
      popover.hidden = !open;
      btn.setAttribute("aria-expanded", open);
    };
    const select = PageNumberPalette.mount(document.getElementById(name + "ColorPalette"), (c) => {
      input.value = c;
      swatch.style.background = c;
      select(c);
      setOpen(false);
      onPick(c);
    });
    btn.addEventListener("mousedown", (e) => e.preventDefault());
    btn.addEventListener("click", () => setOpen(popover.hidden));
    document.addEventListener("mousedown", (e) => {
      if (!popover.hidden && !popover.contains(e.target) && !btn.contains(e.target)) setOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") setOpen(false);
    });
    swatch.style.background = input.value;
    select(input.value);
    return { input, swatch, select };
  }

  const textPicker = mountColorPopover("text", (c) => editor.chain().focus().setColor(c).run());
  const textColorInput = textPicker.input;
  const textColorSwatch = textPicker.swatch;

  const highlightPicker = mountColorPopover("highlight", (c) => editor.chain().focus().setHighlight({ color: c }).run());
  document.getElementById("noHighlightBtn").addEventListener("click", () => {
    editor.chain().focus().unsetHighlight().run();
    document.getElementById("highlightColorPopover").hidden = true;
    document.getElementById("highlightColorBtn").setAttribute("aria-expanded", false);
  });

  const DEFAULT_FONT_FAMILY = fontFamilySelect.options[0].value;
  const DEFAULT_FONT_SIZE = "11";
  const DEFAULT_TEXT_COLOR = textColorInput.value;

  function syncToolbarToSelection() {
    const styleAttrs = editor.getAttributes("textStyle");
    fontFamilySelect.value = styleAttrs.fontFamily || DEFAULT_FONT_FAMILY;

    const size = styleAttrs.fontSize ? styleAttrs.fontSize.replace(/pt$/, "") : DEFAULT_FONT_SIZE;
    if ([...fontSizeSelect.options].some((opt) => opt.value === size)) fontSizeSelect.value = size;

    const color = styleAttrs.color || DEFAULT_TEXT_COLOR;
    textColorInput.value = color;
    textColorSwatch.style.background = color;
    textPicker.select(color);

    const highlightColor = editor.getAttributes("highlight").color;
    const highlightColorSwatch = document.getElementById("highlightColorSwatch");
    const highlightColorInput = document.getElementById("highlightColorInput");
    if (highlightColor) {
      highlightColorInput.value = highlightColor;
      highlightColorSwatch.style.background = highlightColor;
      highlightColorSwatch.style.outline = "";
      highlightPicker.select(highlightColorInput.value);
    } else {
      highlightColorSwatch.style.background = "transparent";
      highlightColorSwatch.style.outline = "1px solid #b8bfcf";
      highlightPicker.select(null);
    }

    let headingLevel = null;
    for (let level = 1; level <= 3; level++) {
      if (editor.isActive("heading", { level })) headingLevel = level;
    }
    blockFormatSelect.value = headingLevel ? `H${headingLevel}` : "P";

    document.querySelectorAll(".fmt-btn[data-cmd]").forEach((btn) => {
      const cmd = btn.dataset.cmd;
      let active;
      if (MARK_TOGGLES[cmd]) active = editor.isActive(MARK_TOGGLES[cmd]);
      else if (TEXT_ALIGNS[cmd]) active = editor.isActive({ textAlign: TEXT_ALIGNS[cmd] });
      else return;
      btn.classList.toggle("active", active);
    });
    document.getElementById("insertUnorderedListBtn").classList.toggle("active", editor.isActive("bulletList"));
    document.getElementById("insertOrderedListBtn").classList.toggle("active", editor.isActive("orderedList"));
    document.getElementById("linkBtn").classList.toggle("active", editor.isActive("link"));
  }
  editor.on("transaction", syncToolbarToSelection);
  syncToolbarToSelection();

  // ---------------------------------------------------------------------
  // Publish sidebar
  // ---------------------------------------------------------------------
  const sectionCheckboxes = Array.from(document.querySelectorAll(".section-checkbox"));

  function selectedSectionIds() {
    return sectionCheckboxes.filter((cb) => cb.checked).map((cb) => cb.value);
  }
  function setSelectedSectionIds(ids) {
    const set = new Set(ids);
    sectionCheckboxes.forEach((cb) => {
      cb.checked = set.has(cb.value);
    });
  }
  function syncPublishedEnabled() {
    const any = selectedSectionIds().length > 0;
    publishedInput.disabled = !any;
    if (!any) publishedInput.checked = false;
  }
  sectionCheckboxes.forEach((cb) =>
    cb.addEventListener("change", () => {
      syncPublishedEnabled();
      markDirty();
    })
  );
  publishedInput.addEventListener("change", markDirty);
  summaryInput.addEventListener("input", markDirty);

  // ---------------------------------------------------------------------
  // Autosave / save
  // ---------------------------------------------------------------------
  function markDirty() {
    if (loadingArticle) return;
    dirty = true;
    setStatus("Saving…");
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => {
      saveArticle().catch((e) => setStatus("Save failed: " + e.message, true));
    }, 1200);
  }

  async function saveArticle() {
    if (saving) {
      saveAgainAfter = true;
      return;
    }
    saving = true;
    if (autosaveTimer) {
      clearTimeout(autosaveTimer);
      autosaveTimer = null;
    }
    const payload = {
      title: titleInput.value.trim() || "Untitled webpage",
      doc: editor.getJSON(),
      summary: summaryInput.value,
      section_ids: selectedSectionIds(),
      published: publishedInput.checked,
      thumbnail_image_id: thumbnailImageId,
    };
    try {
      const res = await fetch(articleUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(await res.text());
      dirty = false;
      setStatus("Saved");
    } finally {
      saving = false;
      if (saveAgainAfter) {
        saveAgainAfter = false;
        await saveArticle();
      }
    }
  }

  titleInput.addEventListener("input", markDirty);
  titleInput.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    setTimeout(() => {
      saveArticle().then(() => flashSaved(titleInput)).catch((err) => setStatus("Save failed: " + err.message, true));
    }, 0);
  });

  window.addEventListener("beforeunload", () => {
    if (!dirty) return;
    try {
      const blob = new Blob(
        [JSON.stringify({
          title: titleInput.value.trim() || "Untitled webpage", doc: editor.getJSON(),
          summary: summaryInput.value, section_ids: selectedSectionIds(),
          published: publishedInput.checked, thumbnail_image_id: thumbnailImageId,
        })],
        { type: "application/json" }
      );
      navigator.sendBeacon(articleUrl, blob);
    } catch (err) {
      // best effort only
    }
  });

  saveBtn.addEventListener("click", async () => {
    try {
      await saveArticle();
    } catch (e) {
      setStatus("Save failed: " + e.message, true);
    }
  });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveArticle().then(() => flashSaved(titleInput)).catch((err) => setStatus("Save failed: " + err.message, true));
    }
  });

  // ---------------------------------------------------------------------
  // Initial load
  // ---------------------------------------------------------------------
  async function loadArticle() {
    loadingArticle = true;
    try {
      const res = await fetch(articleUrl);
      const data = await res.json();
      titleInput.value = data.title || "";
      summaryInput.value = data.summary || "";
      setSelectedSectionIds(data.section_ids || []);
      publishedInput.checked = !!data.published;
      syncPublishedEnabled();
      thumbnailImageId = data.thumbnail_image_id || "";
      const loadChain = editor.chain().setMeta("addToHistory", false);
      if (data.doc && data.doc.type === "doc") loadChain.setContent(data.doc);
      else loadChain.setContent("");
      loadChain.run();
      dirty = false;
      setStatus("");
      await loadThumbnailChoices(); // after thumbnailImageId is set, so the right one renders selected
    } catch (e) {
      setStatus("Failed to load webpage: " + e.message, true);
    } finally {
      loadingArticle = false;
    }
  }

  loadArticle();
})();
