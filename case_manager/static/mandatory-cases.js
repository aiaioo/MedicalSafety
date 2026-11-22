// The cases a new report / document must be associated with when the user can add to the cause only through cases
// they can edit (templates/_default_cause_hint.html draws the checkboxes). No checkboxes -> nothing chosen, and the
// server picks its default.
window.mandatoryCaseIds = function () {
  return [...document.querySelectorAll(".mandatory-case-cb:checked")].map((cb) => cb.value);
};
window.mandatoryCaseQuery = function () {
  const ids = window.mandatoryCaseIds();
  return ids.length ? "?case_ids=" + encodeURIComponent(ids.join(",")) : "";
};
