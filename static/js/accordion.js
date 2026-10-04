document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("details.accordion-item").forEach((details) => {
    details.addEventListener("toggle", () => {
      if (!details.open) return;
      const slot = details.querySelector(".lazy-slot");
      if (!slot || slot.dataset.loaded === "1") return;
      const url = slot.dataset.lazyUrl;
      if (!url) return;
      fetch(url)
        .then((r) => (r.ok ? r.text() : Promise.reject()))
        .then((html) => {
          slot.innerHTML = html;
          slot.dataset.loaded = "1";
        })
        .catch(() => {
          slot.innerHTML = '<p class="muted">Couldn\'t load details.</p>';
        });
    });
  });
});
