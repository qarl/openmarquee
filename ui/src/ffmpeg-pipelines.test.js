// Tests for ffmpeg-pipelines: the describeFfmpegError mapping
// table + transcodeToH264 contract (via mocked @ffmpeg/ffmpeg).
//
// Per QA Batch 2 dispatch: describeFfmpegError covers the
// non-Error throw shapes that @ffmpeg/ffmpeg occasionally emits;
// transcodeToH264 is exercised via a mocked FFmpeg class so the
// real wasm core isn't loaded.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// r11 (2026-05-26): replace clock-poll-as-side-effect-proxy with a
// deadline-bounded side-effect poll. The 4 sites below previously
// used fixed-iteration `for (i = 0; i < N; i++) await
// setTimeout(0)` to wait for a mock side-effect (listener
// attachment, exec push). Under full-suite load on the Mac dev box,
// vitest's per-test environment slows past N microtask hops and
// the assertion fires before the side-effect lands. Same anti-
// pattern shape as r9's pytest fix (02a08fa: clock-poll vs side-
// effect-await); same fix shape ported to JS.
//
// Returns true iff `predicate()` evaluated truthy within the
// timeout window. Caller asserts the truth value AND the side-
// effect — the latter gives the post-mortem the right diagnostic
// shape (which mock field was null) on a real regression. 5ms
// poll cadence avoids busy-wait at sub-microtask precision; 1s
// outer deadline absorbs jitter while keeping a true regression
// fast-to-fail (vs. silently chewing the 5s test timeout).
async function waitForCondition(
    predicate,
    { timeoutMs = 1000, intervalMs = 5 } = {},
) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (predicate()) return true;
        await new Promise((r) => setTimeout(r, intervalMs));
    }
    return Boolean(predicate());
}

// --- @ffmpeg/ffmpeg + @ffmpeg/util mocks ----------------------------------
// The mocked FFmpeg instance records writeFile/exec/readFile/
// deleteFile calls and exposes the .on('progress', ...) listener
// so tests can drive progress updates manually.

const ffmpegState = {
    instance: null,
    progressListener: null,
    logListener: null,
    // When set to a Promise, the mock's exec() awaits it before
    // resolving. Tests use this to keep the pipeline "in flight"
    // long enough to fire progress events through the registered
    // listener.
    execGate: null,
};

vi.mock("@ffmpeg/ffmpeg", () => {
    class FFmpeg {
        constructor() {
            this.calls = { writeFile: [], exec: [], readFile: [], deleteFile: [] };
            ffmpegState.instance = this;
        }
        on(event, listener) {
            if (event === "progress") ffmpegState.progressListener = listener;
            if (event === "log") ffmpegState.logListener = listener;
        }
        off(event, listener) {
            if (event === "progress" && ffmpegState.progressListener === listener) {
                ffmpegState.progressListener = null;
            }
        }
        async load() { return undefined; }
        async writeFile(name, data) {
            this.calls.writeFile.push({ name, data });
        }
        async exec(args) {
            this.calls.exec.push(args);
            if (ffmpegState.execGate) await ffmpegState.execGate;
        }
        async readFile(name) {
            this.calls.readFile.push(name);
            return new Uint8Array([0xDE, 0xAD, 0xBE, 0xEF]);
        }
        async deleteFile(name) {
            this.calls.deleteFile.push(name);
        }
    }
    return { FFmpeg };
});

vi.mock("@ffmpeg/util", () => ({
    fetchFile: async file => new Uint8Array([1, 2, 3, 4]),
    toBlobURL: async url => `blob:${url}`,
}));

// Reset module state between tests so getFfmpeg's singleton
// doesn't leak across cases.
beforeEach(async () => {
    vi.resetModules();
    ffmpegState.instance = null;
    ffmpegState.progressListener = null;
    ffmpegState.logListener = null;
    ffmpegState.execGate = null;
});

afterEach(() => {
    vi.restoreAllMocks();
});

// --- describeFfmpegError --------------------------------------------------

