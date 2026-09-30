// One-click colour palette for page numbers, shared by the annexure sidebar
// and the report Page setup dialog. PageNumberPalette.mount(el, onPick)
// fills `el` with swatch buttons and returns select(color) to mark the current one.
window.PageNumberPalette = (function () {
  function hslToHex(h, s, l) {
    s /= 100; l /= 100;
    const f = (n) => {
      const k = (n + h / 30) % 12;
      const c = l - s * Math.min(l, 1 - l) * Math.max(-1, Math.min(k - 3, 9 - k, 1));
      return Math.round(c * 255).toString(16).padStart(2, "0");
    };
    return "#" + f(0) + f(8) + f(4);
  }

  // A row of greys plus brown and navy, then eight hues at strong and muted
  // saturations and a few lightnesses.
  const PALETTE = [
    "#000000", "#333333", "#555555", "#777777", "#999999", "#bbbbbb", "#8b5a2b", "#1f3a93",
    ...[[85, 30], [85, 45], [85, 65], [40, 40], [40, 65]].flatMap(([sat, light]) =>
      [0, 30, 50, 130, 175, 210, 270, 320].map((hue) => hslToHex(hue, sat, light))),
  ];

  function mount(el, onPick) {
    const select = (color) => el.querySelectorAll(".color-swatch").forEach((b) => {
      const on = b.dataset.color === color;
      b.classList.toggle("selected", on);
      b.setAttribute("aria-checked", on);
    });
    PALETTE.forEach((c) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "color-swatch";
      b.dataset.color = c;
      b.style.background = c;
      b.title = c;
      b.setAttribute("role", "radio");
      b.addEventListener("click", () => onPick(c));
      el.appendChild(b);
    });
    return select;
  }

  return { mount };
})();
