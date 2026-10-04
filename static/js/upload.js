document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("upload-form");
  if (!form) return;
  const input = document.getElementById("upload-file-input");
  const errorBox = document.getElementById("upload-error");
  const dropzone = document.getElementById("upload-dropzone");
  const filenameLabel = document.getElementById("dropzone-filename");

  if (input && filenameLabel) {
    input.addEventListener("change", () => {
      if (input.files && input.files[0]) {
        filenameLabel.textContent = input.files[0].name;
      }
    });
  }

  if (dropzone) {
    dropzone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropzone.style.borderColor = "var(--color-secondary)";
    });
    dropzone.addEventListener("dragleave", () => {
      dropzone.style.borderColor = "";
    });
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.style.borderColor = "";
      if (input && e.dataTransfer.files && e.dataTransfer.files[0]) {
        input.files = e.dataTransfer.files;
        input.dispatchEvent(new Event("change"));
      }
    });
  }

  form.addEventListener("submit", (event) => {
    const file = input.files[0];
    const maxBytes = parseInt(input.dataset.maxBytes || "0", 10);
    if (file && maxBytes && file.size > maxBytes) {
      event.preventDefault();
      const maxMb = Math.round(maxBytes / (1024 * 1024));
      if (errorBox) {
        errorBox.textContent = `File exceeds the ${maxMb} MB limit.`;
        errorBox.classList.remove("hidden");
      }
    } else if (errorBox) {
      errorBox.classList.add("hidden");
    }
  });
});
