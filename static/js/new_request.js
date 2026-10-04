document.addEventListener("DOMContentLoaded", () => {
  const textarea = document.getElementById("request-text");
  const charCount = document.getElementById("char-count");
  if (textarea && charCount) {
    const updateCount = () => { charCount.textContent = String(textarea.value.length); };
    textarea.addEventListener("input", updateCount);
    updateCount();
  }

  document.querySelectorAll(".quick-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!textarea) return;
      textarea.value = chip.dataset.fill || "";
      textarea.dispatchEvent(new Event("input"));
      textarea.focus();
    });
  });

  const form = document.getElementById("request-form");
  const submitBtn = document.getElementById("submit-btn");
  const panel = document.getElementById("pipeline-panel");
  const steps = panel ? panel.querySelectorAll(".lineage-step") : [];

  if (form && panel && steps.length) {
    form.addEventListener("submit", () => {
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Submitting…";
      }
      panel.hidden = false;
      let active = 0;
      steps[0].classList.add("is-active");
      const interval = setInterval(() => {
        if (active >= steps.length - 1) {
          clearInterval(interval);
          return;
        }
        steps[active].classList.remove("is-active");
        active += 1;
        steps[active].classList.add("is-active");
      }, 2200);
    });
  }
});
