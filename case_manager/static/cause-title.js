// Builds a case's cause title (a ProseMirror doc) from its cause title
// template, filling in the case's court, number and parties. Shared by the
// case details page and the report editor's "Insert cause title" button.
(function () {
  function partiesText(data, side) {
    const names = data.parties.filter((p) => p.side === side && p.name.trim()).map((p) => p.name.trim());
    if (data.cause_title_one_line_parties) return names.length > 1 ? `${names[0]} and Ors.` : names.join("");
    return names.length > 1 ? names.map((n, i) => `${i + 1}. ${n}`).join("\n") : names.join("");
  }

  function currentTemplate(data, templates) {
    return templates.find((t) => t.id === data.cause_title_template_id) || templates[0] || null;
  }

  // Template bodies are ProseMirror docs (JSON strings) from the admin page's
  // rich-text editor; older ones are plain text, one paragraph per line.
  function templateDoc(body) {
    if (typeof body === "string" && body.trimStart().startsWith("{")) {
      try {
        const doc = JSON.parse(body);
        if (doc && doc.type === "doc") return doc;
      } catch (e) { /* plain text that merely starts with a brace */ }
    }
    return { type: "doc", content: String(body).split(/\r?\n/).map((line) => ({ type: "paragraph", content: line ? [{ type: "text", text: line }] : [] })) };
  }

  function placeholderValues(data) {
    return {
      COURT_NAME: data.court.toUpperCase() || "[COURT_NAME]",
      COURT_LOCATION: data.court_location.toUpperCase() || "[COURT_LOCATION]",
      CASE_NUMBER: data.case_number.toUpperCase() || "[CASE_NUMBER]",
      PLAINTIFFS: partiesText(data, "complainant") || "[PLAINTIFFS]",
      RESPONDENTS: partiesText(data, "respondent") || "[RESPONDENTS]",
    };
  }

  // One pass over the text, so a party name that happens to contain a
  // placeholder is never substituted a second time. A value with line breaks
  // (the party lists) becomes text + hardBreak nodes, keeping the marks.
  function substituteNode(node, values) {
    const text = node.text.replace(/\[(COURT_NAME|COURT_LOCATION|CASE_NUMBER|PLAINTIFFS|RESPONDENTS)\]/g, (_, k) => values[k]);
    const out = [];
    text.split("\n").forEach((line, i) => {
      if (i) out.push({ type: "hardBreak" });
      if (line) out.push({ ...node, text: line });
    });
    return out;
  }

  // The cause title as a ProseMirror doc: the chosen template with the
  // placeholders filled in.
  function generateCauseDoc(data, templates) {
    const template = currentTemplate(data, templates);
    if (!template) return { type: "doc", content: [{ type: "paragraph" }] };
    const values = placeholderValues(data);
    const doc = templateDoc(template.body);
    return {
      type: "doc",
      content: (doc.content || []).map((block) => {
        const content = (block.content || []).flatMap((n) => (n.type === "text" ? substituteNode(n, values) : [n]));
        return { ...block, content };
      }),
    };
  }

  // True when the case has something a generated title would show.
  function hasCauseTitle(data, templates) {
    if (data.cause_title_doc) return true;
    const filled = data.court || data.case_number || data.court_location || (data.parties || []).some((p) => p.name.trim());
    return !!(filled && templates.length);
  }

  window.CauseTitle = {
    // The saved hand-edited title, else one generated from the template.
    doc: (data, templates) => data.cause_title_doc || generateCauseDoc(data, templates),
    generate: generateCauseDoc,
    currentTemplate,
    exists: hasCauseTitle,
  };
})();
