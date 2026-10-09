// The causes, cases and allegations pages autosave, so Ctrl/Cmd+S has no page-save to offer. Cards that are
// editable flush their pending save on it (see each page's script); anywhere else -- focus on the page
// background, a button, or a read-only card -- it would only open the browser's "Save page as" dialog, so
// swallow it.
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "s") e.preventDefault();
});
