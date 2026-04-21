// Image upload: pick a file, preview-scale it to panel dimensions in the
// browser, upload the PNG bytes. The backend only ever sees pre-scaled
// bitmap data per SYSTEM_SPEC §5.1.

const TEMPLATE = `
    <section class="image-upload">
        <div class="image-upload-header">
            <h2 class="image-upload-heading">Upload an image</h2>
            <button type="button" class="image-upload-new" hidden>+ New image</button>
        </div>
        <div class="preview-wrap">
            <canvas class="image-upload-canvas" aria-label="image preview"></canvas>
        </div>
        <form class="controls" autocomplete="off">
            <label class="field">
                <span>Image file (JPG or PNG)</span>
                <input type="file" accept="image/jpeg,image/png" class="field-file">
                <span class="field-hint image-upload-edit-hint" hidden>
                    Editing an existing image — leave the file picker blank
                    to just update name / duration.
                </span>
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
 * @param {(payload) => Promise<any>} options.onSave — called for NEW slides.
 * @param {(id, payload) => Promise<any>} [options.onSaveExisting] — called
 *     on Save when the uploader is in edit mode. Payload's png_base64 is
 *     included only when the operator re-picked a file; omit to leave
 *     existing bytes untouched.
 * @returns {{ loadForEdit: (slide) => Promise<void> }}
 */
export function mountImageUploader(
    container,
    { width, height, onSave, onSaveExisting },
) {
    container.innerHTML = TEMPLATE;

    const canvas = container.querySelector(".image-upload-canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = `${width} / ${height}`;

    const headingEl = container.querySelector(".image-upload-heading");
    const newBtnEl = container.querySelector(".image-upload-new");
    const editHintEl = container.querySelector(".image-upload-edit-hint");
    const fileEl = container.querySelector(".field-file");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const form = container.querySelector(".controls");
    const saveBtn = container.querySelector(".field-save");
    const statusEl = container.querySelector(".image-upload-status");

    const state = {
        // `hasImage` = canvas has a freshly-picked file's pixels drawn.
        // `editingId` = non-null when the operator opened an existing slide
        // for edit; Save goes to onSaveExisting(id, …) and can skip sending
        // png_base64 when they never repicked a file.
        hasImage: false,
        editingId: null,
    };

    function updateSaveEnabled() {
        // In edit mode, metadata-only saves are valid (no new file needed).
        // In create mode, a freshly-picked image is required.
        saveBtn.disabled =
            (!state.editingId && !state.hasImage)
            || saveBtn.dataset.inFlight === "1";
    }

    fileEl.addEventListener("change", async () => {
        const file = fileEl.files?.[0];
        if (!file) {
            state.hasImage = false;
            if (!state.editingId) clearCanvas();
            updateSaveEnabled();
            return;
        }
        try {
            await drawFileToCanvas(file, canvas);
            state.hasImage = true;
            statusEl.textContent = "";
            if (nameEl.value === "Image") {
                nameEl.value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
            }
        } catch (err) {
            state.hasImage = false;
            clearCanvas();
            statusEl.textContent = `Could not load image: ${err.message}`;
        } finally {
            updateSaveEnabled();
        }
    });

    newBtnEl.addEventListener("click", () => resetToBlank());

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (saveBtn.disabled) return;

        saveBtn.dataset.inFlight = "1";
        updateSaveEnabled();
        statusEl.textContent = "Saving…";
        try {
            const durationSeconds = Number(durationEl.value) || 5;
            // Send png_base64 only when a new file was picked, or in create
            // mode where it's always a fresh upload.
            const png_base64 =
                state.hasImage || !state.editingId
                    ? canvasToBase64(canvas)
                    : null;
            const payload = {
                name: nameEl.value || "Image",
                duration_ms: Math.round(durationSeconds * 1000),
                png_base64,
            };
            if (state.editingId && onSaveExisting) {
                await onSaveExisting(state.editingId, payload);
                statusEl.textContent = "Updated.";
            } else {
                // Create-mode requires png_base64; strip the null case by
                // calling with the canvas bytes.
                payload.png_base64 = canvasToBase64(canvas);
                await onSave(payload);
                statusEl.textContent = "Saved.";
            }
            resetToBlank();
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

    function resetToBlank() {
        state.editingId = null;
        state.hasImage = false;
        headingEl.textContent = "Upload an image";
        newBtnEl.hidden = true;
        editHintEl.hidden = true;
        fileEl.value = "";
        nameEl.value = "Image";
        durationEl.value = "5";
        clearCanvas();
        updateSaveEnabled();
    }

    async function loadForEdit(slide) {
        if (!slide || slide.type !== "image") {
            statusEl.textContent =
                "Only image slides are editable here — text and video open their own editors.";
            return;
        }
        state.editingId = String(slide.id);
        state.hasImage = false; // canvas has the existing bytes, not a new file
        headingEl.textContent = `Editing: ${slide.name || "Untitled"}`;
        newBtnEl.hidden = false;
        editHintEl.hidden = false;
        nameEl.value = slide.name || "Image";
        durationEl.value = String(
            Math.max(1, (slide.duration_ms || 5000) / 1000),
        );
        try {
            await drawUrlToCanvas(`/api/content/${slide.id}/asset`, canvas);
        } catch (err) {
            clearCanvas();
            statusEl.textContent = `Could not load image: ${err.message}`;
        }
        updateSaveEnabled();
    }

    clearCanvas();
    return { loadForEdit };
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

/**
 * Paint an image fetched from a URL onto `canvas`, letterbox-fit. Used
 * when opening an existing ImageSlide for edit — the stored PNG is
 * already at panel resolution so this is mostly a faithful draw, but
 * the letterbox path handles weird historical resize cases too.
 */
export function drawUrlToCanvas(url, canvas) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.onload = () => {
            try {
                const ctx = canvas.getContext("2d");
                ctx.save();
                try {
                    ctx.fillStyle = "#000000";
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    const scale = Math.min(
                        canvas.width / img.width,
                        canvas.height / img.height,
                    );
                    const drawW = img.width * scale;
                    const drawH = img.height * scale;
                    ctx.drawImage(
                        img,
                        (canvas.width - drawW) / 2,
                        (canvas.height - drawH) / 2,
                        drawW,
                        drawH,
                    );
                } finally {
                    ctx.restore();
                }
                resolve();
            } catch (err) {
                reject(err);
            }
        };
        img.onerror = () => reject(new Error("could not load image"));
        img.src = url;
    });
}

/** Strip the data URL prefix, returning just the base64 body. */
export function canvasToBase64(canvas) {
    const dataUrl = canvas.toDataURL("image/png");
    return dataUrl.split(",")[1];
}
