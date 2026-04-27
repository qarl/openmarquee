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
        <div class="slide-browser-slot"></div>
        <form class="controls" autocomplete="off">
            <div class="om-card" style="margin-bottom: 12px;">
                <div class="om-row" style="gap: 10px;">
                    <label class="om-field" style="flex: 1;">
                        <span>Slide name</span>
                        <input type="text" class="om-input field-name" value="Video" maxlength="200">
                    </label>
                    <label class="om-field" style="width: 110px;">
                        <span>Duration (s)</span>
                        <input type="number" class="om-input field-duration" value="10" min="1" max="3600" step="1">
                    </label>
                </div>
            </div>
            <div class="preview-wrap">
                <video class="video-upload-video" playsinline controls muted
                       controlslist="novolumeslider nofullscreen noplaybackrate nodownload noremoteplayback"
                       aria-label="video preview"></video>
            </div>
            <div class="om-card">
                <label class="om-field">
                    <span>Video file (any format ffmpeg can decode)</span>
                    <input type="file" accept="video/*" class="om-input field-file">
                </label>
            </div>
            <p class="om-save-status video-upload-status" role="status" aria-live="polite" data-state="idle"></p>
            <progress class="video-upload-progress" value="0" max="100" hidden style="width: 100%; margin-top: 6px;"></progress>
        </form>
    </section>
