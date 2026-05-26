// @vitest-environment jsdom
//
// Perf-night r8 / r2.5: phase histogram capture tests.
//
// Split:
//   1. Pure helpers (captureButtonLabel, captureButtonDisabled)
//   2. Capture orchestrator (captureHistogram) — fetcher + sleep
//      injected for hermetic deterministic timing
//   3. copyTextToClipboard (modern path + legacy fallback)
//   4. Mount lifecycle (build/destroy/state transitions)

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
    PERF_CAPTURE_FRAMES,
    PERF_CAPTURE_WAIT_MS,
    captureButtonDisabled,
    captureButtonLabel,
    captureHistogram,
    copyTextToClipboard,
    mountPerfHistogramControl,
} from "./perf-histogram.js";

beforeEach(() => {
    document.body.innerHTML = "";
});

afterEach(() => {
    vi.restoreAllMocks();
});

// =====================================================================
// 1. Pure helpers
// =====================================================================

describe("captureButtonLabel", () => {
    it("idle: default label", () => {
        expect(captureButtonLabel("idle")).toBe("Capture phase histogram");
    });
    it("capturing: in-progress label", () => {
        expect(captureButtonLabel("capturing")).toBe(
            "Capturing 300 frames (~10s)…",
        );
    });
    it("ready: 'Capture again' to invite re-capture", () => {
        expect(captureButtonLabel("ready")).toBe("Capture again");
    });
    it("error: 'Capture again' (same as ready — operator retries)", () => {
        expect(captureButtonLabel("error")).toBe("Capture again");
    });
    it("not_ready: 'Retry dump' (skip the wait, just re-fetch dump)", () => {
        expect(captureButtonLabel("not_ready")).toBe("Retry dump");
    });
    it("unknown: falls back to idle label", () => {
        expect(captureButtonLabel("nonsense")).toBe("Capture phase histogram");
    });
});

describe("captureButtonDisabled", () => {
    it("idle: enabled", () => {
        expect(captureButtonDisabled("idle")).toBe(false);
    });
    it("capturing: disabled (don't let operator double-click)", () => {
        expect(captureButtonDisabled("capturing")).toBe(true);
    });
    it("ready: enabled (re-capture)", () => {
        expect(captureButtonDisabled("ready")).toBe(false);
    });
    it("error: enabled (let operator retry)", () => {
        expect(captureButtonDisabled("error")).toBe(false);
    });
    it("not_ready: enabled (let operator click retry-dump)", () => {
        expect(captureButtonDisabled("not_ready")).toBe(false);
    });
});

describe("module constants pin the wire contract", () => {
    it("PERF_CAPTURE_FRAMES is 300 (matches code1's renderer profile spec)", () => {
        expect(PERF_CAPTURE_FRAMES).toBe(300);
    });

    it("PERF_CAPTURE_WAIT_MS is 12000 (10s for 300 frames @ 30fps + 2s grace)", () => {
        expect(PERF_CAPTURE_WAIT_MS).toBe(12_000);
    });
});

// =====================================================================
// 2. Capture orchestrator
// =====================================================================

function makeFetcherReturning(map) {
    // map: { [path]: response | () => response }
    return vi.fn(async (path, init) => {
        const entry = map[path];
        if (!entry) {
            throw new Error(`unexpected fetch ${path}`);
        }
        return typeof entry === "function" ? entry(init) : entry;
    });
}

