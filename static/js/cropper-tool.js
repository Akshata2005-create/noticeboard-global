/* ==========================================================================
   Shared manual photo cropper (Cropper.js) for the admin portal.
   Wires a <input type="file"> to a thumbnail strip where each photo can be
   dragged/zoomed and cropped freeform before the form is submitted. Cropped
   results replace the original files in the input via the DataTransfer API,
   so the normal multipart form submit keeps working unchanged.
   ========================================================================== */

(function () {
  let cropper = null;
  let activeCallback = null;

  const modal = document.getElementById("crop-modal");
  const modalImg = document.getElementById("crop-target-img");
  const applyBtn = document.getElementById("crop-apply");
  const cancelBtn = document.getElementById("crop-cancel");
  const resetBtn = document.getElementById("crop-reset");
  const zoomInBtn = document.getElementById("crop-zoom-in");
  const zoomOutBtn = document.getElementById("crop-zoom-out");

  if (!modal) return; // page doesn't include the crop modal — nothing to wire

  function openCropper(file, onApply) {
    const url = URL.createObjectURL(file);
    modal.style.display = "flex";
    modalImg.src = url;
    activeCallback = onApply;

    modalImg.onload = () => {
      if (cropper) cropper.destroy();
      cropper = new Cropper(modalImg, {
        viewMode: 1,
        dragMode: "move",
        autoCropArea: 0.95,
        background: false,
        aspectRatio: NaN, // freeform
        responsive: true,
        guides: true,
        center: true,
      });
    };
  }

  function closeModal() {
    modal.style.display = "none";
    if (cropper) {
      cropper.destroy();
      cropper = null;
    }
    activeCallback = null;
  }

  applyBtn.addEventListener("click", () => {
    if (!cropper) return;
    cropper.getCroppedCanvas({ imageSmoothingQuality: "high" }).toBlob((blob) => {
      if (activeCallback) activeCallback(blob);
      closeModal();
    }, "image/jpeg", 0.92);
  });
  cancelBtn.addEventListener("click", closeModal);
  resetBtn.addEventListener("click", () => cropper && cropper.reset());
  zoomInBtn.addEventListener("click", () => cropper && cropper.zoom(0.1));
  zoomOutBtn.addEventListener("click", () => cropper && cropper.zoom(-0.1));

  /**
   * Wire a file input so every selected photo gets a thumbnail + "Crop" button.
   * Cropped results replace the matching file in the input's FileList.
   * @param {string} inputId    id of the <input type="file">
   * @param {string} previewId  id of an empty container to render thumbnails into
   */
  window.wireCropField = function (inputId, previewId) {
    const input = document.getElementById(inputId);
    const preview = document.getElementById(previewId);
    if (!input || !preview) return;

    let fileStore = [];

    input.addEventListener("change", () => {
      fileStore = Array.from(input.files);
      renderPreview();
    });

    function renderPreview() {
      preview.innerHTML = "";
      fileStore.forEach((file, idx) => {
        const url = URL.createObjectURL(file);
        const item = document.createElement("div");
        item.className = "crop-thumb";

        const thumbImg = document.createElement("img");
        thumbImg.src = url;
        item.appendChild(thumbImg);

        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-ghost btn-sm crop-thumb-btn";
        btn.textContent = "✂ Crop";
        btn.addEventListener("click", () => {
          openCropper(file, (blob) => {
            const baseName = file.name.replace(/\.[^.]+$/, "");
            const newFile = new File([blob], `${baseName}_cropped.jpg`, { type: "image/jpeg" });
            fileStore[idx] = newFile;
            syncInput();
            renderPreview();
          });
        });
        item.appendChild(btn);

        preview.appendChild(item);
      });
    }

    function syncInput() {
      const dt = new DataTransfer();
      fileStore.forEach((f) => dt.items.add(f));
      input.files = dt.files;
    }
  };
})();
