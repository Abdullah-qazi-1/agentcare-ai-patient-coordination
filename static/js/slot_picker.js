document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-doctor-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.doctorToggle;
      const panel = document.getElementById(`slot-panel-${id}`);
      if (!panel) return;
      const willOpen = panel.hidden;
      document.querySelectorAll(".slot-panel").forEach((p) => {
        p.hidden = true;
      });
      panel.hidden = !willOpen;
    });
  });

  document.querySelectorAll(".slot-form").forEach((form) => {
    const slotIdField = form.querySelector(".slot-form-slot-id");
    const confirmBox = form.querySelector(".slot-confirm");
    form.querySelectorAll(".slot-btn").forEach((slotBtn) => {
      slotBtn.addEventListener("click", () => {
        form.querySelectorAll(".slot-btn").forEach((b) => b.classList.remove("selected"));
        slotBtn.classList.add("selected");
        if (slotIdField) slotIdField.value = slotBtn.dataset.slotId;
        if (confirmBox) confirmBox.classList.remove("hidden");
      });
    });
  });
});
