// Shared ffmpeg.wasm plumbing + the H.264 transcode helper.
//
// Consumer:
//   - Production video uploader (ui/src/video-upload.js) — transcodes
//     operator uploads to H.264 MP4 at the Pi's 1080p decoder cap.
//
// (The maintainer-facing /spike.html dev probe was retired alongside
// simulator.js; if you need to exercise the pipeline end-to-end again,
// resurrect from git history rather than keeping it in the bundle.)
//
// COI / SAB note: `@ffmpeg/ffmpeg` v0.12+ supports single-threaded mode
// without Cross-Origin-Isolation headers, so this runs on the captive-
// portal's bare HTTP surface without needing COOP/COEP.

// Batch 7.4: @ffmpeg/ffmpeg + @ffmpeg/util are large (~30 KB
// minified between them) and only needed for video uploads. Load
// lazily inside getFfmpeg + transcodeToH264 so the cold path that
// never uploads a video doesn't pay for the parse.

const FFMPEG_CORE_BASE = "/dist/vendor/ffmpeg-core";

let _instance = null;
let _loadPromise = null;
// Cached @ffmpeg/util namespace -- fetchFile is needed at transcode
// time. Same import resolves to the same module via the dynamic-
// import dedupe, so this just avoids the extra await on warm calls.
let _ffmpegUtil = null;

async function _getFfmpegUtil() {
    if (!_ffmpegUtil) {
        _ffmpegUtil = await import("@ffmpeg/util");
    }
    return _ffmpegUtil;
}

/**
 * Lazy-init the ffmpeg.wasm runtime and return the singleton instance.
 * Safe to call concurrently — concurrent callers share the in-flight
 * load() promise so we never instantiate two cores.
 *
 * The instance is global and its log/progress handlers are persistent,
 * so per-call wiring (status / progress bars on the UI) can't live on
 * the FFmpeg instance — it lives on the per-call helpers below, and
 * the verbose per-frame ffmpeg log goes to console.debug only.
 */
export async function getFfmpeg() {
    if (_instance) return _instance;
    if (_loadPromise) return _loadPromise;

    _loadPromise = (async () => {
        // Lazy-import both packages -- they're only reachable from
        // this singleton constructor, which only fires when a
        // video upload starts.
        const { FFmpeg } = await import("@ffmpeg/ffmpeg");
        const { toBlobURL } = await _getFfmpegUtil();
        const instance = new FFmpeg();
        // Keep verbose log out of the UI — operators don't want a wall
        // of "frame=  42 fps=12 q=23.0 ..." text. Console only.
        instance.on("log", ({ message }) => console.debug("[ffmpeg]", message));
        await instance.load({
            coreURL: await toBlobURL(
                `${FFMPEG_CORE_BASE}/ffmpeg-core.js`,
                "text/javascript",
            ),
            wasmURL: await toBlobURL(
                `${FFMPEG_CORE_BASE}/ffmpeg-core.wasm`,
                "application/wasm",
            ),
            // @ffmpeg/ffmpeg spawns a module Web Worker; esbuild doesn't
            // auto-bundle the `new URL('./worker.js', import.meta.url)`
            // pattern, so we ship it as a separate entry (see package.json
            // build script: `ffmpeg-worker=…`).
            classWorkerURL: "/dist/ffmpeg-worker.js",
        });
        _instance = instance;
        return instance;
    })();
    try {
        return await _loadPromise;
    } finally {
        _loadPromise = null;
    }
}

// Per-call progress wiring. The ffmpeg.wasm "progress" event handler
// is global on the instance, so we attach + detach a one-off forwarder
// for each pipeline call instead of leaking listeners.
//
// Async + try/finally so listener detachment is guaranteed on every
// exit path -- including a synchronous throw out of fn(). The previous
// shape used Promise.resolve(fn()).finally(...) which never reached
// .finally() if fn() threw before being wrapped, leaving the listener
// attached on the singleton (and pinning the caller's onProgress
// closure for the singleton's lifetime). Exported for direct test
// access; the bug is invisible through transcodeToH264 because the
// mock's exec() is async so its throws are never synchronous.
export async function withProgressListener(ff, onProgress, fn) {
    if (!onProgress) return fn();
    const listener = ({ progress }) => {
        // ffmpeg.wasm reports progress as a 0..1 fraction that can
        // briefly overshoot 1.0 (spurious end-of-stream tick). Clamp
        // so the UI bar doesn't visually rebound.
        const pct = Math.max(0, Math.min(1, progress)) * 100;
        try { onProgress(pct); } catch { /* never let UI errors abort the pipeline */ }
    };
    ff.on("progress", listener);
    try {
        return await fn();
    } finally {
        try { ff.off?.("progress", listener); } catch { /* older ffmpeg.wasm exposes no off() */ }
    }
}

/**
 * Transcode a source video to H.264 MP4 at the target panel dims.
 * Returned bytes are the MP4 ready to upload.
 *
 * @param {object} opts
 * @param {object} [hooks]
 * @param {(msg: string) => void} [hooks.onStatus] — phase change ("transcoding…", "done.")
 * @param {(pct: number) => void} [hooks.onProgress] — 0..100, fires repeatedly.
 * @returns {Uint8Array}
 */
export async function transcodeToH264(
    { file, width, height },
    { onStatus, onProgress } = {},
) {
    const ff = await getFfmpeg();
    const { fetchFile } = await _getFfmpegUtil();
    const inName = `input-${Date.now()}`;
    const outName = `output-${Date.now()}.mp4`;
    await ff.writeFile(inName, await fetchFile(file));
    onStatus?.("transcoding to H.264 MP4…");
    await withProgressListener(ff, onProgress, () => ff.exec([
        "-i", inName,
        // scale= + force-even-dimensions via the round-down trick; libx264
        // with yuv420p hates odd dimensions.
        "-vf", `scale=${width}:${height}:force_original_aspect_ratio=decrease,pad=${width}:${height}:(ow-iw)/2:(oh-ih)/2`,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-an", // drop audio — signs don't speak
        outName,
    ]));
    onProgress?.(100);
    const data = await ff.readFile(outName);
    // Best-effort cleanup — ffmpeg.wasm's virtual FS can accumulate.
    try {
        await ff.deleteFile(inName);
        await ff.deleteFile(outName);
    } catch {
        // ignore
    }
    return data;
}

/** Best-effort stringify for ffmpeg.wasm's occasional non-Error throws. */
export function describeFfmpegError(err) {
    if (err instanceof Error) return err.message;
    if (typeof err === "string") return err;
    try {
        return JSON.stringify(err);
    } catch {
        return String(err);
    }
}
