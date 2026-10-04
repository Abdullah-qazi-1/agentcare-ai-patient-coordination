document.addEventListener("DOMContentLoaded", () => {
  const chips = document.querySelectorAll("[data-dept-filter]");
  if (!chips.length) return;
  chips.forEach((chip) => {
    chip.addEventListener("click", () => {
      chips.forEach((c) => c.classList.toggle("active", c === chip));
      const dept = chip.dataset.deptFilter;
      document.querySelectorAll(".doctor-card").forEach((card) => {
        card.hidden = dept !== "all" && card.dataset.dept !== dept;
      });
    });
  });
});
