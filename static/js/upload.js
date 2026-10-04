document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("upload-form");
  if (!form) return;
  const input = form.querySelector('input[type="file"]');
  const errorBox = document.getElementById("upload-error");

  form.addEventListener("submit", (event) => {
    const file = input.files[0];
    const maxBytes = parseInt(input.dataset.maxBytes || "0", 10);
    if (file && maxBytes && file.size > maxBytes) {
      event.preventDefault();
      const maxMb = Math.round(maxBytes / (1024 * 1024));
      errorBox.textContent = `File exceeds the ${maxMb} MB limit.`;
      errorBox.classList.remove("hidden");
    } else if (errorBox) {
      errorBox.classList.add("hidden");
    }
  });
});
