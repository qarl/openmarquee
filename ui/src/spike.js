// ffmpeg.wasm Phase-1 spike page — exercises both content pipelines.
//
// H.264 MP4 pipeline  (target: HDMI renderer, pi hardware decoder)
//   decode input → scale to target resolution → re-encode H.264 in MP4
//   at a modest bitrate → emit as a downloadable file.
//
// Raw RGB frames pipeline  (target: HUB75 / WS2812B / composite)
//   decode input → scale → emit concatenated RGB888 bytes at the given
//   FPS → download a .rgb blob the operator can byte-compare against
//   `ffmpeg -f rawvideo -pix_fmt rgb24`.
//
// Byte validation: an operator running `ffmpeg -i source.mp4 -vf
// scale=128:96,fps=10 -f rawvideo -pix_fmt rgb24 ref.rgb` should get
// bytes that diff cleanly against the raw-frames output here. The
// spike's whole point is proving we can.
//
// COI / SAB note: `@ffmpeg/ffmpeg` v0.12+ supports single-threaded mode
// without Cross-Origin-Isolation headers, so this page runs on any
// origin — including the captive portal, which doesn't ship
// Cross-Origin-Opener-Policy / Cross-Origin-Embedder-Policy.

import { FFmpeg } from "@ffmpeg/ffmpeg";
import { fetchFile, toBlobURL } from "@ffmpeg/util";

const FFMPEG_CORE_BASE = "/dist/vendor/ffmpeg-core";

let ffmpeg = null;

async function loadFfmpeg(logFn) {
    if (ffmpeg) return ffmpeg;
    logFn("loading ffmpeg-core…");
    const instance = new FFmpeg();
    instance.on("log", ({ message }) => logFn(message));
    instance.on("progress", ({ progress }) => {
        logFn(`progress: ${(progress * 100).toFixed(0)}%`);
    });
    await instance.load({
        // toBlobURL fetches the JS/wasm into a blob: URL so the worker
        // instantiates them without tripping classic CORS rules.
        coreURL: await toBlobURL(`${FFMPEG_CORE_BASE}/ffmpeg-core.js`, "text/javascript"),
        wasmURL: await toBlobURL(`${FFMPEG_CORE_BASE}/ffmpeg-core.wasm`, "application/wasm"),
        // @ffmpeg/ffmpeg's main thread spawns a Web Worker whose source
        // esbuild does NOT auto-bundle when it sees `new Worker(new
        // URL("./worker.js", import.meta.url))` — we ship it as a separate
        // entry (see package.json's build script: `ffmpeg-worker=…`).
        classWorkerURL: "/dist/ffmpeg-worker.js",
    });
    ffmpeg = instance;
    logFn("ffmpeg-core loaded.");
    return ffmpeg;
}

function makeLogger(statusEl, logEl) {
    const lines = [];
    return (msg) => {
        statusEl.textContent = msg;
        lines.push(msg);
        // Keep the log to the last ~200 lines so it doesn't run away.
        if (lines.length > 200) lines.shift();
        logEl.textContent = lines.join("\n");
    };
}

function downloadBytes(bytes, filename, mime) {
    const blob = new Blob([bytes], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.textContent = `download ${filename} (${(bytes.length / 1024 / 1024).toFixed(2)} MB)`;
    a.className = "spike-download";
    // Click-to-download is the point — rendering the anchor inline lets the
    // operator grab the file without a second tap.
    a.addEventListener("click", () => {
        // Don't revoke immediately — the browser may still be streaming
        // the download when the click handler returns.
        setTimeout(() => URL.revokeObjectURL(url), 60_000);
    });
    return a;
}

async function runH264Pipeline({ file, width, height }, logFn) {
    const ff = await loadFfmpeg(logFn);
    const inName = "input";
    const outName = "output.mp4";
    await ff.writeFile(inName, await fetchFile(file));
    logFn("transcoding to H.264 MP4…");
    await ff.exec([
        "-i", inName,
        "-vf", `scale=${width}:${height}`,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-an", // drop audio — signs don't speak
        outName,
    ]);
    const data = await ff.readFile(outName);
    return data;
}

async function runRawFramesPipeline({ file, width, height, fps }, logFn) {
    const ff = await loadFfmpeg(logFn);
    const inName = "input";
    const outName = "frames.rgb";
    await ff.writeFile(inName, await fetchFile(file));
    logFn("extracting raw RGB frames…");
    // -f rawvideo -pix_fmt rgb24 emits concatenated R,G,B,R,G,B,... bytes,
    // one frame after another, no header. Matches what the hzeller +
    // rpi_ws281x renderers want per SYSTEM_SPEC §7.6.
    await ff.exec([
        "-i", inName,
        "-vf", `scale=${width}:${height},fps=${fps}`,
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        outName,
    ]);
    const data = await ff.readFile(outName);
    return data;
}

function boot() {
    const fileEl = document.getElementById("source-file");
    const widthEl = document.getElementById("target-width");
    const heightEl = document.getElementById("target-height");
    const fpsEl = document.getElementById("target-fps");
    const h264Btn = document.getElementById("run-h264");
    const rgbBtn = document.getElementById("run-rgb");
    const statusEl = document.getElementById("spike-status");
    const logEl = document.getElementById("spike-log");
    const outputEl = document.getElementById("spike-output");
    const logFn = makeLogger(statusEl, logEl);

    async function withSpinner(runner, outName, mime) {
        const file = fileEl.files?.[0];
        if (!file) {
            logFn("pick a video first.");
            return;
        }
        h264Btn.disabled = true;
        rgbBtn.disabled = true;
        outputEl.innerHTML = "";
        const t0 = performance.now();
        try {
            const data = await runner(
                {
                    file,
                    width: Number(widthEl.value) || 128,
                    height: Number(heightEl.value) || 96,
                    fps: Number(fpsEl.value) || 10,
                },
                logFn,
            );
            const dt = ((performance.now() - t0) / 1000).toFixed(2);
            logFn(`done in ${dt}s; ${data.length} bytes`);
            outputEl.appendChild(downloadBytes(data, outName, mime));
        } catch (err) {
            // ffmpeg.wasm surfaces some failures as plain strings or as
            // objects with no .message — stringify defensively so the
            // spike page shows something useful instead of "undefined".
            let detail;
            if (err instanceof Error) {
                detail = err.message;
            } else if (typeof err === "string") {
                detail = err;
            } else {
                try {
                    detail = JSON.stringify(err);
                } catch {
                    detail = String(err);
                }
            }
            console.error("[spike] pipeline error:", err);
            logFn(`error: ${detail}`);
        } finally {
            h264Btn.disabled = false;
            rgbBtn.disabled = false;
        }
    }

    h264Btn.addEventListener("click", () =>
        withSpinner(runH264Pipeline, "output.mp4", "video/mp4"),
    );
    rgbBtn.addEventListener("click", () =>
        withSpinner(runRawFramesPipeline, "frames.rgb", "application/octet-stream"),
    );

    logFn("ready. pick a video, pick a pipeline, click.");
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}
