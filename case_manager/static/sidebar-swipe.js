// On narrow screens the annotations/snippets sidebar (see .sidebar in
// style.css) docks off-screen and slides in over the document/source view
// instead of sharing the row with it. Swipe left from the right edge to
// open it, swipe right anywhere inside it to close it. Wide screens are
// untouched -- the media query in style.css only applies the slide-in
// layout below 780px, so this never fights the normal flex sidebar there.
(function () {
  const layout = document.querySelector(".viewer-layout");
  const sidebar = layout && layout.querySelector(".sidebar");
  if (!layout || !sidebar) return;

  const MOBILE_QUERY = window.matchMedia("(max-width: 780px)");
  const EDGE_ZONE = 24; // px from the right screen edge that can start an "open" swipe
  const SWIPE_THRESHOLD = 60; // px of horizontal travel needed to trigger a toggle

  let startX = null;
  let startY = null;
  let mode = null; // "open" | "close" | null

  function isOpen() {
    return sidebar.classList.contains("is-open");
  }

  function onTouchStart(e) {
    if (!MOBILE_QUERY.matches || e.touches.length !== 1) {
      mode = null;
      return;
    }
    const touch = e.touches[0];
    const withinSidebar = sidebar.contains(e.target);
    if (isOpen() && withinSidebar) {
      mode = "close";
    } else if (!isOpen() && !withinSidebar && touch.clientX >= window.innerWidth - EDGE_ZONE) {
      mode = "open";
    } else {
      mode = null;
      return;
    }
    startX = touch.clientX;
    startY = touch.clientY;
  }

  function onTouchEnd(e) {
    if (!mode) return;
    const touch = e.changedTouches[0];
    const dx = touch.clientX - startX;
    const dy = touch.clientY - startY;
    if (Math.abs(dx) >= SWIPE_THRESHOLD && Math.abs(dx) > Math.abs(dy) * 1.5) {
      if (mode === "open" && dx < 0) sidebar.classList.add("is-open");
      else if (mode === "close" && dx > 0) sidebar.classList.remove("is-open");
    }
    mode = null;
  }

  document.addEventListener("touchstart", onTouchStart, { passive: true });
  document.addEventListener("touchend", onTouchEnd, { passive: true });

  MOBILE_QUERY.addEventListener("change", (e) => {
    if (!e.matches) sidebar.classList.remove("is-open");
  });
})();