`;

import { attachAutoSave } from "./auto-save.js";
import { drawPlaceholderToCanvas } from "./image-upload.js";
import {
    describeFfmpegError,
    transcodeToH264,
} from "./ffmpeg-pipelines.js";
import { mountSlideBrowser, nextAutoName } from "./slide-browser.js";

// Tiny pre-encoded H.264 MP4 (128×96, ~0.5s, all-black) used as the
// placeholder bytes when +New creates a fresh video slide. The backend
// requires valid MP4 bytes on create; this satisfies the validator
// without running ffmpeg.wasm just to make a placeholder. The real MP4
// replaces these bytes via the auto-save PATCH on first file pick.
const PLACEHOLDER_MP4_B64 =
    "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAANCbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAAhYAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAm10cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAhYAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAIAAAABgAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAIWAAAAAAABAAAAAAHlbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAA8AAAAIABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABkG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAVBzdGJsAAAAuHN0c2QAAAAAAAAAAQAAAKhhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAIAAYABIAAAASAAAAAAAAAABFUxhdmM2MS4xOS4xMDEgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAALmF2Y0MBQsAK/+EAF2dCwAraCDbARAAAAwAEAAADAHg8SJqAAQAEaM4PyAAAABBwYXNwAAAAAQAAAAEAAAAUYnRydAAAAAAAACo/AAAAAAAAABhzdHRzAAAAAAAAAAEAAAAIAAAEAAAAABRzdHNzAAAAAAAAAAEAAAABAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAAIAAAAAQAAADRzdHN6AAAAAAAAAAAAAAAIAAACiwAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAUc3RjbwAAAAAAAAABAAADcgAAAGF1ZHRhAAAAWW1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJhcHBsAAAAAAAAAAAAAAAALGlsc3QAAAAkqXRvbwAAABxkYXRhAAAAAQAAAABMYXZmNjEuNy4xMDAAAAAIZnJlZQAAAtltZGF0AAACVAYF//9Q3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NCByMzEwOCAzMWUxOWY5IC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyMyAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTAgcmVmPTEgZGVibG9jaz0wOjA6MCBhbmFseXNlPTA6MCBtZT1kaWEgc3VibWU9MCBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0wIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MCA4eDhkY3Q9MCBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0wIHRocmVhZHM9MyBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTAgd2VpZ2h0cD0wIGtleWludD0yNTAga2V5aW50X21pbj0xNSBzY2VuZWN1dD0wIGludHJhX3JlZnJlc2g9MCByYz1jcmYgbWJ0cmVlPTAgY3JmPTIzLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MACAAAAAL2WIhDomKAAJAsnJycnJycnXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXgAAAABkGaIDaBjAAAAAZBmkA6gYwAAAAGQZpgOoGMAAAABkGagDqBjAAAAAZBmqA6gYwAAAAGQZrAPoGMAAAABkGa4D6BjA==";

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

    const videoEl = container.querySelector(".video-upload-video");
    videoEl.style.aspectRatio = `${width} / ${height}`;

    // Hidden offscreen canvas used only for thumbnail generation —
    // painted from the transcoded video's first frame (new file) or
    // the existing server PNG (edit mode). canvasToBase64 at save time
    // grabs these bytes as the slide's `asset.png` thumbnail.
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;

    const fileEl = container.querySelector(".field-file");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const statusEl = container.querySelector(".video-upload-status");
    const progressEl = container.querySelector(".video-upload-progress");
    const form = container.querySelector(".controls");

    // outputMode is no longer a branching signal — all modes receive the
    // same H.264 MP4 and the device scales at decode time. Accepted for
    // signature compat with the old caller.
    void outputMode;

    function setStatus(msg) {
        statusEl.textContent = msg;
        // Transcode-progress messages are advisory, not save status —
        // mark them so the auto-save helper's color rules don't apply.
        statusEl.dataset.state = "idle";
    }
    function setProgress(pct) {
        progressEl.hidden = false;
        progressEl.value = pct;
    }
    function clearProgress() {
        progressEl.hidden = true;
        progressEl.value = 0;
    }

    // The preview <video>'s current src — tracked so we can revoke a
    // blob URL when it's replaced. Server URLs are left alone.
    let currentPreviewObjectUrl = null;
    function setPreviewSrc(src, revokeOnSwap) {
        try { videoEl.pause?.(); } catch { /* ignore */ }
        if (currentPreviewObjectUrl) {
            URL.revokeObjectURL(currentPreviewObjectUrl);
            currentPreviewObjectUrl = null;
        }
        if (src) {
            videoEl.src = src;
            if (revokeOnSwap) currentPreviewObjectUrl = src;
        } else {
            videoEl.removeAttribute("src");
            videoEl.load?.();
        }
    }
    function clearPreview() {
        setPreviewSrc(null, false);
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

    clearCanvas(canvas);

    fileEl.addEventListener("change", async () => {
        const file = fileEl.files?.[0];
        if (!file) {
            state.thumbnailCanvasReady = false;
            state.mp4Base64 = null;
            state.durationSeconds = null;
            if (!state.editingId) {
                clearCanvas(canvas);
                clearPreview();
            }
            return;
        }

        // Pre-populate the slide name from the filename even if processing
        // fails — operator can still type over it.
        if (nameEl.value === "Video") {
            nameEl.value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
        }

        // Suspend auto-save while ffmpeg.wasm chews on the file — the
        // status pill shows transcoding progress and we don't want a save
        // attempt mid-transcode. Field edits (name, duration) made during
        // transcode aren't lost: they live in the DOM, and the post-
        // transcode kick() reads current field values via performSave.
        autoSave.cancel();
        try {
            await processVideo(file);
            // After transcode completes, kick auto-save so the new bytes
            // get persisted without an explicit Save button click.
            autoSave.kick();
        } catch (err) {
            state.thumbnailCanvasReady = false;
            state.mp4Base64 = null;
            clearCanvas(canvas);
            clearPreview();
            statusEl.textContent = `Could not process video: ${describeFfmpegError(err)}`;
            statusEl.dataset.state = "error";
        } finally {
            clearProgress();
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
        // Point the preview <video> at the transcoded blob so the
        // operator can scrub + play the exact bytes the device will
        // store. Revoke on the next load to avoid blob-URL leakage.
        setPreviewSrc(URL.createObjectURL(mp4Blob), /* revokeOnSwap */ true);
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

    async function performSave() {
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
            // Subsequent metadata-only saves shouldn't re-upload the
            // (already-stored) MP4 bytes.
            state.thumbnailCanvasReady = false;
            state.mp4Base64 = null;
            return;
        }
        const created = await onSave(payload);
        if (created?.id) {
            state.editingId = String(created.id);
            state.thumbnailCanvasReady = false;
            state.mp4Base64 = null;
            if (browser) browser.highlight(state.editingId);
        }
    }

    const autoSave = attachAutoSave(form, {
        save: performSave,
        status: statusEl,
        canSave: () => Boolean(
            state.editingId
            || (state.thumbnailCanvasReady && state.mp4Base64),
        ),
    });

    async function resetToBlank() {
        // Sync blank-state setup. Anything here can be safely
        // overridden by a loadForEdit that interleaves later.
        state.editingId = null;
        state.thumbnailCanvasReady = false;
        state.mp4Base64 = null;
        state.durationSeconds = null;
        fileEl.value = "";
        durationEl.value = "10";
        clearCanvas(canvas);
        clearPreview();
        autoSave.cancel();
        statusEl.textContent = "";
        statusEl.dataset.state = "idle";

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
        if (browser) browser.highlight(slide.id);
        nameEl.value = slide.name || "Video";
        durationEl.value = String(
            Math.max(1, (slide.duration_ms || 10000) / 1000),
        );
        // Paint the stored thumbnail into the hidden canvas (used as
        // the save-time `asset.png` if the operator never re-picks a
        // file) and point the preview <video> at the server's MP4 so
        // they can play it back inline.
        try {
            await drawUrlToCanvas(`/api/content/${slide.id}/asset`, canvas);
        } catch (err) {
            statusEl.textContent = `Could not load thumbnail: ${err.message}`;
            statusEl.dataset.state = "error";
        }
        setPreviewSrc(`/api/content/${slide.id}/video`, /* revokeOnSwap */ false);
        // Loading is not an edit — drop any auto-save scheduled by the
        // field mutations above.
        autoSave.cancel();
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

    // Initial state: a blank create form. Same UX argument as the image
    // uploader (QA 2026-04-26 explore-image-upload.md): operators come
    // here to upload NEW files; auto-opening the most-recent video for
    // edit means a fresh file pick PUTs over its bytes silently.
    // Slide-browser tiles still dispatch loadForEdit on click for the
    // explicit edit-existing path.
    (async () => {
        await resetToBlank();
    })();
    /**
     * +New flow: render a placeholder thumbnail + submit a tiny
     * pre-bundled black-frame MP4 so the slide IMMEDIATELY appears in
     * the pallet. Then pop the file picker so the operator's next click
     * picks the real video — the change handler transcodes + auto-save
     * PATCHes both PNG + MP4 with the real bytes.
     */
    async function createNew() {
        await resetToBlank();
        drawPlaceholderToCanvas(canvas, nameEl.value || "New video");
        // No auto file-picker pop — would race with the placeholder save
        // (twin slides) or break the user-gesture chain on Safari if
        // popped after. Operator clicks Choose File as the next step.
        const payload = {
            name: nameEl.value || "Video",
            duration_ms: 10_000,
            png_base64: canvasToBase64(canvas),
            mp4_base64: PLACEHOLDER_MP4_B64,
        };
        try {
            const created = await onSave(payload);
            if (created?.id) {
                state.editingId = String(created.id);
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
