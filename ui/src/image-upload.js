// Image upload: pick a file, preview it in a panel-shaped canvas
// (cover-fit), upload the SOURCE bytes verbatim. The backend keeps
// the operator's full-resolution PNG/JPG and the playback engine
// scales to panel dims at slide entry — so a panel resize never
// degrades a stored asset.

import { attachAutoSave } from "./auto-save.js";
import { mountSlideBrowser, nextAutoName } from "./slide-browser.js";

const TEMPLATE = `
    <section class="image-upload">
        <div class="slide-browser-slot"></div>
        <form class="controls" autocomplete="off">
            <div class="om-card" style="margin-bottom: 12px;">
                <div class="om-row" style="gap: 10px;">
                    <label class="om-field" style="flex: 1;">
                        <span>Slide name</span>
                        <input type="text" class="om-input field-name" value="Image" maxlength="200">
                    </label>
                    <label class="om-field" style="width: 110px;">
                        <span>Duration (s)</span>
                        <input type="number" class="om-input field-duration" value="5" min="1" max="300" step="1">
                    </label>
                </div>
            </div>
            <div class="preview-wrap">
                <canvas class="image-upload-canvas" aria-label="image preview"></canvas>
            </div>
            <div class="om-card">
                <label class="om-field">
                    <span>Image file (JPG or PNG)</span>
                    <input type="file" accept="image/jpeg,image/png" class="om-input field-file">
                    <span class="image-upload-edit-hint" hidden style="margin-top: 4px; color: var(--om-text-dim); font-size: 12px;">
                        Editing an existing image — leave the file picker blank
                        to just update name / duration.
                    </span>
                </label>
            </div>
            <p class="om-save-status image-upload-status" role="status" aria-live="polite" data-state="idle"></p>
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
    const statusEl = container.querySelector(".image-upload-status");

    const state = {
        // The picked source file, kept around so auto-save can FileReader
        // it as base64. Cleared after the create-mode save promotes us
        // to edit mode (the bytes are then server-side).
        sourceFile: null,
        // `editingId` = non-null once an existing slide is loaded OR once
        // a fresh create-mode save returns an id. Subsequent auto-saves
        // are metadata-only PATCHes that omit image bytes.
        editingId: null,
    };

    async function performSave() {
        const durationSeconds = Number(durationEl.value) || 5;
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
            return;
        }
        if (!image_base64) {
            throw new Error("pick an image file first");
        }
        const created = await onSave(payload);
        // Promote to edit mode: subsequent auto-saves PATCH the same id
        // and don't need to re-upload bytes.
        if (created?.id) {
            state.editingId = String(created.id);
            state.sourceFile = null;
            editHintEl.hidden = false;
            if (browser) browser.highlight(state.editingId);
        }
    }

    const autoSave = attachAutoSave(form, {
        save: performSave,
        status: statusEl,
        canSave: () => Boolean(state.editingId || state.sourceFile),
    });

    fileEl.addEventListener("change", async () => {
        const file = fileEl.files?.[0];
        if (!file) {
            state.sourceFile = null;
            if (!state.editingId) clearCanvas();
            return;
        }
        try {
            // Preview is just visual feedback; the bytes we upload come
            // straight from the source file (FileReader at save time).
            await drawFileToCanvas(file, canvas);
            state.sourceFile = file;
            if (nameEl.value === "Image") {
                nameEl.value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
            }
            // The form-level `change` listener also schedules — kicking
            // here ensures we re-arm AFTER state.sourceFile is set so
            // the canSave gate flips true on the same tick.
            autoSave.kick();
        } catch (err) {
            state.sourceFile = null;
            clearCanvas();
            statusEl.textContent = `Could not load image: ${err.message}`;
            statusEl.dataset.state = "error";
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
        // Drop any pending auto-save — the form is being cleared,
        // not edited.
        autoSave.cancel();
        statusEl.textContent = "";
        statusEl.dataset.state = "idle";

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
            statusEl.dataset.state = "error";
        }
        // Loading an existing slide is not a user edit — drop any pending
        // auto-save scheduled by the field-value mutations above.
        autoSave.cancel();
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

    // Initial state: a blank create form. The editor surface is
    // dominantly used to upload NEW images, not to re-edit existing
    // ones (image bytes rarely change after upload — operators replace
    // a wrong file by deleting and re-uploading, not by editing in
    // place). Auto-opening the most-recent slide for edit caused a UX
    // surprise (QA 2026-04-26 explore-image-upload.md): an operator
    // who landed here to upload a new file picked one and silently
    // overwrote the auto-loaded slide via PUT. The slide-browser tile
    // click path (onSelect → loadForEdit) keeps the explicit
    // edit-existing flow available — it just isn't the default.
    (async () => {
        await resetToBlank();
    })();
    /**
     * +New flow: render a placeholder thumbnail (black canvas with the
     * auto-name as a label) and IMMEDIATELY persist it as a new image
     * slide so the operator sees a fresh tile in the pallet right away.
     * Then pop the file picker so their next click drops in the real
     * image — the existing change handler previews + auto-save PATCHes
     * the slide with the new bytes.
     */
    async function createNew() {
        await resetToBlank();
        drawPlaceholderToCanvas(canvas, nameEl.value || "New image");
        // Pre-check: don't auto-pop the file picker. Doing so before the
        // save creates a race (operator picks a file → file-change handler
        // fires create-mode save with real bytes → twin slides). Doing
        // it after breaks Safari's user-gesture chain. Operator clicks
        // Choose File as the explicit next step.
        const payload = {
            name: nameEl.value || "Image",
            duration_ms: 5000,
            image_base64: canvasToBase64(canvas),
        };
        try {
            const created = await onSave(payload);
            if (created?.id) {
                state.editingId = String(created.id);
                editHintEl.hidden = false;
                if (browser) {
                    await browser.refresh();
                    browser.highlight(state.editingId);
                }
            }
        } catch (err) {
            statusEl.textContent = `Could not create slide: ${err?.message || err}`;
            statusEl.dataset.state = "error";
        }
    }

    return {
        loadForEdit,
        reset: resetToBlank,
        createNew,
        refreshBrowser: () => browser?.refresh(),
        flushAutoSave: () => autoSave.flush(),
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
 * Render a placeholder thumbnail onto `canvas` — a black background
 * with the supplied label centered. Used by +New so a freshly-created
 * image/video slide has visible chrome in the pallet before the
 * operator drops in real bytes.
 */
export function drawPlaceholderToCanvas(canvas, label) {
    const ctx = canvas.getContext("2d");
    ctx.save();
    try {
        ctx.fillStyle = "#000000";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "#ffffff";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const fontSize = Math.max(8, Math.floor(canvas.height * 0.16));
        ctx.font = `${fontSize}px sans-serif`;
        ctx.fillText(
            String(label || "(empty)"),
            canvas.width / 2,
            canvas.height / 2,
        );
    } finally {
        ctx.restore();
    }
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