describe("describeFfmpegError", () => {
    it("unwraps Error.message", async () => {
        const { describeFfmpegError } = await import("./ffmpeg-pipelines.js");
        expect(describeFfmpegError(new Error("boom"))).toBe("boom");
    });

    it("passes through plain strings", async () => {
        const { describeFfmpegError } = await import("./ffmpeg-pipelines.js");
        expect(describeFfmpegError("string error")).toBe("string error");
    });

    it("JSON-stringifies plain objects", async () => {
        const { describeFfmpegError } = await import("./ffmpeg-pipelines.js");
        expect(describeFfmpegError({ code: 42, msg: "x" }))
            .toBe('{"code":42,"msg":"x"}');
    });

    it("falls back to String(err) when JSON.stringify throws (circular)", async () => {
        // Circular refs throw inside JSON.stringify -- the catch
        // arm uses String(err) which produces "[object Object]"
        // for a bare object.
        const { describeFfmpegError } = await import("./ffmpeg-pipelines.js");
        const circular = {};
        circular.self = circular;
        expect(describeFfmpegError(circular)).toBe("[object Object]");
    });

    it("handles primitives via JSON.stringify (numbers, booleans)", async () => {
        const { describeFfmpegError } = await import("./ffmpeg-pipelines.js");
        expect(describeFfmpegError(42)).toBe("42");
        expect(describeFfmpegError(true)).toBe("true");
        expect(describeFfmpegError(null)).toBe("null");
    });

    it("handles Error subclasses (preserves .message)", async () => {
        const { describeFfmpegError } = await import("./ffmpeg-pipelines.js");
        class FfmpegRuntimeError extends Error {}
        expect(describeFfmpegError(new FfmpegRuntimeError("bad codec")))
            .toBe("bad codec");
    });
});

// --- transcodeToH264 ------------------------------------------------------

