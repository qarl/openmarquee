// File/MediaElement-bound helpers extracted from video-upload.js for
// testability. video-upload's processVideo() calls these as local
// references inside the same module, which made them unmockable via
// vi.mock("./video-upload.js") (vi.mock can replace external imports,
// not same-module locals). Moving them out gives the test suite a
// clean mock seam without stubbing HTMLVideoElement / URL /
// FileReader.
//
// All 3 functions are pure file/blob → promise — no state, no
// closures over video-upload's mount context. Safe to extract;
// behavior identical to the pre-extract inline definitions.

/**
 * Load `file` into an offscreen <video>, seek to the first visible
 * frame, paint it onto `canvas` (cover-fit to canvas dimensions), and
 * resolve with the detected duration.
 *
 * @param {File | Blob} file
 * @param {HTMLCanvasElement} canvas
 * @returns {Promise<{ durationSeconds: number }>}
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
 * Read `file` as a base64-encoded string (no data: prefix). Uses
 * FileReader because videos can be tens of MB and we don't want to
 * hold two copies (ArrayBuffer + base64) in memory longer than
 * needed.
 *
 * @param {File | Blob} file
 * @returns {Promise<string>}
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

/**
 * Probe a source file's video dimensions via a hidden <video>
 * element. Used to pick the transcode target size (source dims,
 * capped at the Pi's 1080p H.264 decoder envelope). Resolves with
 * {width, height}; rejects on any decode failure so the caller
 * surfaces a clean error.
 *
 * @param {File | Blob} file
 * @returns {Promise<{ width: number, height: number }>}
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
