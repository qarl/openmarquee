// Image upload: pick a file, preview it in a panel-shaped canvas
// (cover-fit), upload the SOURCE bytes verbatim. The backend keeps
// the operator's full-resolution PNG/JPG and the playback engine
// scales to panel dims at slide entry — so a panel resize never
// degrades a stored asset.

import { mountSlideBrowser, nextAutoName } from "./slide-browser.js";

const TEMPLATE = `
    <section class="image-upload">
        <h2 class="subpage-title">Image Slides</h2>
        <div class="slide-browser-slot"></div>
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
    { width, height, onSave, onSaveExisting, fetchItems },
) {
    container.innerHTML = TEMPLATE;

    const canvas = container.querySelector(".image-upload-canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = `${width} / ${height}`;

    const editHintEl = container.querySelector(".image-upload-edit-hint");
    const fileEl = container.querySelector(".field-file");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const form = container.querySelector(".controls");
    const saveBtn = container.querySelector(".field-save");
    const statusEl = container.querySelector(".image-upload-status");

    const state = {
        // The picked source file, kept around so submit can FileReader
        // it as base64 — we no longer round-trip through Canvas, which
        // would have downsampled to panel dims and re-encoded as PNG.
        sourceFile: null,
        // `editingId` = non-null when the operator opened an existing slide
        // for edit; Save can skip sending image bytes when they never
        // repicked a file.
        editingId: null,
    };

    function updateSaveEnabled() {
        // In edit mode, metadata-only saves are valid (no new file needed).
        // In create mode, a freshly-picked source file is required.
        saveBtn.disabled =
            (!state.editingId && !state.sourceFile)
            || saveBtn.dataset.inFlight === "1";
    }

    fileEl.addEventListener("change", async () => {
        const file = fileEl.files?.[0];
        if (!file) {
            state.sourceFile = null;
            if (!state.editingId) clearCanvas();
            updateSaveEnabled();
            return;
        }
        try {
            // Preview is just visual feedback; the bytes we upload come
            // straight from the source file (FileReader on submit).
            await drawFileToCanvas(file, canvas);
            state.sourceFile = file;
            statusEl.textContent = "";
            if (nameEl.value === "Image") {
                nameEl.value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
            }
        } catch (err) {
            state.sourceFile = null;
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
            const durationSeconds = Number(durationEl.value) || 5;
            // Send the source file's bytes verbatim when we have one;
            // omit on metadata-only edits so the server keeps existing.
            const image_base64 = state.sourceFile
                ? await fileToBase64(state.sourceFile)
                : null;
            const payload = {
                name: nameEl.value || "Image",
                duration_ms: Math.round(durationSeconds * 1000),
                image_base64,
            };
            if (state.editingId && onSaveExisting) {
                await onSaveExisting(state.editingId, payload);
                statusEl.textContent = "Updated.";
            } else {
                // Create-mode requires image bytes; defensive guard.
                if (!image_base64) {
                    throw new Error("pick an image file first");
                }
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

    async function resetToBlank() {
        // Sync blank-state setup. Anything here can be safely
        // overridden by a loadForEdit that interleaves later.
        state.editingId = null;
        state.sourceFile = null;
        editHintEl.hidden = true;
        fileEl.value = "";
        durationEl.value = "5";
        clearCanvas();
        updateSaveEnabled();

        // Async tail: gap-filled default name + browser refresh.
        // Both are no-ops if loadForEdit grabbed editingId while
        // computeDefaultName was awaiting.
        const defaultName = await computeDefaultName();
        if (state.editingId !== null) return;
        nameEl.value = defaultName;
        if (browser) {
            await browser.refresh();
            browser.highlight(null);
        }
    }

    async function computeDefaultName() {
        if (!fetchItems) return "Image Slide 1";
        try {
            const items = await fetchItems();
            return nextAutoName(
                items.filter((i) => i.type === "image"),
                "Image Slide",
            );
        } catch {
            return "Image Slide 1";
        }
    }

    async function loadForEdit(slide) {
        if (!slide || slide.type !== "image") {
            statusEl.textContent =
                "Only image slides are editable here — text and video open their own editors.";
            return;
        }
        state.editingId = String(slide.id);
        state.sourceFile = null; // existing bytes are server-side; no new file picked
        editHintEl.hidden = false;
        if (browser) browser.highlight(slide.id);
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

    let browser = null;
    if (fetchItems) {
        browser = mountSlideBrowser(
            container.querySelector(".slide-browser-slot"),
            {
                type: "image",
                fetchItems,
                onSelect: (item) => loadForEdit(item),
                onCreate: () => resetToBlank(),
            },
        );
    }

    resetToBlank();
    return {
        loadForEdit,
        reset: resetToBlank,
        refreshBrowser: () => browser?.refresh(),
    };
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
                    // Cover-fit — matches the inline preview's behavior so
                    // what the operator sees in the editor IS what plays.
                    // The crop is intentional: the device can't render
                    // black bars and a stretched image looks worse than a
                    // tasteful center-crop.
                    const scale = Math.max(
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
 * Paint an image fetched from a URL onto `canvas`, cover-fit. Used when
 * opening an existing ImageSlide for edit — the stored PNG is already
 * at panel resolution so this is mostly a faithful draw, but the
 * cover-fit path handles weird historical resize cases too.
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
                    const scale = Math.max(
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

/**
 * Read `file` as a base64-encoded string (no data: prefix). Streams via
 * FileReader so a 30MB JPEG doesn't hold an extra ArrayBuffer in memory.
 */
export function fileToBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            const result = reader.result;
            if (typeof result !== "string") {
                reject(new Error("FileReader produced non-string result"));
                return;
            }
            const comma = result.indexOf(",");
            resolve(comma >= 0 ? result.slice(comma + 1) : result);
        };
        reader.onerror = () => reject(new Error("file read failed"));
        reader.readAsDataURL(file);
    });
}
