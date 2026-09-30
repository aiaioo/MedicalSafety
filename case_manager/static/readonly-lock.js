// Puts a rendered card or detail pane into a read-only state for users who
// can view but not edit it: text fields become read-only (still selectable),
// every other field and button (save, delete, add, pickers) is disabled, and
// drag-to-reorder is switched off. The server enforces the same rule; this
// just stops the page offering edits that would be rejected.
window.ReadOnlyLock = {
  lock(root) {
    root.classList.add("ro-locked");
    root.querySelectorAll("input, textarea, select, button").forEach((el) => {
      const textLike = el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && /^(text|search|number|date|)$/.test(el.type));
      if (textLike) el.readOnly = true;
      else el.disabled = true;
    });
    root.querySelectorAll("[draggable=true]").forEach((el) => { el.draggable = false; });
    if (root.draggable) root.draggable = false;
  },
};
