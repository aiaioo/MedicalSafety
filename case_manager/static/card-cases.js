// Edit which cases a report or document is associated with, from its card. window.openCaseLinks(opts):
//   kind: "report" | "doc", id, causeIds, caseIds (its current links), onChange(caseIds) after each change.
// Offers the cases the user can edit under the item's cause(s) (plus any already linked). Each tick is saved at
// once through /api/<kind>/<id>/links; the server refuses a change that would leave the item without a case the
// user needs for adding to its cause, and its message is shown.
(function () {
  let overlay = null;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function build() {
    overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true"><h3>Cases</h3><div class="card-cases-body"></div>' +
      '<p class="modal-hint card-cases-error" style="color:#c0392b" hidden></p>' +
      '<div class="modal-actions"><button type="button" class="card-cases-close">Done</button></div></div>';
    document.body.appendChild(overlay);
    const close = () => overlay.classList.remove("open");
    overlay.querySelector(".card-cases-close").addEventListener("click", close);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  }

  window.openCaseLinks = async function ({ kind, id, causeIds, caseIds, onChange }) {
    if (!overlay) build();
    const body = overlay.querySelector(".card-cases-body");
    const err = overlay.querySelector(".card-cases-error");
    err.hidden = true;
    body.textContent = "Loading…";
    overlay.classList.add("open");
    let cases = [];
    try {
      const res = await fetch("/api/allegation-cases");
      const all = await res.json();
      cases = Array.isArray(all) ? all : all.cases || [];
    } catch (e) {
      body.textContent = "Could not load cases.";
      return;
    }
    const linked = new Set(caseIds || []);
    const causes = new Set(causeIds || []);
    for (const c of cases) if (linked.has(c.id)) causes.add(c.cause_id);
    const candidates = cases.filter((c) => linked.has(c.id) || (causes.has(c.cause_id) && (c.role === "owner" || c.role === "editor")));
    if (!candidates.length) {
      body.innerHTML = '<p class="empty">No cases you can edit under this item\'s cause.</p>';
      return;
    }
    body.innerHTML = candidates
      .map((c) => `<label class="case-link-row"><input type="checkbox" value="${esc(c.id)}"${linked.has(c.id) ? " checked" : ""}> ${esc(c.name || c.id)}</label>`)
      .join("");
    body.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
      cb.addEventListener("change", async () => {
        err.hidden = true;
        try {
          const res = await fetch(`/api/${kind}/${encodeURIComponent(id)}/links`, {
            method: cb.checked ? "POST" : "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ case_id: cb.value }),
          });
          if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
          if (cb.checked) linked.add(cb.value); else linked.delete(cb.value);
          if (onChange) onChange([...linked]);
        } catch (e) {
          cb.checked = !cb.checked;
          err.textContent = e.message;
          err.hidden = false;
        }
      });
    });
  };
})();
