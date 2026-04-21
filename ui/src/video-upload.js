// Video upload: pick a video (any FFmpeg-decodable format), transcode
// client-side via ffmpeg.wasm to an H.264 MP4 at the device's configured
// dimensions, extract a thumbnail from the transcoded output, upload
// both to the backend.
//
// Transcode happens on file-pick (not on Save) so the operator sees the
// real thumbnail + the real duration once the wasm pipeline finishes.
// The produced MP4 is what lands on disk — playback on Pi Zero 2 W's
// hardware H.264 decoder is reliable because WE chose the profile.
//
// Panel-mode footnote: HUB75 / WS2812B / composite renderers ultimately
// want raw RGB frames, not H.264. The raw-frames storage path isn't
// shipped yet, so for now panel modes still upload H.264 (with a visible
// banner explaining the fallback). The ffmpeg.wasm raw-frames pipeline
// at /spike.html stays reachable for operators who want to inspect
// frame bytes while that storage layer lands.

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
                <span>Video file (any format ffmpeg can decode)</span>
                <input type="file" accept="video/*" class="field-file">
                <span class="field-hint video-upload-edit-hint" hidden>
                    Editing an existing video — leave the file picker blank
                    to just update name / duration.
                </span>
            </label>
            <p class="field-hint video-upload-panel-hint" hidden>
                This device is in a <strong>panel</strong> output mode
                (HUB75 / WS2812B / composite) — the real contract wants
                raw RGB frames. Raw-frames storage isn't shipped yet, so
                the video uploads as H.264 for now and the renderer will
                transcode on the way out. Maintainers watching: raw-frames
                storage is on the follow-up list.
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
            <button type="submit" class="primary field-save" disabled>Save video</button>
            <p class="video-upload-status" role="status" aria-live="polite"></p>
            <details class="video-upload-log">
                <summary>ffmpeg.wasm log</summary>
                <pre class="video-upload-log-body"></pre>
            </details>
        </form>
    </section>
`;

import {
    describeFfmpegError,
    transcodeToH264,
} from "./ffmpeg-pipelines.js";

const PANEL_OUTPUT_MODES = new Set(["hub75", "ws281x", "composite"]);

/**
 * Mount the video-upload UI into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {number} options.width  — sign width in pixels (transcode target)
 * @param {number} options.height — sign height in pixels
 * @param {string} [options.outputMode] — device's current output_mode
 *     from /api/settings. Drives the panel-fallback banner + future
 *     raw-frames-vs-H.264 branching when storage lands.
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
    { width, height, outputMode = "hdmi", onSave, onSaveExisting },
) {
    container.innerHTML = TEMPLATE;

    const canvas = container.querySelector(".video-upload-canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = `${width} / ${height}`;

    const headingEl = container.querySelector(".video-upload-heading");
    const newBtnEl = container.querySelector(".video-upload-new");
    const editHintEl = container.querySelector(".video-upload-edit-hint");
    const panelHintEl = container.querySelector(".video-upload-panel-hint");
    const fileEl = container.querySelector(".field-file");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const saveBtn = container.querySelector(".field-save");
    const statusEl = container.querySelector(".video-upload-status");
    const logEl = container.querySelector(".video-upload-log-body");
    const form = container.querySelector(".controls");

    panelHintEl.hidden = !PANEL_OUTPUT_MODES.has(outputMode);

    const logLines = [];
    function logFn(msg) {
        statusEl.textContent = msg;
        logLines.push(msg);
        if (logLines.length > 200) logLines.shift();
        if (logEl) logEl.textContent = logLines.join("\n");
    }

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

        // Pre-populate the slide name from the filename even if transcode
        // fails — operator can still type over it.
        if (nameEl.value === "Video") {
            nameEl.value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
        }

        // Disable Save during transcode; the in-flight guard handles the
        // rest once state flips back.
        saveBtn.dataset.inFlight = "1";
        updateSaveEnabled();
        try {
            logFn("transcoding via ffmpeg.wasm…");
            const mp4Bytes = await transcodeToH264(
                { file, width, height },
                logFn,
            );
            // Wrap the transcoded bytes in a Blob so the thumbnail is
            // extracted from the *encoded output* — that's what will
            // actually play on the device, so the preview matches reality.
            const mp4Blob = new Blob([mp4Bytes], { type: "video/mp4" });
            const [{ durationSeconds }, bytesB64] = await Promise.all([
                drawFirstFrameToCanvas(mp4Blob, canvas),
                fileToBase64(mp4Blob),
            ]);
            state.thumbnailCanvasReady = true;
            state.videoBytesBase64 = bytesB64;
            if (Number.isFinite(durationSeconds) && durationSeconds > 0) {
                durationEl.value = String(Math.round(durationSeconds));
            }
            logFn(
                `ready. transcoded to ${width}×${height} H.264 MP4 · ${(mp4Bytes.length / 1024).toFixed(1)} KB`,
            );
        } catch (err) {
            state.thumbnailCanvasReady = false;
            state.videoBytesBase64 = null;
            clearCanvas(canvas);
            logFn(`Could not process video: ${describeFfmpegError(err)}`);
        } finally {
            delete saveBtn.dataset.inFlight;
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
                pipeline: "h264_mp4",
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