describe("transcodeToH264", () => {
    it("writes input, runs exec with H.264 args, reads output, returns bytes", async () => {
        const { transcodeToH264 } = await import("./ffmpeg-pipelines.js");
        const fakeFile = new Blob(["fake mp4"], { type: "video/mp4" });
        const bytes = await transcodeToH264({
            file: fakeFile, width: 1280, height: 720,
        });

        expect(bytes).toBeInstanceOf(Uint8Array);
        expect(bytes.length).toBe(4);  // mock readFile returns 4 bytes

        const ff = ffmpegState.instance;
        expect(ff.calls.writeFile).toHaveLength(1);
        expect(ff.calls.exec).toHaveLength(1);
        expect(ff.calls.readFile).toHaveLength(1);

        const args = ff.calls.exec[0];
        // Pin the load-bearing flags. If any of these regress,
        // the Pi decoder would reject the output -- it's strict
        // about yuv420p / even dimensions / no-audio.
        expect(args).toContain("-c:v");
        expect(args[args.indexOf("-c:v") + 1]).toBe("libx264");
        expect(args).toContain("-pix_fmt");
        expect(args[args.indexOf("-pix_fmt") + 1]).toBe("yuv420p");
        expect(args).toContain("-an");  // no audio
        expect(args.find(a => a.startsWith("scale=1280:720"))).toBeTruthy();
    });

    // Perf-night r4 (2026-05-26): VPU-friendly preset pin. Each flag
    // pinned here is rationalized in ffmpeg-pipelines.js's transcodeToH264
    // body; the test catches an accidental flag removal that would
    // re-introduce VPU-stalling output.
    it("emits VPU-friendly H.264 flags (baseline / level 4.0 / bf=0 / g=30 / r=30 / maxrate 8M)", async () => {
        const { transcodeToH264 } = await import("./ffmpeg-pipelines.js");
        await transcodeToH264({
            file: new Blob(["fake mp4"], { type: "video/mp4" }),
            width: 1280,
            height: 720,
        });
        const args = ffmpegState.instance.calls.exec[0];

        // Baseline profile — Pi VC4 decoder's documented comfort zone
        // (mp4_demux.rs:6 + renderer/src/v4l2.rs).
        expect(args).toContain("-profile:v");
        expect(args[args.indexOf("-profile:v") + 1]).toBe("baseline");

        // Level 4.0 — caps the decoder's per-frame budget. Level 4.0
        // supports 1080p30 (fix-D-1 in main proves the decoder
        // handles it); production cap is 720p (uploads clamp via
        // pickTranscodeTarget) but the encoder level stays at 4.0
        // so a future 1080p experiment doesn't have to re-encode.
        expect(args).toContain("-level");
        expect(args[args.indexOf("-level") + 1]).toBe("4.0");

        // No B-frames — VC4's reference-frame buffer pool stalls
        // under bframes>=1 (libx264 default is 3). Required by
        // mp4_demux.rs's baseline-only assumption.
        expect(args).toContain("-bf");
        expect(args[args.indexOf("-bf") + 1]).toBe("0");

        // 1s GOP at 30fps — keeps slide-transition pre-roll cheap.
        expect(args).toContain("-g");
        expect(args[args.indexOf("-g") + 1]).toBe("30");

        // 30fps cap — VC4 sustains 720p30 well under load; the
        // matching r=30 caps over-spec source uploads (60fps phone
        // captures) to the production rate.
        expect(args).toContain("-r");
        expect(args[args.indexOf("-r") + 1]).toBe("30");

        // 8 Mbps peak / 16 Mbps VBV buffer — caps motion-heavy
        // content from spiking past the VC4's sustained-decode rate.
        expect(args).toContain("-maxrate");
        expect(args[args.indexOf("-maxrate") + 1]).toBe("8M");
        expect(args).toContain("-bufsize");
        expect(args[args.indexOf("-bufsize") + 1]).toBe("16M");
    });

    it("calls onStatus with the transcoding phase message", async () => {
        const { transcodeToH264 } = await import("./ffmpeg-pipelines.js");
        const onStatus = vi.fn();
        await transcodeToH264(
            { file: new Blob([]), width: 320, height: 240 },
            { onStatus },
        );
        expect(onStatus).toHaveBeenCalledWith("transcoding to H.264 MP4…");
    });

    it("forwards progress events to onProgress (clamped 0..1 * 100)", async () => {
        const { transcodeToH264 } = await import("./ffmpeg-pipelines.js");
        const onProgress = vi.fn();

        // Hold exec() open so the listener stays attached and we
        // can fire progress events through it.
        let releaseExec;
        ffmpegState.execGate = new Promise(r => { releaseExec = r; });

        const pipeline = transcodeToH264(
            { file: new Blob([]), width: 320, height: 240 },
            { onProgress },
        );

        // r11: wait for the listener to attach. Batch 7.4 made the
        // FFmpeg + util imports dynamic; under suite load the
        // attachment can take more than the original fixed 20
        // microtask hops. waitForCondition with a 1s deadline
        // eliminates the race surface vs the old fixed-iteration
        // poll. See helper at the top of this file for rationale.
        const attached = await waitForCondition(() => ffmpegState.progressListener);
        expect(attached).toBe(true);
        expect(ffmpegState.progressListener).toBeTypeOf("function");

        ffmpegState.progressListener({ progress: 0.5 });
        // Out-of-band clamp tests.
        ffmpegState.progressListener({ progress: 1.5 });   // -> 100
        ffmpegState.progressListener({ progress: -0.1 });  // -> 0

        releaseExec();
        await pipeline;

        expect(onProgress).toHaveBeenCalledWith(50);
        expect(onProgress).toHaveBeenCalledWith(100);
        expect(onProgress).toHaveBeenCalledWith(0);
    });

    it("cleans up writeFile + readFile via deleteFile (best-effort)", async () => {
        const { transcodeToH264 } = await import("./ffmpeg-pipelines.js");
        await transcodeToH264({
            file: new Blob([]), width: 320, height: 240,
        });
        const ff = ffmpegState.instance;
        expect(ff.calls.deleteFile).toHaveLength(2);
        // Both the input and the output file get removed.
        expect(ff.calls.deleteFile.some(n => n.startsWith("input-"))).toBe(true);
        expect(ff.calls.deleteFile.some(n => n.startsWith("output-"))).toBe(true);
    });

    it("UI-side onProgress exceptions in the listener forwarder don't abort the pipeline", async () => {
        // NB: only the LISTENER-forwarded onProgress call is
        // try/catch-wrapped (see withProgressListener). The final
        // unconditional onProgress?.(100) at the end of
        // transcodeToH264 is NOT wrapped -- but a UI that throws
        // synchronously from a progress callback is already an
        // app bug, and the resulting rejection still lets the
        // transcoded bytes reach memory. This test pins the
        // documented "listener forwarder swallows UI errors"
        // contract from the header comment.
        const { transcodeToH264 } = await import("./ffmpeg-pipelines.js");
        // Throw only from the listener-forwarded calls (50%, etc.)
        // -- let the final 100 succeed by switching to no-op.
        const onProgress = vi.fn().mockImplementationOnce(() => {
            throw new Error("UI bar fell over");
        });

        let releaseExec;
        ffmpegState.execGate = new Promise(r => { releaseExec = r; });

        const pipeline = transcodeToH264(
            { file: new Blob([]), width: 320, height: 240 },
            { onProgress },
        );
        // r11: deadline-bounded side-effect-await (helper at top
        // of file); same rationale as the listener-attach wait
        // above.
        await waitForCondition(() => ffmpegState.progressListener);
        expect(ffmpegState.progressListener).toBeTypeOf("function");
        ffmpegState.progressListener({ progress: 0.3 });  // throws, swallowed
        releaseExec();

        await expect(pipeline).resolves.toBeInstanceOf(Uint8Array);
        // The listener-throw didn't stop the final onProgress(100).
        expect(onProgress).toHaveBeenCalledWith(100);
    });
});

