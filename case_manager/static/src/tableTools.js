// Tables for the report editor.
//
// Schema: table > tableRow+ > (tableCell | tableHeader)+ > block content. A
// cell may hold paragraphs, headings, lists, quotes, code, rules and images
// -- but never another table (the cell content expression below leaves
// `table` out), and a table is never allowed inside a list item (see
// TableNesting). Rows carry an optional pixel `height` (a minimum: taller
// content still grows the row), cells carry the stock `colwidth`.
//
// Interactions, beyond what prosemirror-tables gives for free (cell
// selection by dragging, Tab between cells, column-boundary dragging):
//   - TableRowResize: drag a row's bottom boundary to change its height.
//   - TableGrips: row/column/table-select grips outside the table's edge,
//     and a corner handle that scales the whole table.
// Pagination (pagination.js) treats each row as an unsplittable unit, so a
// row never straddles a page.
import { Extension } from "@tiptap/core";
import { Table, TableRow, TableCell, TableHeader } from "@tiptap/extension-table";
import { Plugin, PluginKey, TextSelection } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { CellSelection, TableMap } from "@tiptap/pm/tables";

export const TABLE_MAX_ROWS = 60;
export const TABLE_MAX_COLS = 12;
const MIN_COL_PX = 30;
const MIN_ROW_PX = 20;

const CELL_CONTENT = "(paragraph | heading | bulletList | orderedList | blockquote | codeBlock | horizontalRule | image)+";

export const ReportTable = Table.configure({ resizable: true, cellMinWidth: MIN_COL_PX });

export const ReportTableRow = TableRow.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      height: {
        default: null,
        parseHTML: (el) => parseInt(el.style.height, 10) || null,
        renderHTML: (attrs) => (attrs.height ? { style: `height: ${attrs.height}px` } : {}),
      },
    };
  },
});

export const ReportTableCell = TableCell.extend({ content: CELL_CONTENT });
export const ReportTableHeader = TableHeader.extend({ content: CELL_CONTENT });

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------
const isTableRole = (node, role) => node.type.spec.tableRole === role;

function tableAncestor($pos) {
  for (let d = $pos.depth; d > 0; d--) {
    const node = $pos.node(d);
    if (isTableRole(node, "table")) return { node, pos: $pos.before(d), start: $pos.start(d) };
  }
  return null;
}

export function selectionInTable(state) {
  return !!tableAncestor(state.selection.$from);
}

// A table goes at the top level only: not in another table or in a list.
export function canInsertTable(state) {
  const { $from } = state.selection;
  if ($from.depth < 1) return false;
  for (let d = $from.depth; d > 0; d--) {
    const name = $from.node(d).type.name;
    if (["table", "tableCell", "tableHeader", "listItem", "bulletList", "orderedList"].includes(name)) return false;
  }
  return true;
}

export function insertReportTable(editor, rows, cols) {
  const { state, view } = editor;
  if (!canInsertTable(state)) return false;
  rows = Math.max(1, Math.min(TABLE_MAX_ROWS, rows | 0));
  cols = Math.max(1, Math.min(TABLE_MAX_COLS, cols | 0));
  const { schema } = state;
  const colWidth = Math.max(MIN_COL_PX, Math.floor((view.dom.clientWidth - 1) / cols) - 1);
  const makeRow = () => {
    const cells = [];
    for (let c = 0; c < cols; c++) cells.push(schema.nodes.tableCell.createAndFill({ colwidth: [colWidth] }));
    return schema.nodes.tableRow.create(null, cells);
  };
  const table = schema.nodes.table.create(null, Array.from({ length: rows }, makeRow));

  const { $from } = state.selection;
  const block = $from.node(1);
  const emptyPara = block.type.name === "paragraph" && block.content.size === 0;
  const from = $from.before(1);
  const to = emptyPara ? $from.after(1) : from + block.nodeSize;
  const at = emptyPara ? from : to;
  const tr = state.tr;
  if (emptyPara) tr.replaceWith(from, to, table);
  else tr.insert(at, table);
  const after = at + table.nodeSize;
  if (!tr.doc.resolve(after).nodeAfter) tr.insert(after, schema.nodes.paragraph.create());
  tr.setSelection(TextSelection.near(tr.doc.resolve(at + 1)));
  view.dispatch(tr.scrollIntoView());
  view.focus();
  return true;
}

