document.addEventListener("DOMContentLoaded", () => {
  const chips = document.querySelectorAll("[data-dept-filter]");
  const searchInput = document.getElementById("doctor-search");
  const cards = document.querySelectorAll("#doctor-grid .doctor-card");
  const noResults = document.getElementById("no-doctor-results");
  if (!cards.length) return;

  let activeDept = "all";

  function applyFilter() {
    const query = (searchInput && searchInput.value ? searchInput.value : "").trim().toLowerCase();
    let visibleCount = 0;
    cards.forEach((card) => {
      const matchesDept = activeDept === "all" || card.dataset.dept === activeDept;
      const name = (card.querySelector(".doctor-name") ? card.querySelector(".doctor-name").textContent : "").toLowerCase();
      const matchesSearch = !query || name.includes(query);
      const show = matchesDept && matchesSearch;
      card.hidden = !show;
      if (show) visibleCount += 1;
    });
    if (noResults) noResults.hidden = visibleCount !== 0;
  }

  chips.forEach((chip) => {
    chip.addEventListener("click", () => {
      chips.forEach((c) => c.classList.toggle("active", c === chip));
      activeDept = chip.dataset.deptFilter;
      applyFilter();
    });
  });

  if (searchInput) {
    searchInput.addEventListener("input", applyFilter);
  }
});