// --- withProgressListener cleanup contract -------------------------------

describe("withProgressListener cleanup", () => {
    // Direct test of the wrapper. Uses an inline fakeFf with on/off
    // recorders rather than the FFmpeg mock because the bug is
    // invisible through transcodeToH264 -- the mock's exec() is async,
    // so its throws are never SYNCHRONOUS, and only the sync-throw
    // path bypasses the listener-detach in the pre-fix shape.
    function makeFakeFf() {
        const onCalls = [];
        const offCalls = [];
        return {
            ff: {
                on(event, listener) { onCalls.push({ event, listener }); },
                off(event, listener) { offCalls.push({ event, listener }); },
            },
            onCalls,
            offCalls,
        };
    }

    it("removes progress listener when fn throws synchronously", async () => {
        // Regression-lock for the bug fixed alongside this test: the
        // pre-fix shape `return Promise.resolve(fn()).finally(detach)`
        // never reached .finally() if fn() threw before being wrapped,
        // leaving the listener attached on the ffmpeg.wasm singleton
        // (and pinning the caller's onProgress closure forever).
        const { withProgressListener } = await import("./ffmpeg-pipelines.js");
        const { ff, onCalls, offCalls } = makeFakeFf();
        await expect(
            withProgressListener(ff, () => {}, () => { throw new Error("boom"); }),
        ).rejects.toThrow("boom");
        expect(onCalls).toHaveLength(1);
        expect(onCalls[0].event).toBe("progress");
        expect(offCalls).toHaveLength(1);
        expect(offCalls[0].event).toBe("progress");
        expect(offCalls[0].listener).toBe(onCalls[0].listener);
    });

    it("removes progress listener when fn rejects asynchronously", async () => {
        // Companion lock: the async-rejection path was already correct
        // pre-fix, but the new try/finally shape must preserve it.
        const { withProgressListener } = await import("./ffmpeg-pipelines.js");
        const { ff, onCalls, offCalls } = makeFakeFf();
        await expect(
            withProgressListener(ff, () => {}, async () => { throw new Error("async-boom"); }),
        ).rejects.toThrow("async-boom");
        expect(offCalls).toHaveLength(1);
        expect(offCalls[0].listener).toBe(onCalls[0].listener);
    });

    it("removes progress listener after a successful sync return", async () => {
        const { withProgressListener } = await import("./ffmpeg-pipelines.js");
        const { ff, onCalls, offCalls } = makeFakeFf();
        const result = await withProgressListener(ff, () => {}, () => "done");
        expect(result).toBe("done");
        expect(offCalls).toHaveLength(1);
        expect(offCalls[0].listener).toBe(onCalls[0].listener);
    });

    it("skips listener attachment entirely when onProgress is null", async () => {
        // Fast-path preservation. The wrapper short-circuits before
        // any .on() call so the no-progress caller pays nothing.
        const { withProgressListener } = await import("./ffmpeg-pipelines.js");
        const { ff, onCalls, offCalls } = makeFakeFf();
        const result = await withProgressListener(ff, null, () => "fast-path");
        expect(result).toBe("fast-path");
        expect(onCalls).toHaveLength(0);
        expect(offCalls).toHaveLength(0);
    });
});

