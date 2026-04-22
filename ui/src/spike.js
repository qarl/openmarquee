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

import {
    describeFfmpegError,
    extractRawFrames,
    transcodeToH264,
} from "./ffmpeg-pipelines.js";

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
    const hooks = {
        onStatus: logFn,
        onProgress: (pct) => logFn(`progress: ${pct.toFixed(0)}%`),
    };

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
                hooks,
            );
            const dt = ((performance.now() - t0) / 1000).toFixed(2);
            logFn(`done in ${dt}s; ${data.length} bytes`);
            outputEl.appendChild(downloadBytes(data, outName, mime));
        } catch (err) {
            console.error("[spike] pipeline error:", err);
            logFn(`error: ${describeFfmpegError(err)}`);
        } finally {
            h264Btn.disabled = false;
            rgbBtn.disabled = false;
        }
    }

    h264Btn.addEventListener("click", () =>
        withSpinner(transcodeToH264, "output.mp4", "video/mp4"),
    );
    rgbBtn.addEventListener("click", () =>
        withSpinner(extractRawFrames, "frames.rgb", "application/octet-stream"),
    );

    logFn("ready. pick a video, pick a pipeline, click.");
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}
