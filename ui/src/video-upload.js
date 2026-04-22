// Video upload: pick a video (any FFmpeg-decodable format), transcode
// client-side via ffmpeg.wasm to H.264 MP4 at min(source, 1920×1080),
// upload the MP4 bytes + a first-frame thumbnail PNG.
//
// The 1080p cap is hardware-driven: the Pi Zero 2 W's H.264 decoder
// tops out at 1080p30; anything larger falls to software decode and
// stutters. The playback engine scales further down to the current
// panel dims via ffmpeg's filter graph at decode time, so the stored
// MP4 is resolution-independent below that cap.
//
// Processing happens on file-pick (not Save) so the operator sees the
// real thumbnail + real duration before committing.

const TEMPLATE = `
    <section class="video-upload">
        <h2 class="subpage-title">Video Slides</h2>
        <div class="slide-browser-slot"></div>
        <div class="video-upload-header">
            <h3 class="video-upload-heading">Upload a video</h3>
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
            <p class="field-hint">
                Transcoded to H.264 MP4, capped at 1080p. Playback
                rescales to panel dims on the device.
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
            <progress class="video-upload-progress" value="0" max="100" hidden></progress>
        </form>
    </section>
`;

import {
    describeFfmpegError,
    transcodeToH264,
} from "./ffmpeg-pipelines.js";
import { mountSlideBrowser, nextAutoName } from "./slide-browser.js";

// Hardware cap for the Pi Zero 2 W's H.264 decoder. 1080p30 is the
// documented maximum; anything larger falls back to software decode.
const MAX_VIDEO_W = 1920;
const MAX_VIDEO_H = 1080;

/** Compute the transcode target: source dims clamped to the hardware cap,
 * keeping aspect ratio and forcing even numbers (yuv420p hates odd). */