// --- r24 serialization ---------------------------------------------------

describe("transcodeToH264 serialization (r24)", () => {
    // Round-24 regression-locks: ffmpeg.wasm is single-threaded;
    // concurrent transcodes pre-fix shared one exec queue + MEMFS,
    // producing corrupted output silently. The fix uses a module-
    // level promise chain so B's exec runs strictly after A's
    // cleanup completes.

    it("B's exec runs only after A's cleanup completes when both fire concurrently", async () => {
        const { transcodeToH264 } = await import("./ffmpeg-pipelines.js");
        // Hold A's exec on a gate so we can prove B doesn't start.
        let releaseGate;
        ffmpegState.execGate = new Promise((r) => { releaseGate = r; });

        // Fire A; it'll hang on the gate after writeFile + exec push.
        const pipelineA = transcodeToH264({
            file: new Blob(["A"], { type: "video/mp4" }),
            width: 320, height: 240,
        });

        // r11: wait for the mock instance + A's exec to be queued.
        // Replaces fixed 30-iteration microtask poll which flaked
        // under suite load when the Batch 7.4 dynamic-import hops
        // exceeded the budget. See waitForCondition helper at top.
        await waitForCondition(
            () => ffmpegState.instance && ffmpegState.instance.calls.exec.length > 0,
        );
        const ff = ffmpegState.instance;
        expect(ff.calls.exec).toHaveLength(1);
        expect(ff.calls.writeFile).toHaveLength(1);

        // Fire B while A is in-flight (gated).
        const pipelineB = transcodeToH264({
            file: new Blob(["B"], { type: "video/mp4" }),
            width: 320, height: 240,
        });

        // r11: spin the microtask queue for a bounded window to
        // PROVE B's writeFile + exec did NOT run (negative-result
        // wait — predicate stays false through the whole window).
        // Different shape than the positive-result waits above:
        // here we're confirming the absence of a side-effect by
        // exhausting the queue. 100ms is well past any reasonable
        // mock-resolution latency under suite load + matches the
        // "30 microtask hops" original intent without the
        // hop-count cliff.
        const negativeWindowEnd = Date.now() + 100;
        while (Date.now() < negativeWindowEnd) {
            await new Promise((r) => setTimeout(r, 5));
        }
        expect(ff.calls.exec).toHaveLength(1);  // only A's
        expect(ff.calls.writeFile).toHaveLength(1);  // only A's

        // Release A. Now B's chain link resolves and B runs.
        releaseGate();
        ffmpegState.execGate = null;
        await pipelineA;
        await pipelineB;

        // Both have completed.
        expect(ff.calls.exec).toHaveLength(2);
        expect(ff.calls.writeFile).toHaveLength(2);
        // Monotonic counter: A's and B's writeFile names differ.
        expect(ff.calls.writeFile[0].name).not.toBe(ff.calls.writeFile[1].name);
        expect(ff.calls.writeFile[0].name).toMatch(/^input-\d+$/);
        expect(ff.calls.writeFile[1].name).toMatch(/^input-\d+$/);
    });

    it("a failed prior transcode does NOT wedge the chain — B still runs", async () => {
        const { transcodeToH264 } = await import("./ffmpeg-pipelines.js");
        // A's exec throws once.
        const origExec = ffmpegState;  // capture for restoration
        let aExecFired = false;
        ffmpegState.execGate = new Promise((_, reject) => {
            // Reject immediately when A's exec awaits the gate. The
            // mock's exec does `await ffmpegState.execGate`, so a
            // rejected gate makes exec throw → myJob throws → A's
            // promise rejects.
            setTimeout(() => reject(new Error("A failed")), 0);
        });
        // Suppress unhandled-rejection from the gate itself (vitest
        // is strict). We attach a no-op .catch.
        ffmpegState.execGate.catch(() => {});

        const pipelineA = transcodeToH264({
            file: new Blob(["A"], { type: "video/mp4" }),
            width: 320, height: 240,
        }).catch(() => "A-failed");

        await pipelineA;
        // Clear the rejected gate so B's exec succeeds.
        ffmpegState.execGate = null;

        const pipelineB = await transcodeToH264({
            file: new Blob(["B"], { type: "video/mp4" }),
            width: 320, height: 240,
        });
        expect(pipelineB).toBeInstanceOf(Uint8Array);
        // The chain ran B even after A's rejection.
        const ff = ffmpegState.instance;
        expect(ff.calls.exec.length).toBeGreaterThanOrEqual(1);
    });
});

