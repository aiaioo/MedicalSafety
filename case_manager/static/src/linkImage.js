// The "Link" and "Image" toolbar buttons, shared by the report editor
// (editor.js) and the webpage editor (article-editor.js).

const SCHEME_RE = /^[a-z][a-z0-9+.-]*:/i;
const HOST_LABEL_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/i;
const EMAIL_RE = /^[^\s@/:]+@[^\s@/:]+\.[^\s@/:]+$/;

// Turns what the user typed into a usable link target, or returns null if
// it can't be one. A missing protocol becomes https://; only http(s) and
// mailto targets are accepted (what the server's sanitizer keeps). A bare
// host:port ("localhost:8080") has a colon but no scheme, so the scheme
// check looks for "//" (or mailto:) rather than just a colon.
export function normalizeLinkUrl(raw) {
  let text = String(raw || "").trim();
  if (!text || /\s/.test(text)) return null;
  if (/^mailto:/i.test(text)) {
    const address = text.slice(7).split("?")[0];
    return EMAIL_RE.test(address) ? text : null;
  }
  if (text.startsWith("//")) text = "https:" + text;
  else if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) text = "https://" + text;
  let url;
  try {
    url = new URL(text);
  } catch (e) {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  if (url.username || url.password) return null;
  const host = url.hostname.replace(/\.$/, "");
  const ipv4 = /^\d{1,3}(\.\d{1,3}){3}$/.test(host);
  const labels = host.split(".");
  const plausibleHost = host === "localhost" || ipv4
    || (labels.length >= 2 && labels.every((l) => HOST_LABEL_RE.test(l)) && /^[a-z]{2,}$/i.test(labels[labels.length - 1]));
  return plausibleHost ? text : null;
}

// Wires #linkBtn, #insertImageBtn and #imageFileInput to `editor`.
//   imageUploadUrl   POST target taking an "image" file, answering {url}
//   canEdit          false makes both buttons inert (read-only viewers)
//   onChange()       called after the document changed
//   onImageUploaded  optional, called after an image was inserted
export function setupLinkAndImage({ editor, imageUploadUrl, canEdit = true, setStatus, onChange, onImageUploaded }) {
  document.getElementById("linkBtn").addEventListener("click", () => {
    if (!canEdit) return;
    let value = editor.getAttributes("link").href || "";
    for (;;) {
      const answer = window.prompt("Link URL (leave blank to remove the link):", value);
      if (answer === null) return;
      if (!answer.trim()) {
        editor.chain().focus().unsetLink().run();
        break;
      }
      const href = normalizeLinkUrl(answer);
      if (href) {
        editor.chain().focus().extendMarkRange("link").setLink({ href }).run();
        break;
      }
      window.alert("That doesn't look like a valid web address (or email address). Please check it and try again.");
      value = answer;
    }
    onChange();
  });

  const imageFileInput = document.getElementById("imageFileInput");
  document.getElementById("insertImageBtn").addEventListener("click", () => {
    if (canEdit) imageFileInput.click();
  });
  imageFileInput.addEventListener("change", async () => {
    const file = imageFileInput.files[0];
    imageFileInput.value = "";
    if (!file || !canEdit) return;
    setStatus("Uploading image…");
    try {
      const body = new FormData();
      body.append("image", file);
      const res = await fetch(imageUploadUrl, { method: "POST", body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Upload failed");
      editor.chain().focus().setImage({ src: data.url, alt: file.name }).run();
      setStatus("");
      if (onImageUploaded) await onImageUploaded();
    } catch (e) {
      setStatus("Image upload failed: " + e.message, true);
    }
    onChange();
  });
}