function pickTranscodeTarget(srcW, srcH) {
    if (!srcW || !srcH) return { width: MAX_VIDEO_W, height: MAX_VIDEO_H };
    const scale = Math.min(1, MAX_VIDEO_W / srcW, MAX_VIDEO_H / srcH);
    const w = Math.max(2, 2 * Math.floor((srcW * scale) / 2));
    const h = Math.max(2, 2 * Math.floor((srcH * scale) / 2));
    return { width: w, height: h };
}

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
    { width, height, outputMode = "hdmi", onSave, onSaveExisting, fetchItems },
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
    const saveBtn = container.querySelector(".field-save");
    const statusEl = container.querySelector(".video-upload-status");
    const progressEl = container.querySelector(".video-upload-progress");
    const form = container.querySelector(".controls");

    // outputMode is no longer a branching signal — all modes receive the
    // same H.264 MP4 and the device scales at decode time. Accepted for
    // signature compat with the old caller.
    void outputMode;

    function setStatus(msg) {
        statusEl.textContent = msg;
    }
    function setProgress(pct) {
        progressEl.hidden = false;
        progressEl.value = pct;
    }
    function clearProgress() {
        progressEl.hidden = true;
        progressEl.value = 0;
    }
    const transcodeHooks = { onStatus: setStatus, onProgress: setProgress };

    const state = {
        // Populated only when the operator picks a NEW file. In edit
        // mode these stay null when they leave the picker empty; Save
        // then omits the fields so the server retains existing bytes.
        mp4Base64: null,
        durationSeconds: null,
        thumbnailCanvasReady: false,
        editingId: null,
    };

    function updateSaveEnabled() {
        const hasNewFile = state.thumbnailCanvasReady && state.mp4Base64;
        saveBtn.disabled =
            (!state.editingId && !hasNewFile)
            || saveBtn.dataset.inFlight === "1";
    }

    clearCanvas(canvas);

    fileEl.addEventListener("change", async () => {
        const file = fileEl.files?.[0];
        if (!file) {
            state.thumbnailCanvasReady = false;
            state.mp4Base64 = null;
            state.durationSeconds = null;
            if (!state.editingId) clearCanvas(canvas);
            updateSaveEnabled();
            return;
        }

        // Pre-populate the slide name from the filename even if processing
        // fails — operator can still type over it.
        if (nameEl.value === "Video") {
            nameEl.value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
        }

        saveBtn.dataset.inFlight = "1";
        updateSaveEnabled();
        try {
            await processVideo(file);
        } catch (err) {
            state.thumbnailCanvasReady = false;
            state.mp4Base64 = null;
            clearCanvas(canvas);
            setStatus(`Could not process video: ${describeFfmpegError(err)}`);
        } finally {
            clearProgress();
            delete saveBtn.dataset.inFlight;
            updateSaveEnabled();
        }
    });

    async function processVideo(file) {
        setStatus("inspecting source…");
        // Source dims drive the transcode target — we keep the source
        // resolution verbatim up to the Pi's 1080p H.264 decoder cap.
        const { width: srcW, height: srcH } = await peekVideoDims(file);
        const target = pickTranscodeTarget(srcW, srcH);

        setStatus("transcoding via ffmpeg.wasm…");
        setProgress(0);
        const mp4Bytes = await transcodeToH264(
            { file, width: target.width, height: target.height },
            transcodeHooks,
        );
        const mp4Blob = new Blob([mp4Bytes], { type: "video/mp4" });
        const [{ durationSeconds }, bytesB64] = await Promise.all([
            drawFirstFrameToCanvas(mp4Blob, canvas),
            fileToBase64(mp4Blob),
        ]);
        state.thumbnailCanvasReady = true;
        state.mp4Base64 = bytesB64;
        state.durationSeconds = durationSeconds;
        if (Number.isFinite(durationSeconds) && durationSeconds > 0) {
            durationEl.value = String(Math.round(durationSeconds));
        }
        setStatus(
            `ready. ${target.width}×${target.height} H.264 MP4 · ${(mp4Bytes.length / 1024).toFixed(1)} KB`,
        );
    }

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
                png_base64: includeAssets ? canvasToBase64(canvas) : null,
                mp4_base64: includeAssets ? state.mp4Base64 : null,
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

    async function resetToBlank() {
        // Sync blank-state setup. Anything here can be safely
        // overridden by a loadForEdit that interleaves later.
        state.editingId = null;
        state.thumbnailCanvasReady = false;
        state.mp4Base64 = null;
        state.durationSeconds = null;
        headingEl.textContent = "Upload a video";
        newBtnEl.hidden = true;
        editHintEl.hidden = true;
        fileEl.value = "";
        durationEl.value = "10";
        clearCanvas(canvas);
        updateSaveEnabled();

        // Async tail: gap-filled default name + browser refresh, both
        // no-ops if loadForEdit took ownership during the await.
        const defaultName = await computeDefaultName();
        if (state.editingId !== null) return;
        nameEl.value = defaultName;
        if (browser) {
            await browser.refresh();
            browser.highlight(null);
        }
    }

    async function computeDefaultName() {
        if (!fetchItems) return "Video Slide 1";
        try {
            const items = await fetchItems();
            return nextAutoName(
                items.filter((i) => i.type === "video"),
                "Video Slide",
            );
        } catch {
            return "Video Slide 1";
        }
    }

    async function loadForEdit(slide) {
        if (!slide || slide.type !== "video") {
            statusEl.textContent =
                "Only video slides are editable here — text and image open their own editors.";
            return;
        }
        state.editingId = String(slide.id);
        state.thumbnailCanvasReady = false;
        state.mp4Base64 = null;
        state.durationSeconds = null;
        headingEl.textContent = `Editing: ${slide.name || "Untitled"}`;
        newBtnEl.hidden = false;
        editHintEl.hidden = false;
        if (browser) browser.highlight(slide.id);
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

    let browser = null;
    if (fetchItems) {
        browser = mountSlideBrowser(
            container.querySelector(".slide-browser-slot"),
            {
                type: "video",
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
                    // Cover-fit so the preview matches what plays — the
                    // device renders this exact bitmap and a center-crop
                    // beats letterbox bars on a portrait source.
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
        // `auto` (vs `metadata`) ensures the browser actually buffers
        // a frame; without it the seek can complete before any pixel
        // data exists and the canvas reads black.
        video.preload = "auto";
        video.crossOrigin = "anonymous";

        const cleanup = () => URL.revokeObjectURL(url);
        let drew = false;

        function paint() {
            if (drew) return;
            // Need at least HAVE_CURRENT_DATA so the video's texture has
            // a frame for drawImage to read.
            if (video.readyState < 2 || !video.videoWidth) return;
            drew = true;
            try {
                const ctx = canvas.getContext("2d");
                ctx.save();
                try {
                    ctx.fillStyle = "#000000";
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    // Cover-fit so the thumbnail matches what plays.
                    const scale = Math.max(
                        canvas.width / video.videoWidth,
                        canvas.height / video.videoHeight,
                    );
                    const drawW = video.videoWidth * scale;
                    const drawH = video.videoHeight * scale;
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
        }

        // Seek only after `loadeddata` — guarantees at least one frame
        // exists, so the subsequent `seeked` event isn't firing on an
        // empty video texture.
        video.addEventListener("loadeddata", () => {
            video.currentTime = Math.min(0.1, video.duration / 10 || 0.1);
        });
        video.addEventListener("seeked", () => {
            // Some browsers fire `seeked` before the new frame is
            // composited into the video element's texture. One rAF
            // is enough breathing room for drawImage to read the
            // post-seek pixels instead of the prior frame's (often
            // black) backing store.
            requestAnimationFrame(paint);
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

/**
 * Probe a source file's video dimensions via a hidden <video> element.
 * Used to pick the transcode target size (source dims, capped at the
 * Pi's 1080p H.264 decoder envelope). Resolves with {width, height};
 * rejects on any decode failure so the caller surfaces a clean error.
 */
export function peekVideoDims(file) {
    return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const video = document.createElement("video");
        video.muted = true;
        video.playsInline = true;
        video.preload = "metadata";
        video.addEventListener("loadedmetadata", () => {
            const w = video.videoWidth;
            const h = video.videoHeight;
            URL.revokeObjectURL(url);
            if (!w || !h) {
                reject(new Error("could not read video dimensions"));
                return;
            }
            resolve({ width: w, height: h });
        });
        video.addEventListener("error", () => {
            URL.revokeObjectURL(url);
            reject(new Error("browser could not decode video"));
        });
        video.src = url;
    });
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
