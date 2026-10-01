// Tab/Shift+Tab outside a list: type a tab character at the cursor (instead of
// moving focus out of the editor); Shift+Tab removes a tab just before it.
// Returns true when the key was handled.
export function handleTabCharacter(editor, e) {
  if (!e.shiftKey) {
    editor.chain().focus().insertContent("\t").run();
    return true;
  }
  const { from, empty } = editor.state.selection;
  if (empty && from > 1 && editor.state.doc.textBetween(from - 1, from) === "\t") {
    editor.chain().focus().deleteRange({ from: from - 1, to: from }).run();
  }
  return true;
}
