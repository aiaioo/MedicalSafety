(function () {
  const picker = document.getElementById("causePicker");
  if (!picker) return;
  let current = picker.value;
  picker.addEventListener("change", async () => {
    try {
      const res = await fetch(picker.dataset.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cause_id: picker.value }),
      });
      if (!res.ok) throw new Error(await res.text());
      current = picker.value;
      if (picker.dataset.reload === "1") location.reload();
    } catch (e) {
      console.error(e);
      picker.value = current;
    }
  });
})();
