// Video upload: pick an MP4, extract a thumbnail (first visible frame),
// preview it, upload both the video bytes and the thumbnail.
//
// Scope note: this does NOT yet run a client-side transcode. The user
// uploads the file as-is; if it's H.264 + a resolution the Pi Zero 2 W's
// hardware decoder can handle, playback will be smooth on HDMI. The
// ffmpeg.wasm pipelines (decode → scale → re-encode for HDMI, OR
// decode → scale → raw RGB frames for HUB75/WS2812B/composite) land
// alongside the real renderers in a follow-up. Until then this module
// is a *direct-passthrough uploader* plus a thumbnail extractor.

const TEMPLATE = `
    <section class="video-upload">
        <div class="video-upload-header">
            <h2 class="video-upload-heading">Upload a video</h2>
            <button type="button" class="video-upload-new" hidden>+ New video</button>
        </div>
        <div class="preview-wrap">
            <canvas class="video-upload-canvas" aria-label="thumbnail preview"></canvas>
        </div>
        <form class="controls" autocomplete="off">
            <label class="field">
                <span>Video file (MP4)</span>
                <input type="file" accept="video/mp4" class="field-file">
                <span class="field-hint video-upload-edit-hint" hidden>
                    Editing an existing video — leave the file picker blank
                    to just update name / duration.
                </span>
            </label>
            <p class="field-hint video-upload-hint">
                Client-side transcoding via ffmpeg.wasm isn't wired into
                this uploader yet — today you upload what you've got. For
                smooth HDMI playback on Pi Zero 2 W, pre-encode as H.264
                at your target resolution, or open the
                <a href="/spike.html" target="_blank">ffmpeg.wasm spike page</a>
                to transcode in the browser and download the output.
            </p>
            <div class="row">
                <label class="field">
                    <span>Slide name</span>
                    <input type="text" class="field-name" value="Video" maxlength="200">
                </label>
                <label class="field field-duration-wrap">
                    <span>Duration (s)</span>
                    <input type="number" class="field-duration" value="10" min="1" max="3600" step="1">
                </label>
            </div>
            <label class="field">
                <span>Pipeline</span>
                <select class="field-pipeline">
                    <option value="h264_mp4" selected>H.264 MP4 (HDMI)</option>
                    <option value="raw_frames">Raw frames (HUB75/WS2812B/composite) — spike only</option>
                </select>
            </label>
            <button type="submit" class="primary field-save" disabled>Save video</button>
            <p class="video-upload-status" role="status" aria-live="polite"></p>
        </form>
    </section>
`;

/**
 * Mount the video-upload UI into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {number} options.width  — sign width
 * @param {number} options.height — sign height
 * @param {(payload) => Promise<any>} options.onSave — called with
 *     { name, duration_ms, pipeline, png_base64, mp4_base64 } for
 *     new-slide creation.
 * @param {(id, payload) => Promise<any>} [options.onSaveExisting] —
 *     called on edit. Payload's asset bodies are included only when
 *     the operator re-picked a file.
 * @returns {{ loadForEdit: (slide) => Promise<void> }}
 */
