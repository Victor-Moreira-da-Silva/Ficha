const fileInput = document.getElementById("ocr-reference");
const wrapper = document.getElementById("ocr-canvas-wrapper");
const previewImage = document.getElementById("ocr-preview-image");
const selection = document.getElementById("ocr-selection");
const xInput = document.getElementById("x_percent");
const yInput = document.getElementById("y_percent");
const widthInput = document.getElementById("width_percent");
const heightInput = document.getElementById("height_percent");

let dragging = false;
let startX = 0;
let startY = 0;

function updateSelectionFromInputs() {
  if (!previewImage.src) return;
  selection.style.left = `${xInput.value}%`;
  selection.style.top = `${yInput.value}%`;
  selection.style.width = `${widthInput.value}%`;
  selection.style.height = `${heightInput.value}%`;
}

function setInputsFromSelection(left, top, width, height) {
  xInput.value = left.toFixed(2);
  yInput.value = top.toFixed(2);
  widthInput.value = width.toFixed(2);
  heightInput.value = height.toFixed(2);
  updateSelectionFromInputs();
}

if (fileInput) {
  fileInput.addEventListener("change", (event) => {
    const [file] = event.target.files;
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      previewImage.src = reader.result;
      wrapper.classList.add("has-image");
      updateSelectionFromInputs();
    };
    reader.readAsDataURL(file);
  });
}

[xInput, yInput, widthInput, heightInput].forEach((input) => {
  input?.addEventListener("input", updateSelectionFromInputs);
});

wrapper?.addEventListener("mousedown", (event) => {
  if (!previewImage.src) return;
  dragging = true;
  const rect = wrapper.getBoundingClientRect();
  startX = ((event.clientX - rect.left) / rect.width) * 100;
  startY = ((event.clientY - rect.top) / rect.height) * 100;
  setInputsFromSelection(startX, startY, 0, 0);
});

window.addEventListener("mousemove", (event) => {
  if (!dragging) return;
  const rect = wrapper.getBoundingClientRect();
  const currentX = ((event.clientX - rect.left) / rect.width) * 100;
  const currentY = ((event.clientY - rect.top) / rect.height) * 100;

  const left = Math.max(0, Math.min(startX, currentX));
  const top = Math.max(0, Math.min(startY, currentY));
  const width = Math.min(100 - left, Math.abs(currentX - startX));
  const height = Math.min(100 - top, Math.abs(currentY - startY));
  setInputsFromSelection(left, top, width, height);
});

window.addEventListener("mouseup", () => {
  dragging = false;
});

updateSelectionFromInputs();
