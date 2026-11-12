// Browser-side PDF helpers for "Create from images and PDF sections" (static/images-to-pdf.js).
// Everything happens in this tab's memory: PDFs are read from File objects, rendered to canvases for thumbnails,
// and the chosen pages are merged with pdf-lib. Nothing is written to disk or sent to the server.
import * as pdfjs from "pdfjs-dist/build/pdf.mjs";
import { PDFDocument, degrees } from "pdf-lib";

// Open a PDF file. Returns {pageCount, thumb(i, maxPx) -> canvas, destroy()} plus an internal bytes handle.
export async function openPdf(file, workerSrc) {
  if (workerSrc) pdfjs.GlobalWorkerOptions.workerSrc = workerSrc;
  const bytes = new Uint8Array(await file.arrayBuffer());
  // pdf.js transfers the buffer it is given to its worker, so give it a copy and keep ours for merging.
  const task = pdfjs.getDocument({ data: bytes.slice() });
  const doc = await task.promise;
  return {
    bytes,
    pageCount: doc.numPages,
    async thumb(i, maxPx) {
      const page = await doc.getPage(i + 1);
      const v1 = page.getViewport({ scale: 1 });
      const scale = maxPx / Math.max(v1.width, v1.height);
      const vp = page.getViewport({ scale });
      const c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(vp.width));
      c.height = Math.max(1, Math.round(vp.height));
      await page.render({ canvas: c, canvasContext: c.getContext("2d"), viewport: vp, background: "white" }).promise;
      page.cleanup();
      return c;
    },
    destroy() { return task.destroy(); },  // the loading task owns the worker; the document proxy has no destroy()
  };
}

// entries: [{pdf: <openPdf result>, page: index, rot: quarter turns} | {image: PDF bytes, page: 0, rot: 0}] in
// output order. Image entries refer to single-page PDFs built by the caller. Returns the merged PDF as a Blob.
export async function mergePages(entries) {
  const out = await PDFDocument.create();
  const loaded = new Map();  // bytes -> PDFDocument, so each source is parsed once
  for (const e of entries) {
    const bytes = e.pdf ? e.pdf.bytes : e.image;
    let src = loaded.get(bytes);
    if (!src) { src = await PDFDocument.load(bytes); loaded.set(bytes, src); }
    const [page] = await out.copyPages(src, [e.page]);
    if (e.rot) page.setRotation(degrees((page.getRotation().angle + e.rot * 90) % 360));
    out.addPage(page);
  }
  return new Blob([await out.save()], { type: "application/pdf" });
}
