document.addEventListener("DOMContentLoaded", () => {
  const tabs = document.querySelectorAll("#status-filter-tabs .filter-tab");
  const searchInput = document.getElementById("request-search");
  const items = document.querySelectorAll("#requests-list .accordion-item");
  const noResults = document.getElementById("no-results-msg");
  if (!items.length) return;

  let activeStatus = "all";

  function applyFilter() {
    const query = (searchInput && searchInput.value ? searchInput.value : "").trim().toLowerCase();
    let visibleCount = 0;
    items.forEach((item) => {
      const matchesStatus = activeStatus === "all" || item.dataset.status === activeStatus;
      const matchesSearch = !query || (item.dataset.search || "").includes(query);
      const show = matchesStatus && matchesSearch;
      item.hidden = !show;
      if (show) visibleCount += 1;
    });
    if (noResults) noResults.hidden = visibleCount !== 0;
  }

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.toggle("active", t === tab));
      activeStatus = tab.dataset.filter;
      applyFilter();
    });
  });

  if (searchInput) {
    searchInput.addEventListener("input", applyFilter);
  }
});
