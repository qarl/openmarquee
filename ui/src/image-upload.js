// Image upload: pick a file, preview-scale it to panel dimensions in the
// browser, upload the PNG bytes. The backend only ever sees pre-scaled
// bitmap data per SYSTEM_SPEC §5.1.

const TEMPLATE = `
    <section class="image-upload">
        <h2 class="image-upload-heading">Upload an image</h2>
        <div class="preview-wrap">
            <canvas class="image-upload-canvas" aria-label="image preview"></canvas>
        </div>
        <form class="controls" autocomplete="off">
            <label class="field">
                <span>Image file (JPG or PNG)</span>
                <input type="file" accept="image/jpeg,image/png" class="field-file">
            </label>
            <div class="row">
                <label class="field">
                    <span>Slide name</span>
                    <input type="text" class="field-name" value="Image" maxlength="200">
                </label>
                <label class="field field-duration-wrap">
                    <span>Duration (s)</span>
                    <input type="number" class="field-duration" value="5" min="1" max="300" step="1">
                </label>
            </div>
            <div class="row">
                <label class="field">
                    <span>Transition into next</span>
                    <select class="field-transition">
                        <option value="cut" selected>Cut (instant)</option>
                        <option value="fade">Fade</option>
                    </select>
                </label>
                <label class="field field-duration-wrap">
                    <span>Fade time (ms)</span>
                    <input type="number" class="field-transition-ms" value="500" min="0" max="5000" step="50">
                </label>
            </div>
            <button type="submit" class="primary field-save" disabled>Save image</button>
            <p class="image-upload-status" role="status" aria-live="polite"></p>
        </form>
    </section>
`;

/**
 * Mount the image-upload UI into `container`.
 *
 * @param {HTMLElement} container — parent element (emptied and replaced).
 * @param {object} options
 * @param {number} options.width — target sign width in pixels.
 * @param {number} options.height — target sign height in pixels.
 * @param {(payload: object) => Promise<any>} options.onSave
 */
export function mountImageUploader(container, { width, height, onSave }) {
    container.innerHTML = TEMPLATE;

    const canvas = container.querySelector(".image-upload-canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = `${width} / ${height}`;

    const fileEl = container.querySelector(".field-file");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const transitionEl = container.querySelector(".field-transition");
    const transitionMsEl = container.querySelector(".field-transition-ms");
    const form = container.querySelector(".controls");
    const saveBtn = container.querySelector(".field-save");
    const statusEl = container.querySelector(".image-upload-status");

    let hasImage = false;

    function updateSaveEnabled() {
        saveBtn.disabled = !hasImage || saveBtn.dataset.inFlight === "1";
    }

    fileEl.addEventListener("change", async () => {
        const file = fileEl.files?.[0];
        if (!file) {
            hasImage = false;
            clearCanvas();
            updateSaveEnabled();
            return;
        }
        try {
            await drawFileToCanvas(file, canvas);
            hasImage = true;
            statusEl.textContent = "";
            // Auto-name from the filename if the user hasn't touched the field.
            if (nameEl.value === "Image") {
                nameEl.value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
            }
        } catch (err) {
            hasImage = false;
            clearCanvas();
            statusEl.textContent = `Could not load image: ${err.message}`;
        } finally {
            updateSaveEnabled();
        }
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (saveBtn.disabled) return;

        saveBtn.dataset.inFlight = "1";
        updateSaveEnabled();
        statusEl.textContent = "Saving…";
        try {
            const png_base64 = canvasToBase64(canvas);
            const durationSeconds = Number(durationEl.value) || 5;
            const transitionMs = Number(transitionMsEl.value);
            await onSave({
                name: nameEl.value || "Image",
                duration_ms: Math.round(durationSeconds * 1000),
                transition: transitionEl.value,
                transition_ms: Number.isFinite(transitionMs) ? transitionMs : 500,
                png_base64,
            });
            statusEl.textContent = "Saved.";
            // Reset for the next upload.
            fileEl.value = "";
            clearCanvas();
            hasImage = false;
        } catch (err) {
            statusEl.textContent = `Save failed: ${err.message}`;
        } finally {
            delete saveBtn.dataset.inFlight;
            updateSaveEnabled();
        }
    });

    function clearCanvas() {
        const ctx = canvas.getContext("2d");
        ctx.save();
        try {
            ctx.fillStyle = "#000000";
            ctx.fillRect(0, 0, canvas.width, canvas.height);
        } finally {
            ctx.restore();
        }
    }

    clearCanvas();
}

/**
 * Read `file` as an Image, draw it onto `canvas` scaled to canvas dimensions.
 * Uses URL.createObjectURL for efficiency (no base64 round-trip).
 */
export function drawFileToCanvas(file, canvas) {
    return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => {
            try {
                const ctx = canvas.getContext("2d");
                ctx.save();
                try {
                    ctx.fillStyle = "#000000";
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    // Letterbox-fit so we don't distort aspect.
                    const scale = Math.min(
                        canvas.width / img.width,
                        canvas.height / img.height,
                    );
                    const drawW = img.width * scale;
                    const drawH = img.height * scale;
                    const drawX = (canvas.width - drawW) / 2;
                    const drawY = (canvas.height - drawH) / 2;
                    ctx.drawImage(img, drawX, drawY, drawW, drawH);
                } finally {
                    ctx.restore();
                }
                resolve();
            } finally {
                URL.revokeObjectURL(url);
            }
        };
        img.onerror = () => {
            URL.revokeObjectURL(url);
            reject(new Error("could not decode image"));
        };
        img.src = url;
    });
}

/** Strip the data URL prefix, returning just the base64 body. */
export function canvasToBase64(canvas) {
    const dataUrl = canvas.toDataURL("image/png");
    return dataUrl.split(",")[1];
}