// ---------------------------------------------------------------------
// Keep tables out of lists (cells already can't hold tables by schema).
// Catches pasting a table into a list item, or wrapping one in a list.
// ---------------------------------------------------------------------
function tableInForbiddenSpot(doc, from, to) {
  let bad = false;
  doc.nodesBetween(from, to, (node, pos) => {
    if (bad) return false;
    if (!isTableRole(node, "table")) return true;
    const $p = doc.resolve(pos);
    for (let d = $p.depth; d >= 0; d--) {
      const name = $p.node(d).type.name;
      if (name === "listItem" || name === "tableCell" || name === "tableHeader") bad = true;
    }
    return false;
  });
  return bad;
}

export const TableNesting = Extension.create({
  name: "tableNesting",
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: new PluginKey("tableNesting"),
        filterTransaction(tr) {
          if (!tr.docChanged) return true;
          const size = tr.doc.content.size;
          let bad = false;
          tr.mapping.maps.forEach((map, i) => {
            if (bad) return;
            map.forEach((_os, _oe, ns, ne) => {
              const rest = tr.mapping.slice(i + 1);
              const from = Math.max(0, rest.map(ns, -1));
              const to = Math.min(size, rest.map(ne, 1));
              if (tableInForbiddenSpot(tr.doc, from, to)) bad = true;
            });
          });
          return !bad;
        },
      }),
    ];
  },
});

// ---------------------------------------------------------------------
// Row resizing: drag a row's bottom boundary (or the top edge of the next
// row). The live height is shown through a node decoration; the document
// is changed once, on release.
// ---------------------------------------------------------------------
const rowKey = new PluginKey("tableRowResize");

function realRow(tr) {
  return tr && !tr.classList.contains("page-break-row") ? tr : null;
}

function rowPosFromDom(view, trEl) {
  const $p = view.state.doc.resolve(view.posAtDOM(trEl, 0));
  for (let d = $p.depth; d > 0; d--) if ($p.node(d).type.name === "tableRow") return $p.before(d);
  return null;
}

function rowBoundaryHit(view, e) {
  if (!view.editable) return null;
  const cell = e.target && e.target.closest && e.target.closest("td, th");
  if (!cell || !view.dom.contains(cell)) return null;
  const r = cell.getBoundingClientRect();
  if (r.right - e.clientX <= 6) return null; // column-resize zone
  if (e.clientX - r.left <= 6 && cell.previousElementSibling) return null;
  if (e.clientY >= r.bottom - 4) return realRow(cell.parentElement);
  if (e.clientY <= r.top + 3) return realRow(cell.parentElement.previousElementSibling);
  return null;
}

export const TableRowResize = Extension.create({
  name: "tableRowResize",
  addProseMirrorPlugins() {
    let drag = null;
    return [
      new Plugin({
        key: rowKey,
        state: {
          init: () => null,
          apply(tr, value) {
            const meta = tr.getMeta(rowKey);
            if (meta !== undefined) return meta;
            return value && tr.docChanged ? { ...value, pos: tr.mapping.map(value.pos) } : value;
          },
        },
        props: {
          decorations(state) {
            const s = rowKey.getState(state);
            const node = s && state.doc.nodeAt(s.pos);
            if (!node || node.type.name !== "tableRow") return null;
            return DecorationSet.create(state.doc, [
              Decoration.node(s.pos, s.pos + node.nodeSize, { style: `height: ${s.height}px` }),
            ]);
          },
          handleDOMEvents: {
            mousemove(view, e) {
              if (!drag) view.dom.classList.toggle("row-resize-cursor", !!rowBoundaryHit(view, e));
              return false;
            },
            mouseleave(view) {
              view.dom.classList.remove("row-resize-cursor");
              return false;
            },
            mousedown(view, e) {
              const trEl = rowBoundaryHit(view, e);
              const pos = trEl && rowPosFromDom(view, trEl);
              if (pos == null) return false;
              e.preventDefault();
              const startH = trEl.getBoundingClientRect().height;
              drag = { height: startH };
              const move = (ev) => {
                drag.height = Math.max(MIN_ROW_PX, Math.round(startH + ev.clientY - e.clientY));
                view.dispatch(view.state.tr.setMeta(rowKey, { pos, height: drag.height }));
              };
              const up = () => {
                document.removeEventListener("mousemove", move);
                document.removeEventListener("mouseup", up);
                const node = view.state.doc.nodeAt(pos);
                const tr = view.state.tr.setMeta(rowKey, null);
                if (node && node.type.name === "tableRow") tr.setNodeMarkup(pos, undefined, { ...node.attrs, height: drag.height });
                drag = null;
                view.dispatch(tr);
              };
              document.addEventListener("mousemove", move);
              document.addEventListener("mouseup", up);
              return true;
            },
          },
        },
      }),
    ];
  },
});