// --- r25 MEMFS cleanup on failure ----------------------------------------

describe("transcodeToH264 MEMFS cleanup (r25)", () => {
    // Round-25 regression-lock: pre-fix cleanup ran AFTER the
    // exec/readFile chain — a thrown exec (bad codec, corrupt input,
    // OOM) bypassed cleanup and left the input bytes resident in the
    // singleton ffmpeg.wasm's MEMFS for the page lifetime. Combined
    // with r24's serialized chain, repeated failures pile up
    // monotonically. ~5-10 failed 50-100MB uploads OOM'd the tab.
    //
    // Fix: try/finally around the exec+readFile block; finally runs
    // ff.deleteFile(inName) + ff.deleteFile(outName) with per-file
    // try/catch suppressing "file not found" for the never-created
    // outName.

    it("calls deleteFile(inName) even when exec throws (counterfactual: pre-fix didn't)", async () => {
        const { transcodeToH264 } = await import("./ffmpeg-pipelines.js");
        // Reject the gate so the mock's exec throws.
        ffmpegState.execGate = Promise.reject(new Error("bad codec"));
        // Suppress unhandled-rejection on the gate itself.
        ffmpegState.execGate.catch(() => {});

        let caught = null;
        try {
            await transcodeToH264({
                file: new Blob(["A"], { type: "video/mp4" }),
                width: 320, height: 240,
            });
        } catch (err) {
            caught = err;
        }
        // Function rejected as expected.
        expect(caught).toBeInstanceOf(Error);
        expect(caught.message).toBe("bad codec");

        // Cleanup STILL ran. Pre-fix: deleteFile would not have been
        // called at all because the cleanup block was AFTER the
        // throw site (no try/finally).
        const ff = ffmpegState.instance;
        // At minimum the inName was deleted; outName attempt also
        // fires (its try/catch absorbs the "not found" since exec
        // never produced output).
        const deletedNames = ff.calls.deleteFile;
        expect(deletedNames).toContain("input-1");
    });

    it("happy path still cleans up both inName AND outName", async () => {
        const { transcodeToH264 } = await import("./ffmpeg-pipelines.js");
        const bytes = await transcodeToH264({
            file: new Blob(["A"], { type: "video/mp4" }),
            width: 320, height: 240,
        });
        expect(bytes).toBeInstanceOf(Uint8Array);
        const ff = ffmpegState.instance;
        // Both files cleaned. seq starts fresh per test (resetModules
        // in beforeEach) so the first call gets seq=1.
        expect(ff.calls.deleteFile).toContain("input-1");
        expect(ff.calls.deleteFile).toContain("output-1.mp4");
    });
});
