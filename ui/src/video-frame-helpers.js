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
        //
        // JUDDER-DEFECT-A (2026-06-22): seek to 0 (was
        // `min(0.1, duration/10)`) so the thumbnail/poster is the
        // TRUE frame 0 of the H.264 stream — pixel-identical to the
        // renderer's first decoded frame.
        //
        // Pre-fix the 0.1s seek grabbed frame ~3 (at 30fps) or
        // even later for short clips (duration/10), causing a
        // BACKWARD JUMP at the poster→live handoff: poster shows
        // frame 3, then live decoder starts at frame 0 → "starts
        // before the poster... back in time" (qarl glass
        // observation). QA confirmed via fp_y comparison on
        // Rainbow:
        //   poster [97,130,144,144,211,187,191,215,237]
        //   live   [182,203,221,141,198,178,101,133,144]
        // BRIGHT/DARK INVERSION = genuinely different frame (no
        // monotonic scale/range transform could produce that).
        //
        // The original 0.1s seek was a "skip the often-black
        // intro" heuristic. The renderer's playback starts at
        // PTS 0 anyway; if the operator's clip has a black intro,
        // the playback shows it too. Better to match what plays
        // than to "skip ahead" for the thumbnail.
        //
        // Setting currentTime=0 on a video already at 0 (post-
        // loadeddata default) MAY NOT fire `seeked` in all
        // browsers (no-op seek). Fall back to a direct rAF→paint;
        // paint()'s `drew` guard prevents double-fire if `seeked`
        // also fires.
        video.addEventListener("loadeddata", () => {
            video.currentTime = 0;
            // Defensive: direct paint trigger in case seeked
            // doesn't fire (no-op seek to current position).
            // paint() bails harmlessly if readyState/videoWidth
            // aren't ready yet.
            requestAnimationFrame(paint);
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
