// The title bar's account dropdown (templates/_owner_picker.html): choosing whose account to work in reloads the page,
// which then lists that account's things.
(function () {
  const picker = document.getElementById("ownerPicker");
  if (!picker) return;
  let current = picker.value;
  picker.addEventListener("change", async () => {
    try {
      const res = await fetch(picker.dataset.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner_id: picker.value ? Number(picker.value) : null }),
      });
      if (!res.ok) throw new Error(res.statusText);
      current = picker.value;
      // A case or allegation in the address would belong to the account just left.
      const url = new URL(location.href);
      url.searchParams.delete("case");
      url.searchParams.delete("allegation");
      location.assign(url.toString());
    } catch (e) {
      console.error(e);
      picker.value = current;
    }
  });
})();