// ---------------------------------------------------------------------
// Grips: select a row / column / the whole table, and scale the table.
// Drawn in an overlay layer inside the page wrapper (not in the document),
// for the table the cursor is in or the mouse is over.
// ---------------------------------------------------------------------
function describeTable(view, tableEl) {
  const rows = Array.from(tableEl.rows).filter((r) => realRow(r));
  if (!rows.length) return null;
  const rowPos = rowPosFromDom(view, rows[0]);
  if (rowPos == null) return null;
  const $r = view.state.doc.resolve(rowPos);
  const node = $r.parent;
  if (!isTableRole(node, "table")) return null;
  return { rows, node, pos: $r.before($r.depth), start: $r.start($r.depth), map: TableMap.get(node) };
}

function cellAt(view, info, row, col) {
  return view.state.doc.resolve(info.start + info.map.map[row * info.map.width + col]);
}

export const TableGrips = Extension.create({
  name: "tableGrips",
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: new PluginKey("tableGrips"),
        view(view) {
          const wrap = view.dom.closest(".editor-page-wrap") || view.dom.parentElement;
          const layer = document.createElement("div");
          layer.className = "table-grips";
          wrap.appendChild(layer);
          let hoverTable = null;
          let raf = null;
          let anchorRow = 0;
          let anchorCol = 0;

          const currentTable = () => {
            if (!view.editable) return null;
            const t = tableAncestor(view.state.selection.$from);
            if (t) {
              const dom = view.nodeDOM(t.pos);
              const el = dom && (dom.tagName === "TABLE" ? dom : dom.querySelector && dom.querySelector("table"));
              if (el) return el;
            }
            return hoverTable && hoverTable.isConnected ? hoverTable : null;
          };

          const addGrip = (kind, x, y, w, h, title, onDown) => {
            const g = document.createElement("div");
            g.className = `table-grip table-grip-${kind}`;
            g.title = title;
            g.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px`;
            g.addEventListener("mousedown", (e) => {
              e.preventDefault();
              onDown(e);
            });
            layer.appendChild(g);
          };

          const select = (sel) => {
            view.dispatch(view.state.tr.setSelection(sel));
            view.focus();
          };

          const render = () => {
            raf = null;
            layer.textContent = "";
            const tableEl = currentTable();
            const info = tableEl && describeTable(view, tableEl);
            if (!info) return;
            const w = wrap.getBoundingClientRect();
            const t = tableEl.getBoundingClientRect();
            const left = t.left - w.left;
            const top = t.top - w.top;
            const lastRow = info.map.height - 1;
            const lastCol = info.map.width - 1;

            addGrip("corner", left - 14, top - 14, 12, 12, "Click to select the table; drag to move it up or down", (e) => {
              startMove(e, info, () => select(new CellSelection(cellAt(view, info, 0, 0), cellAt(view, info, lastRow, lastCol))));
            });
            info.rows.forEach((trEl, i) => {
              const r = trEl.getBoundingClientRect();
              addGrip("row", left - 14, r.top - w.top, 12, r.height, "Select row (shift-click to extend)", (e) => {
                if (!e.shiftKey) anchorRow = i;
                select(CellSelection.rowSelection(cellAt(view, info, anchorRow, 0), cellAt(view, info, i, 0)));
              });
            });
            Array.from(info.rows[0].cells).forEach((c, j) => {
              const r = c.getBoundingClientRect();
              addGrip("col", r.left - w.left, top - 14, r.width, 12, "Select column (shift-click to extend)", (e) => {
                if (!e.shiftKey) anchorCol = j;
                select(CellSelection.colSelection(cellAt(view, info, 0, anchorCol), cellAt(view, info, 0, j)));
              });
            });
            addGrip("scale", left + t.width - 9, top + t.height - 9, 12, 12, "Drag to resize the whole table", (e) =>
              startScale(e, info, tableEl)
            );
          };
          const schedule = () => {
            if (!raf) raf = requestAnimationFrame(render);
          };

          // Drag the table to another place between top-level blocks. A press without movement just selects it.
          function startMove(e, info, onClick) {
            const w = wrap.getBoundingClientRect();
            const doc = view.state.doc;
            const targets = []; // {pos: doc position of a boundary between top-level blocks, y: its viewport y}
            let prevBottom = null;
            doc.forEach((child, offset, i) => {
              const dom = view.nodeDOM(offset);
              if (!(dom instanceof HTMLElement)) return;
              const r = dom.getBoundingClientRect();
              targets.push({ pos: offset, y: prevBottom === null ? r.top : (prevBottom + r.top) / 2 });
              prevBottom = r.bottom;
            });
            targets.push({ pos: doc.content.size, y: prevBottom });
            const size = info.node.nodeSize;
            const usable = targets.filter((t) => t.pos < info.pos || t.pos > info.pos + size);
            const line = document.createElement("div");
            line.className = "table-move-line";
            let moved = false;
            let best = null;
            const move = (ev) => {
              if (!moved && Math.hypot(ev.clientX - e.clientX, ev.clientY - e.clientY) < 4) return;
              moved = true;
              best = usable.reduce((a, t) => (!a || Math.abs(t.y - ev.clientY) < Math.abs(a.y - ev.clientY) ? t : a), null);
              if (!line.isConnected) layer.appendChild(line);
              if (best) line.style.cssText = `left:0;right:0;top:${best.y - w.top - 1}px`;
            };
            const up = () => {
              document.removeEventListener("mousemove", move);
              document.removeEventListener("mouseup", up);
              line.remove();
              if (!moved) return onClick();
              if (!best) return;
              const tr = view.state.tr;
              const node = info.node;
              tr.delete(info.pos, info.pos + size);
              const at = tr.mapping.map(best.pos);
              tr.insert(at, node);
              const after = at + size;
              if (!tr.doc.resolve(after).nodeAfter) tr.insert(after, view.state.schema.nodes.paragraph.create());
              tr.setSelection(TextSelection.near(tr.doc.resolve(at + 1))).scrollIntoView();
              view.dispatch(tr);
              view.focus();
            };
            document.addEventListener("mousemove", move);
            document.addEventListener("mouseup", up);
          }

          function startScale(e, info, tableEl) {
            const t0 = tableEl.getBoundingClientRect();
            const w = wrap.getBoundingClientRect();
            const colW = new Array(info.map.width).fill(0);
            let col = 0;
            Array.from(info.rows[0].cells).forEach((c) => {
              const span = c.colSpan || 1;
              for (let k = 0; k < span; k++) colW[col + k] = c.getBoundingClientRect().width / span;
              col += span;
            });
            const rowH = info.rows.map((r) => r.getBoundingClientRect().height);
            const maxW = view.dom.clientWidth;
            const ghost = document.createElement("div");
            ghost.className = "table-scale-ghost";
            layer.appendChild(ghost);
            let sx = 1;
            let sy = 1;
            const move = (ev) => {
              sx = Math.min(maxW / t0.width, Math.max((info.map.width * MIN_COL_PX) / t0.width, (t0.width + ev.clientX - e.clientX) / t0.width));
              sy = Math.max(0.2, (t0.height + ev.clientY - e.clientY) / t0.height);
              ghost.style.cssText = `left:${t0.left - w.left}px;top:${t0.top - w.top}px;width:${t0.width * sx}px;height:${t0.height * sy}px`;
            };
            const up = () => {
              document.removeEventListener("mousemove", move);
              document.removeEventListener("mouseup", up);
              ghost.remove();
              const newW = colW.map((x) => Math.max(MIN_COL_PX, Math.round(x * sx)));
              const tr = view.state.tr;
              const seen = new Set();
              for (let r = 0; r < info.map.height; r++) {
                for (let c = 0; c < info.map.width; c++) {
                  const off = info.map.map[r * info.map.width + c];
                  if (seen.has(off)) continue;
                  seen.add(off);
                  const cell = info.node.nodeAt(off);
                  tr.setNodeMarkup(info.start + off, undefined, { ...cell.attrs, colwidth: newW.slice(c, c + cell.attrs.colspan) });
                }
              }
              if (Math.abs(sy - 1) > 0.01) {
                info.node.forEach((row, off, i) => {
                  tr.setNodeMarkup(info.start + off, undefined, { ...row.attrs, height: Math.max(MIN_ROW_PX, Math.round(rowH[i] * sy)) });
                });
              }
              view.dispatch(tr);
            };
            document.addEventListener("mousemove", move);
            document.addEventListener("mouseup", up);
          }

          const onOver = (e) => {
            const t = e.target.closest && e.target.closest("table");
            if (t && view.dom.contains(t) && t !== hoverTable) {
              hoverTable = t;
              schedule();
            }
          };
          const onLeave = () => {
            hoverTable = null;
            schedule();
          };
          view.dom.addEventListener("mouseover", onOver);
          wrap.addEventListener("mouseleave", onLeave);
          window.addEventListener("resize", schedule);
          schedule();
          return {
            update: schedule,
            destroy() {
              view.dom.removeEventListener("mouseover", onOver);
              wrap.removeEventListener("mouseleave", onLeave);
              window.removeEventListener("resize", schedule);
              if (raf) cancelAnimationFrame(raf);
              layer.remove();
            },
          };
        },
      }),
    ];
  },
});

// Wires the toolbar's Table button/dropdown and the contextual row/column/table buttons (markup shared by
// reports.html and articles.html; ids: tableBtn, tableInsertDropdown, tableRowsInput, tableColsInput,
// tableInsertError, tableInsertConfirm, tableTools).
export function setupTableToolbar(editor, markDirty) {
  const tableBtn = document.getElementById("tableBtn");
  const dropdown = document.getElementById("tableInsertDropdown");
  const rowsInput = document.getElementById("tableRowsInput");
  const colsInput = document.getElementById("tableColsInput");
  const errorEl = document.getElementById("tableInsertError");
  const tools = document.getElementById("tableTools");
  rowsInput.max = TABLE_MAX_ROWS;
  colsInput.max = TABLE_MAX_COLS;

  const close = () => dropdown.classList.remove("open");
  tableBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    errorEl.textContent = "";
    dropdown.classList.toggle("open");
    if (dropdown.classList.contains("open")) rowsInput.select();
  });
  dropdown.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", close);
  function confirm() {
    const rows = parseInt(rowsInput.value, 10);
    const cols = parseInt(colsInput.value, 10);
    if (!(rows >= 1 && rows <= TABLE_MAX_ROWS && cols >= 1 && cols <= TABLE_MAX_COLS)) {
      errorEl.textContent = `Rows: 1\u2013${TABLE_MAX_ROWS}, columns: 1\u2013${TABLE_MAX_COLS}.`;
      return;
    }
    if (insertReportTable(editor, rows, cols)) {
      markDirty();
      close();
    }
  }
  document.getElementById("tableInsertConfirm").addEventListener("click", confirm);
  [rowsInput, colsInput].forEach((input) =>
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        confirm();
      } else if (e.key === "Escape") close();
    })
  );
  const commands = {
    addRowBefore: () => editor.chain().focus().addRowBefore().run(),
    addRowAfter: () => editor.chain().focus().addRowAfter().run(),
    addColumnBefore: () => editor.chain().focus().addColumnBefore().run(),
    addColumnAfter: () => editor.chain().focus().addColumnAfter().run(),
    deleteRow: () => editor.chain().focus().deleteRow().run(),
    deleteColumn: () => editor.chain().focus().deleteColumn().run(),
    deleteTable: () => editor.chain().focus().deleteTable().run(),
  };
  tools.querySelectorAll("button[data-table-cmd]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (commands[btn.dataset.tableCmd]()) markDirty();
    });
  });
  const sync = () => {
    tableBtn.disabled = !canInsertTable(editor.state);
    tools.hidden = !selectionInTable(editor.state);
  };
  editor.on("transaction", sync);
  sync();
}

// Tab / Shift+Tab inside a table: move between cells (Tab in the last cell adds a row). Returns true if handled.
export function handleTableTab(editor, forward) {
  if (!selectionInTable(editor.state)) return false;
  if (forward) {
    if (!editor.commands.goToNextCell()) editor.chain().addRowAfter().goToNextCell().run();
  } else editor.commands.goToPreviousCell();
  return true;
}

export const TableTools = [ReportTable, ReportTableRow, ReportTableCell, ReportTableHeader, TableNesting, TableRowResize, TableGrips];
