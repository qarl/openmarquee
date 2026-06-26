// Shared ffmpeg.wasm plumbing + the H.264 transcode helper.
//
// Consumer:
//   - Production video uploader (ui/src/video-upload.js) — transcodes
//     operator uploads to H.264 MP4 at the production 720p cap
//     (see video-upload.js header for why 720p vs the 1080p decoder
//     ceiling).
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

// Round 24: ffmpeg.wasm is single-threaded — concurrent transcodes
// share one exec queue + one MEMFS namespace on the singleton `ff`.
// Pre-fix, operator-driven concurrency (drag-drops B before A's 30s
// transcode finishes, or a retry fires before settle) interleaved
// writeFile/exec across calls and produced corrupted output silently.
//
// Serialize via a module-level promise chain (`_transcodeChain`).
// Each call appends its job to the chain and returns the chained
// promise. `.then(myJob, myJob)` resumes after BOTH success and
// failure of a prior job, so a single failed transcode doesn't
// wedge subsequent ones (B doesn't inherit A's error; B's caller
// gets B's own outcome).
//
// Filenames now use `++_transcodeSeq` (monotonic counter) instead
// of `Date.now()` — even at sub-ms cadence, no two jobs collide.
let _transcodeChain = Promise.resolve();
let _transcodeSeq = 0;

/**
 * Transcode a source video to H.264 MP4 at the target panel dims.
 * Returned bytes are the MP4 ready to upload.
 *
 * Concurrent calls SERIALIZE (r24) — ffmpeg.wasm's single-threaded
 * core can't run two execs in parallel without corrupting MEMFS.
 *
 * @param {object} opts
 * @param {object} [hooks]
 * @param {(msg: string) => void} [hooks.onStatus] — phase change ("transcoding…", "done.")
 * @param {(pct: number) => void} [hooks.onProgress] — 0..100, fires repeatedly.
 * @returns {Uint8Array}
 */
export function transcodeToH264(
    { file, width, height },
    { onStatus, onProgress } = {},
) {
    const myJob = async () => {
        const seq = ++_transcodeSeq;
        const inName = `input-${seq}`;
        const outName = `output-${seq}.mp4`;
        const ff = await getFfmpeg();
        const { fetchFile } = await _getFfmpegUtil();
        await ff.writeFile(inName, await fetchFile(file));
        onStatus?.("transcoding to H.264 MP4…");
        try {
            await withProgressListener(ff, onProgress, () => ff.exec([
                "-i", inName,
                // scale= + force-even-dimensions via the round-down trick; libx264
                // with yuv420p hates odd dimensions.
                "-vf", `scale=${width}:${height}:force_original_aspect_ratio=decrease,pad=${width}:${height}:(ow-iw)/2:(oh-ih)/2`,
                "-c:v", "libx264",
                // Perf-night r4 (2026-05-26): VPU-friendly profile.
                // Pi Zero 2 W VC4 hardware H.264 decoder (v4l2 path,
                // renderer/src/v4l2.rs + video_decode.rs) is happiest
                // on Baseline-profile content. Renderer's hand-rolled
                // mp4_demux.rs:6 documents the same expectation:
                // "baseline-profile H.264 video authored by ffmpeg's
                // libx264 output". libx264's CRF default lands at
                // High profile, which the decoder accepts but with
                // higher per-frame cost (CABAC entropy decode +
                // 8x8 DCT). Baseline drops both.
                "-profile:v", "baseline",
                // Cap level at 4.0. libx264 would otherwise pick a
                // level matching dims+bitrate (can land at 4.2+ on
                // borderline content); 4.2+ stresses the VC4 chip's
                // decode budget. 4.0 is the documented Pi Zero 2 W
                // comfort zone for 1080p30.
                "-level", "4.0",
                "-preset", "veryfast",
                "-crf", "23",
                // Zero B-frames. libx264's default `bframes=3` blows
                // up the VC4 decoder's reference-frame buffer pool at
                // 1080p — visible as decoder stalls under motion-
                // heavy content. The Baseline profile already
                // disallows B-frames, but the explicit `-bf 0` is
                // belt-and-suspenders + makes the intent obvious to
                // a future reader who might switch to Main.
                "-bf", "0",
                // GOP = 30 frames = 1 second at 30fps. libx264's
                // default `keyint=250` (8.3s) means a seek/cut/glitch
                // needs up to 8s to re-sync. The renderer's playback
                // model does pre-roll on slide transitions, so a
                // short GOP keeps that pre-roll cheap.
                "-g", "30",
                // Cap output to 30fps. Source may be 60fps (phone
                // video); the Pi VPU can decode 1080p30 comfortably
                // but 1080p60 lives on the edge of the documented
                // budget. Frame-rate down-conversion in ffmpeg is
                // cheap (frame drop) compared with the decoder
                // savings on the Pi.
                "-r", "30",
                // Cap peak bitrate at 8 Mbps + a 16 Mbps VBV buffer.
                // CRF-only encoding can spike to 12+ Mbps on motion-
                // heavy content — past the VC4's comfortable
                // sustained-decode bitrate. The cap is invisible on
                // typical signage content (logos / talking-head /
                // simple animations) but kicks in on the worst-case
                // fast-motion uploads.
                "-maxrate", "8M",
                "-bufsize", "16M",
                "-pix_fmt", "yuv420p",
                "-an", // drop audio — signs don't speak
                outName,
            ]));
            onProgress?.(100);
            return await ff.readFile(outName);
        } finally {
            // Round 25: cleanup ALWAYS runs, even when exec or
            // readFile throws. Pre-fix the cleanup ran after the
            // readFile await — a thrown exec (bad codec / corrupt
            // input / OOM) skipped cleanup and left the input
            // bytes resident in the singleton ffmpeg.wasm's MEMFS
            // for the page lifetime. ~5-10 failed 50-100MB
            // uploads OOM'd the tab.
            //
            // Per-file try/catch suppresses the "file not found"
            // case (outName never created when exec failed before
            // producing output; inName never created if writeFile
            // itself threw and we somehow reached here — defensive).
            try { await ff.deleteFile(inName); } catch { /* not found */ }
            try { await ff.deleteFile(outName); } catch { /* never created if exec failed */ }
        }
    };
    // Both arms run myJob so a prior job's rejection doesn't wedge
    // the chain. Each caller's awaited promise reflects ITS own job.
    const next = _transcodeChain.then(myJob, myJob);
    _transcodeChain = next;
    return next;
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
