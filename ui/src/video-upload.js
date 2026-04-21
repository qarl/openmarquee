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
        <h2 class="video-upload-heading">Upload a video</h2>
        <div class="preview-wrap">
            <canvas class="video-upload-canvas" aria-label="thumbnail preview"></canvas>
        </div>
        <form class="controls" autocomplete="off">
            <label class="field">
                <span>Video file (MP4)</span>
                <input type="file" accept="video/mp4" class="field-file">
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
            <div class="row">
                <label class="field">
                    <span>Pipeline</span>
                    <select class="field-pipeline">
                        <option value="h264_mp4" selected>H.264 MP4 (HDMI)</option>
                        <option value="raw_frames">Raw frames (HUB75/WS2812B/composite) — spike only</option>
                    </select>
                </label>
                <label class="field">
                    <span>Transition into next</span>
                    <select class="field-transition">
                        <option value="cut" selected>Cut (instant)</option>
                        <option value="fade">Fade</option>
                    </select>
                </label>
            </div>
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
 * @param {number} options.width  — sign width (thumbnail is scaled to this)
 * @param {number} options.height — sign height
 * @param {(payload: object) => Promise<any>} options.onSave — called with
 *     { name, duration_ms, pipeline, transition, png_base64, mp4_base64 }
 */
export function mountVideoUploader(container, { width, height, onSave }) {
    container.innerHTML = TEMPLATE;

    const canvas = container.querySelector(".video-upload-canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = `${width} / ${height}`;

    const fileEl = container.querySelector(".field-file");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const pipelineEl = container.querySelector(".field-pipeline");
    const transitionEl = container.querySelector(".field-transition");
    const saveBtn = container.querySelector(".field-save");
    const statusEl = container.querySelector(".video-upload-status");
    const form = container.querySelector(".controls");

    // The in-memory video bytes + thumbnail; populated on file pick.
    let videoBytesBase64 = null;
    let hasThumbnail = false;

    function updateSaveEnabled() {
        saveBtn.disabled = !(hasThumbnail && videoBytesBase64)
            || saveBtn.dataset.inFlight === "1";
    }

    clearCanvas(canvas);

    fileEl.addEventListener("change", async () => {
        const file = fileEl.files?.[0];
        if (!file) {
            hasThumbnail = false;
            videoBytesBase64 = null;
            clearCanvas(canvas);
            updateSaveEnabled();
            return;
        }

        statusEl.textContent = "Reading file…";
        try {
            // Extract thumbnail + detected duration in parallel with the bytes read.
            const [{ durationSeconds }, bytesB64] = await Promise.all([
                drawFirstFrameToCanvas(file, canvas),
                fileToBase64(file),
            ]);
            hasThumbnail = true;
            videoBytesBase64 = bytesB64;
            if (Number.isFinite(durationSeconds) && durationSeconds > 0) {
                durationEl.value = String(Math.round(durationSeconds));
            }
            if (nameEl.value === "Video") {
                nameEl.value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
            }
            statusEl.textContent = "";
        } catch (err) {
            hasThumbnail = false;
            videoBytesBase64 = null;
            clearCanvas(canvas);
            statusEl.textContent = `Could not read video: ${err.message}`;
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
            const durationSeconds = Number(durationEl.value) || 10;
            await onSave({
                name: nameEl.value || "Video",
                duration_ms: Math.round(durationSeconds * 1000),
                pipeline: pipelineEl.value,
                transition: transitionEl.value,
                png_base64,
                mp4_base64: videoBytesBase64,
            });
            statusEl.textContent = "Saved.";
            // Reset for the next upload.
            fileEl.value = "";
            clearCanvas(canvas);
            hasThumbnail = false;
            videoBytesBase64 = null;
        } catch (err) {
            statusEl.textContent = `Save failed: ${err.message}`;
        } finally {
            delete saveBtn.dataset.inFlight;
            updateSaveEnabled();
        }
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
