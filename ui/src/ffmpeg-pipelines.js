// Shared ffmpeg.wasm plumbing + the two content-pipeline helpers.
//
// Consumers:
//   - /spike.html (ui/src/spike.js) — maintainer-facing page that
//     exercises both pipelines end-to-end with downloadable outputs.
//   - Production video uploader (ui/src/video-upload.js) — graduates
//     the H.264 re-encode path into the upload flow so operators get a
//     device-sized MP4 every time, not whatever source they picked.
//
// COI / SAB note: `@ffmpeg/ffmpeg` v0.12+ supports single-threaded mode
// without Cross-Origin-Isolation headers, so this runs on the captive-
// portal's bare HTTP surface without needing COOP/COEP.

import { FFmpeg } from "@ffmpeg/ffmpeg";
import { fetchFile, toBlobURL } from "@ffmpeg/util";

const FFMPEG_CORE_BASE = "/dist/vendor/ffmpeg-core";

let _instance = null;
let _loadPromise = null;

/**
 * Lazy-init the ffmpeg.wasm runtime and return the singleton instance.
 * Safe to call concurrently — concurrent callers share the in-flight
 * load() promise so we never instantiate two cores.
 *
 * @param {(msg: string) => void} [logFn] — receives ffmpeg's per-frame
 *     log + progress lines; defaults to a no-op for silent consumers.
 */
export async function getFfmpeg(logFn = () => {}) {
    if (_instance) return _instance;
    if (_loadPromise) return _loadPromise;

    _loadPromise = (async () => {
        logFn("loading ffmpeg-core…");
        const instance = new FFmpeg();
        instance.on("log", ({ message }) => logFn(message));
        instance.on("progress", ({ progress }) => {
            logFn(`progress: ${(progress * 100).toFixed(0)}%`);
        });
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
        logFn("ffmpeg-core loaded.");
        return instance;
    })();
    try {
        return await _loadPromise;
    } finally {
        _loadPromise = null;
    }
}

/**
 * Transcode a source video to H.264 MP4 at the target panel dims.
 * Returned bytes are the MP4 ready to upload.
 *
 * @returns {Uint8Array}
 */
export async function transcodeToH264({ file, width, height }, logFn = () => {}) {
    const ff = await getFfmpeg(logFn);
    const inName = `input-${Date.now()}`;
    const outName = `output-${Date.now()}.mp4`;
    await ff.writeFile(inName, await fetchFile(file));
    logFn("transcoding to H.264 MP4…");
    await ff.exec([
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
    ]);
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

/**
 * Extract concatenated RGB888 frames at the target panel dims + FPS.
 * Format is the rawvideo `rgb24` pixel contract from SYSTEM_SPEC §7.6:
 * row-major, top-left first, three bytes per pixel (R, G, B). No header.
 *
 * @returns {Uint8Array}
 */
export async function extractRawFrames(
    { file, width, height, fps },
    logFn = () => {},
) {
    const ff = await getFfmpeg(logFn);
    const inName = `input-${Date.now()}`;
    const outName = `frames-${Date.now()}.rgb`;
    await ff.writeFile(inName, await fetchFile(file));
    logFn("extracting raw RGB frames…");
    await ff.exec([
        "-i", inName,
        "-vf", `scale=${width}:${height},fps=${fps}`,
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        outName,
    ]);
    const data = await ff.readFile(outName);
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