export function mountVideoUploader(
    container,
    { width, height, onSave, onSaveExisting },
) {
    container.innerHTML = TEMPLATE;

    const canvas = container.querySelector(".video-upload-canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = `${width} / ${height}`;

    const headingEl = container.querySelector(".video-upload-heading");
    const newBtnEl = container.querySelector(".video-upload-new");
    const editHintEl = container.querySelector(".video-upload-edit-hint");
    const fileEl = container.querySelector(".field-file");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const pipelineEl = container.querySelector(".field-pipeline");
    const saveBtn = container.querySelector(".field-save");
    const statusEl = container.querySelector(".video-upload-status");
    const form = container.querySelector(".controls");

    const state = {
        // Populated only when the operator picks a NEW file — the asset
        // bodies below are null in edit mode when they leave the picker
        // empty. Save omits the fields so the server retains existing bytes.
        videoBytesBase64: null,
        thumbnailCanvasReady: false, // canvas has a fresh first-frame
        editingId: null,
    };

    function updateSaveEnabled() {
        // In create mode we need both a thumbnail + MP4 bytes. In edit
        // mode, metadata-only saves are valid.
        const hasNewFile = state.thumbnailCanvasReady && state.videoBytesBase64;
        saveBtn.disabled =
            (!state.editingId && !hasNewFile)
            || saveBtn.dataset.inFlight === "1";
    }

    clearCanvas(canvas);

    fileEl.addEventListener("change", async () => {
        const file = fileEl.files?.[0];
        if (!file) {
            state.thumbnailCanvasReady = false;
            state.videoBytesBase64 = null;
            if (!state.editingId) clearCanvas(canvas);
            updateSaveEnabled();
            return;
        }

        statusEl.textContent = "Reading file…";
        try {
            const [{ durationSeconds }, bytesB64] = await Promise.all([
                drawFirstFrameToCanvas(file, canvas),
                fileToBase64(file),
            ]);
            state.thumbnailCanvasReady = true;
            state.videoBytesBase64 = bytesB64;
            if (Number.isFinite(durationSeconds) && durationSeconds > 0) {
                durationEl.value = String(Math.round(durationSeconds));
            }
            if (nameEl.value === "Video") {
                nameEl.value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
            }
            statusEl.textContent = "";
        } catch (err) {
            state.thumbnailCanvasReady = false;
            state.videoBytesBase64 = null;
            clearCanvas(canvas);
            statusEl.textContent = `Could not read video: ${err.message}`;
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
            const durationSeconds = Number(durationEl.value) || 10;
            const includeAssets =
                state.thumbnailCanvasReady || !state.editingId;
            const payload = {
                name: nameEl.value || "Video",
                duration_ms: Math.round(durationSeconds * 1000),
                pipeline: pipelineEl.value,
                png_base64: includeAssets ? canvasToBase64(canvas) : null,
                mp4_base64: includeAssets ? state.videoBytesBase64 : null,
            };
            if (state.editingId && onSaveExisting) {
                await onSaveExisting(state.editingId, payload);
                statusEl.textContent = "Updated.";
            } else {
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

    function resetToBlank() {
        state.editingId = null;
        state.thumbnailCanvasReady = false;
        state.videoBytesBase64 = null;
        headingEl.textContent = "Upload a video";
        newBtnEl.hidden = true;
        editHintEl.hidden = true;
        fileEl.value = "";
        nameEl.value = "Video";
        durationEl.value = "10";
        clearCanvas(canvas);
        updateSaveEnabled();
    }

    async function loadForEdit(slide) {
        if (!slide || slide.type !== "video") {
            statusEl.textContent =
                "Only video slides are editable here — text and image open their own editors.";
            return;
        }
        state.editingId = String(slide.id);
        state.thumbnailCanvasReady = false;
        state.videoBytesBase64 = null;
        headingEl.textContent = `Editing: ${slide.name || "Untitled"}`;
        newBtnEl.hidden = false;
        editHintEl.hidden = false;
        nameEl.value = slide.name || "Video";
        durationEl.value = String(
            Math.max(1, (slide.duration_ms || 10000) / 1000),
        );
        pipelineEl.value = slide.pipeline || "h264_mp4";
        // Paint the stored thumbnail into the canvas for visual continuity.
        try {
            await drawUrlToCanvas(`/api/content/${slide.id}/asset`, canvas);
        } catch (err) {
            statusEl.textContent = `Could not load thumbnail: ${err.message}`;
        }
        updateSaveEnabled();
    }

    return { loadForEdit };
}

// Duplicated in image-upload.js — deliberately, so neither uploader
// imports the other. Small helper, not worth a shared module.
function drawUrlToCanvas(url, canvas) {
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
        img.onerror = () => reject(new Error("could not load thumbnail"));
        img.src = url;
    });
}

/**
 * Load `file` into an offscreen <video>, seek to the first visible frame,
 * paint it onto `canvas` (letterbox-fit to canvas dimensions), and resolve
 * with the detected duration.
 */
export function drawFirstFrameToCanvas(file, canvas) {
    return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const video = document.createElement("video");
        video.muted = true;
        video.playsInline = true;
        video.preload = "metadata";
        video.crossOrigin = "anonymous";

        const cleanup = () => URL.revokeObjectURL(url);

        video.addEventListener("loadedmetadata", () => {
            // Seek a hair past 0 to dodge black-frame intros on some encoders.
            video.currentTime = Math.min(0.1, video.duration / 10 || 0.1);
        });
        video.addEventListener("seeked", () => {
            try {
                const ctx = canvas.getContext("2d");
                ctx.save();
                try {
                    ctx.fillStyle = "#000000";
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    const scale = Math.min(
                        canvas.width / (video.videoWidth || 1),
                        canvas.height / (video.videoHeight || 1),
                    );
                    const drawW = (video.videoWidth || canvas.width) * scale;
                    const drawH = (video.videoHeight || canvas.height) * scale;
                    const drawX = (canvas.width - drawW) / 2;
                    const drawY = (canvas.height - drawH) / 2;
                    ctx.drawImage(video, drawX, drawY, drawW, drawH);
                } finally {
                    ctx.restore();
                }
                cleanup();
                resolve({ durationSeconds: video.duration });
            } catch (err) {
                cleanup();
                reject(err);
            }
        });
        video.addEventListener("error", () => {
            cleanup();
            reject(new Error("browser could not decode video"));
        });
        video.src = url;
    });
}

/**
 * Read `file` as a base64-encoded string (no data: prefix). Uses FileReader
 * because videos can be tens of MB and we don't want to hold two copies
 * (ArrayBuffer + base64) in memory longer than needed.
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
            // result is "data:<mime>;base64,<body>"; strip the prefix.
            const comma = result.indexOf(",");
            resolve(comma >= 0 ? result.slice(comma + 1) : result);
        };
        reader.onerror = () => reject(new Error("file read failed"));
        reader.readAsDataURL(file);
    });
}

function canvasToBase64(canvas) {
    const dataUrl = canvas.toDataURL("image/png");
    return dataUrl.split(",")[1];
}

function clearCanvas(canvas) {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.save();
    try {
        ctx.fillStyle = "#000000";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
    } finally {
        ctx.restore();
    }
}