describe("captureHistogram orchestrator", () => {
    it("happy path: start 204 → wait → dump ready → returns text", async () => {
        const fetcher = makeFetcherReturning({
            "/api/playback/perf/start": { ok: true, status: 204 },
            "/api/playback/perf/dump": {
                ok: true,
                status: 200,
                json: async () => ({ ready: true, text: "perf histogram body" }),
            },
        });
        const sleep = vi.fn(async () => {});
        const result = await captureHistogram({ fetcher, sleep, waitMs: 50 });
        expect(result).toEqual({ kind: "ready", text: "perf histogram body" });
        expect(fetcher).toHaveBeenCalledTimes(2);
        expect(sleep).toHaveBeenCalledWith(50, undefined);
    });

    it("posts start with frames body + JSON content-type", async () => {
        const fetcher = makeFetcherReturning({
            "/api/playback/perf/start": { ok: true, status: 204 },
            "/api/playback/perf/dump": {
                ok: true,
                status: 200,
                json: async () => ({ ready: true, text: "ok" }),
            },
        });
        await captureHistogram({
            fetcher,
            sleep: async () => {},
            waitMs: 0,
            frames: 300,
        });
        const startCall = fetcher.mock.calls.find(
            (c) => c[0] === "/api/playback/perf/start",
        );
        expect(startCall).toBeTruthy();
        const init = startCall[1];
        expect(init.method).toBe("POST");
        expect(init.headers["Content-Type"]).toBe("application/json");
        expect(JSON.parse(init.body)).toEqual({ frames: 300 });
    });

    it("start 503 with detail body: returns error with detail", async () => {
        const fetcher = makeFetcherReturning({
            "/api/playback/perf/start": {
                ok: false,
                status: 503,
                json: async () => ({
                    detail: "active renderer does not support profile capture (only RustRenderer does)",
                }),
            },
        });
        const result = await captureHistogram({
            fetcher,
            sleep: async () => {},
            waitMs: 0,
        });
        expect(result.kind).toBe("error");
        expect(result.detail).toBe(
            "active renderer does not support profile capture (only RustRenderer does)",
        );
    });

    it("start network failure: returns error with the throw message", async () => {
        const fetcher = vi.fn(async () => {
            throw new Error("network down");
        });
        const result = await captureHistogram({
            fetcher,
            sleep: async () => {},
            waitMs: 0,
        });
        expect(result.kind).toBe("error");
        expect(result.detail).toMatch(/start request failed.*network down/);
    });

    it("dump ready=false: returns not_ready with the text body", async () => {
        const fetcher = makeFetcherReturning({
            "/api/playback/perf/start": { ok: true, status: 204 },
            "/api/playback/perf/dump": {
                ok: true,
                status: 200,
                json: async () => ({
                    ready: false,
                    text: "frames_remaining=42 (still going)",
                }),
            },
        });
        const result = await captureHistogram({
            fetcher,
            sleep: async () => {},
            waitMs: 0,
        });
        expect(result).toEqual({
            kind: "not_ready",
            text: "frames_remaining=42 (still going)",
        });
    });

    it("dump 503: returns error", async () => {
        const fetcher = makeFetcherReturning({
            "/api/playback/perf/start": { ok: true, status: 204 },
            "/api/playback/perf/dump": {
                ok: false,
                status: 503,
                json: async () => ({ detail: "swapped to Mock mid-flight" }),
            },
        });
        const result = await captureHistogram({
            fetcher,
            sleep: async () => {},
            waitMs: 0,
        });
        expect(result.kind).toBe("error");
        expect(result.detail).toBe("swapped to Mock mid-flight");
    });

    it("AbortSignal aborts mid-wait: throws AbortError", async () => {
        const fetcher = makeFetcherReturning({
            "/api/playback/perf/start": { ok: true, status: 204 },
        });
        const ctrl = new AbortController();
        // Sleep impl that respects abort signal (matches the real
        // production sleep): start the promise, abort, expect throw.
        const promise = captureHistogram({
            fetcher,
            signal: ctrl.signal,
            waitMs: 100,
        });
        // Let the start fetch + sleep schedule. Then abort.
        await new Promise((r) => setTimeout(r, 5));
        ctrl.abort();
        await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    });
});

// =====================================================================
// 3. copyTextToClipboard
// =====================================================================

describe("copyTextToClipboard", () => {
    it("uses navigator.clipboard.writeText when available", async () => {
        const writeText = vi.fn(async () => {});
        const nav = { clipboard: { writeText } };
        const ok = await copyTextToClipboard("hello", { nav });
        expect(ok).toBe(true);
        expect(writeText).toHaveBeenCalledWith("hello");
    });

    it("falls back to textarea + execCommand when clipboard API missing", async () => {
        const doc = document; // real jsdom doc
        // Stub execCommand because jsdom doesn't implement it natively.
        const execCommand = vi.fn(() => true);
        doc.execCommand = execCommand;
        const ok = await copyTextToClipboard("legacy text", {
            doc,
            nav: {},
        });
        expect(ok).toBe(true);
        expect(execCommand).toHaveBeenCalledWith("copy");
    });

    it("falls back to textarea on clipboard.writeText reject", async () => {
        const writeText = vi.fn(async () => {
            throw new Error("not allowed");
        });
        const doc = document;
        doc.execCommand = vi.fn(() => true);
        const ok = await copyTextToClipboard("text", {
            doc,
            nav: { clipboard: { writeText } },
        });
        expect(ok).toBe(true);
    });

    it("returns false when both modern + legacy paths fail", async () => {
        const doc = document;
        doc.execCommand = vi.fn(() => false);
        const ok = await copyTextToClipboard("text", { doc, nav: {} });
        expect(ok).toBe(false);
    });
});

// =====================================================================
// 4. Mount lifecycle
// =====================================================================

function makeContainer() {
    const c = document.createElement("div");
    document.body.appendChild(c);
    return c;
}

