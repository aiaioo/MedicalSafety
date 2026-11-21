// Collapsible sections for the left panel of the cases and allegations pages: what each collaborator has shared
// with the user (cases or allegations), one section per collaborator, headed by their email address.
//
//   const collab = CollabSections.create(host, {meId, label: "case"});
//   await collab.load();                  // fetches the confirmed collaborators, to label sections by email
//   collab.render(items, {
//     groupOf: (item) => owner id,        // which collaborator's section an item goes in
//     buildCard: (item) => Element,
//     extraOwners: [ids],                 // owners to show a section for even with nothing in it
//     onAdd: (ownerId, formHost, close),  // optional: gives each section a "+ Add" button that fills `formHost`
//   });
window.CollabSections = (function () {
  const STORE = "collabSectionOpen:";

  function readOpen(key) {
    try { return localStorage.getItem(STORE + key) !== "0"; } catch (e) { return true; }
  }
  function writeOpen(key, open) {
    try { localStorage.setItem(STORE + key, open ? "1" : "0"); } catch (e) { /* storage unavailable */ }
  }

  function create(host, options) {
    const emails = new Map();
    const open = new Map(); // owner id -> whether its section is expanded (kept across re-renders)

    async function load() {
      try {
        const res = await fetch(options.collaboratorsUrl);
        if (res.ok) for (const c of await res.json()) emails.set(c.id, c.email);
      } catch (e) { /* sections are labelled "Shared by another user" without the list */ }
    }

    function isOpen(ownerId) {
      if (!open.has(ownerId)) open.set(ownerId, readOpen(ownerId));
      return open.get(ownerId);
    }

    function expand(ownerId) { open.set(ownerId, true); writeOpen(ownerId, true); }

    function labelOf(ownerId, items) {
      if (emails.has(ownerId)) return emails.get(ownerId);
      const named = items.find((i) => i.owner_name && i.owner_name !== "You");
      return named ? named.owner_name : "Another user";
    }

    function render(items, opts) {
      const groups = new Map();
      for (const item of items) {
        const id = opts.groupOf(item);
        if (id == null || String(id) === String(options.meId)) continue;
        if (!groups.has(id)) groups.set(id, []);
        groups.get(id).push(item);
      }
      for (const id of opts.extraOwners || []) if (!groups.has(id)) groups.set(id, []);

      host.replaceChildren();
      const sections = [...groups.entries()].map(([id, list]) => ({ id, list, label: labelOf(id, list) }));
      sections.sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase()));
      for (const { id, list, label } of sections) host.appendChild(buildSection(id, list, label, opts));
    }

    function buildSection(ownerId, list, label, opts) {
      const details = document.createElement("details");
      details.className = "collab-section";
      details.dataset.owner = ownerId;
      details.open = isOpen(ownerId);
      details.addEventListener("toggle", () => {
        open.set(ownerId, details.open);
        writeOpen(ownerId, details.open);
      });

      const summary = document.createElement("summary");
      const title = document.createElement("span");
      title.className = "collab-section-title";
      title.textContent = label;
      title.title = label;
      const count = document.createElement("span");
      count.className = "collab-section-count";
      count.textContent = String(list.length);
      summary.append(title, count);

      const formHost = document.createElement("div");
      formHost.className = "collab-section-form";
      formHost.hidden = true;

      if (opts.onAdd) {
        const add = document.createElement("button");
        add.type = "button";
        add.className = "btn collab-section-add";
        add.textContent = "+ Add";
        add.title = "Add to " + label;
        add.addEventListener("click", (e) => {
          e.preventDefault(); // a click on the button shouldn't fold the section
          e.stopPropagation();
          if (!formHost.hidden) { formHost.hidden = true; formHost.replaceChildren(); return; }
          expand(ownerId);
          details.open = true;
          formHost.hidden = false;
          opts.onAdd(ownerId, formHost, () => { formHost.hidden = true; formHost.replaceChildren(); });
        });
        summary.appendChild(add);
      }
      details.appendChild(summary);
      details.appendChild(formHost);

      const body = document.createElement("div");
      body.className = "allegation-list";
      for (const item of list) body.appendChild(opts.buildCard(item));
      if (!list.length) {
        const empty = document.createElement("p");
        empty.className = "empty";
        empty.textContent = "Nothing here yet.";
        body.appendChild(empty);
      }
      details.appendChild(body);
      return details;
    }

    return { load, render, expand };
  }

  return { create };
})();