describe("mountPerfHistogramControl lifecycle", () => {
    it("builds button + status + hidden output", () => {
        const c = makeContainer();
        const { destroy } = mountPerfHistogramControl({ container: c });
        const root = c.querySelector("[data-perf-histogram]");
        expect(root).not.toBeNull();
        const btn = c.querySelector("[data-perf-histogram-capture]");
        expect(btn).not.toBeNull();
        expect(btn.textContent).toBe("Capture phase histogram");
        expect(btn.disabled).toBe(false);
        destroy();
    });

    it("destroy removes the root + is idempotent", () => {
        const c = makeContainer();
        const { destroy } = mountPerfHistogramControl({ container: c });
        expect(c.querySelector("[data-perf-histogram]")).not.toBeNull();
        destroy();
        destroy();
        destroy();
        expect(c.querySelector("[data-perf-histogram]")).toBeNull();
    });

    it("click → capturing state immediately disables button", async () => {
        const c = makeContainer();
        // Keep fetch hung indefinitely so we observe the capturing
        // state without it resolving.
        const fetcher = vi.fn(() => new Promise(() => {}));
        const { destroy } = mountPerfHistogramControl({
            container: c,
            apiFetcher: fetcher,
            waitMs: 50,
        });
        const btn = c.querySelector("[data-perf-histogram-capture]");
        btn.click();
        // Microtask flush so the click handler runs renderState.
        await Promise.resolve();
        expect(btn.disabled).toBe(true);
        expect(btn.textContent).toBe("Capturing 300 frames (~10s)…");
        destroy();
    });

    it("happy path: click → ready → text shown in <pre> + button re-enabled", async () => {
        const c = makeContainer();
        const fetcher = makeFetcherReturning({
            "/api/playback/perf/start": { ok: true, status: 204 },
            "/api/playback/perf/dump": {
                ok: true,
                status: 200,
                json: async () => ({ ready: true, text: "HISTOGRAM\nphase=paint p50=12ms" }),
            },
        });
        const { destroy } = mountPerfHistogramControl({
            container: c,
            apiFetcher: fetcher,
            waitMs: 0, // skip the real 12s wait in tests
        });
        const btn = c.querySelector("[data-perf-histogram-capture]");
        btn.click();
        // Flush enough microtasks for both fetches + json + the
        // sleep(0).
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        const textEl = c.querySelector("[data-perf-histogram-text]");
        expect(textEl.textContent).toBe("HISTOGRAM\nphase=paint p50=12ms");
        expect(btn.textContent).toBe("Capture again");
        expect(btn.disabled).toBe(false);
        destroy();
    });

    it("error path: 503 detail surfaces in the status text", async () => {
        const c = makeContainer();
        const fetcher = makeFetcherReturning({
            "/api/playback/perf/start": {
                ok: false,
                status: 503,
                json: async () => ({ detail: "MockRenderer in dev" }),
            },
        });
        const { destroy } = mountPerfHistogramControl({
            container: c,
            apiFetcher: fetcher,
            waitMs: 0,
        });
        c.querySelector("[data-perf-histogram-capture]").click();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        const statusEl = c.querySelector("[data-perf-histogram-status]");
        expect(statusEl.textContent).toMatch(/Capture failed.*MockRenderer in dev/);
        destroy();
    });

    it("copy button: invokes clipboardCopier with the rendered text", async () => {
        const c = makeContainer();
        const fetcher = makeFetcherReturning({
            "/api/playback/perf/start": { ok: true, status: 204 },
            "/api/playback/perf/dump": {
                ok: true,
                status: 200,
                json: async () => ({ ready: true, text: "copy-this" }),
            },
        });
        const copier = vi.fn(async () => true);
        const { destroy } = mountPerfHistogramControl({
            container: c,
            apiFetcher: fetcher,
            clipboardCopier: copier,
            waitMs: 0,
        });
        c.querySelector("[data-perf-histogram-capture]").click();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        const copyBtn = c.querySelector("[data-perf-histogram-copy]");
        copyBtn.click();
        await new Promise((r) => setTimeout(r, 0));

        expect(copier).toHaveBeenCalledWith("copy-this");
        expect(copyBtn.textContent).toBe("Copied ✓");
        destroy();
    });

    it("not_ready: shows retry-dump status + leaves text visible", async () => {
        const c = makeContainer();
        const fetcher = makeFetcherReturning({
            "/api/playback/perf/start": { ok: true, status: 204 },
            "/api/playback/perf/dump": {
                ok: true,
                status: 200,
                json: async () => ({ ready: false, text: "frames_remaining=10" }),
            },
        });
        const { destroy } = mountPerfHistogramControl({
            container: c,
            apiFetcher: fetcher,
            waitMs: 0,
        });
        c.querySelector("[data-perf-histogram-capture]").click();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        const btn = c.querySelector("[data-perf-histogram-capture]");
        const statusEl = c.querySelector("[data-perf-histogram-status]");
        expect(btn.textContent).toBe("Retry dump");
        expect(statusEl.textContent).toMatch(/still capturing/i);
        const textEl = c.querySelector("[data-perf-histogram-text]");
        expect(textEl.textContent).toBe("frames_remaining=10");
        destroy();
    });
});
