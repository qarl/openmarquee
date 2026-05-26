// @vitest-environment jsdom
//
// Perf-night r2 (2026-05-26). Tests for ui/src/perf-overlay.js.
//
// Splits into:
//   1. Pure-data helpers (perfColor, cumulativeOverBudgetPct,
//      overBudgetPerMinute, ringPush, sparklinePoints). Node-env
//      compatible, no DOM. These are the dispatch's hard-rule #5
//      ("tests for the threshold-to-color mapping") plus
//      defense-in-depth around the rate math.
//   2. Mount/destroy lifecycle. Uses jsdom + injected fakes for
//      apiFetch/document.hidden/Date.now so the test runs
//      hermetically. These cover hard rules #1, #2, #4.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    PERF_OVERLAY_TOGGLE_KEY,
    PERF_SPARKLINE_SAMPLES,
    cumulativeOverBudgetPct,
    isPerfOverlayEnabled,
    mountPerfOverlay,
    overBudgetPerMinute,
    perfColor,
    ringPush,
    setPerfOverlayEnabled,
    sparklinePoints,
} from "./perf-overlay.js";

beforeEach(() => {
    document.body.innerHTML = "";
    try {
        localStorage.removeItem(PERF_OVERLAY_TOGGLE_KEY);
    } catch {
        // noop in environments without localStorage.
    }
});

afterEach(() => {
    vi.restoreAllMocks();
});

// pollOnce() inside the module has two awaits (fetcher + response.json()),
// each producing async-fn microtask hops. A single Promise.resolve()
// flush isn't enough to see the resulting DOM mutations. A short
// setTimeout flushes both the microtask queue AND the macrotask queue
// reliably, regardless of how many internal awaits the impl uses.
function flushAsync() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

// =====================================================================
// 1. Pure-data helpers
// =====================================================================

describe("perfColor threshold mapping (QA r2 hard rule #5)", () => {
    // Boundaries: green < 5, yellow [5, 15), red >= 15.

    it("0% is green", () => {
        expect(perfColor(0)).toBe("green");
    });

    it("4.99% is green (just under 5% threshold)", () => {
        expect(perfColor(4.99)).toBe("green");
    });

    it("exactly 5% is yellow (boundary lives at the next tier)", () => {
        expect(perfColor(5)).toBe("yellow");
    });

    it("10% is yellow (mid-range)", () => {
        expect(perfColor(10)).toBe("yellow");
    });

    it("14.99% is yellow (just under 15% threshold)", () => {
        expect(perfColor(14.99)).toBe("yellow");
    });

    it("exactly 15% is red (boundary lives at the next tier)", () => {
        expect(perfColor(15)).toBe("red");
    });

    it("100% is red", () => {
        expect(perfColor(100)).toBe("red");
    });

    it("NaN clamps to green (no-data default)", () => {
        expect(perfColor(NaN)).toBe("green");
    });

    it("Infinity clamps to green (no-data default)", () => {
        expect(perfColor(Infinity)).toBe("green");
    });

    it("negative clamps to green", () => {
        expect(perfColor(-1)).toBe("green");
    });
});

describe("cumulativeOverBudgetPct", () => {
    it("returns 0 when observed_total is 0 (pre-first-frame)", () => {
        expect(cumulativeOverBudgetPct(0, 0)).toBe(0);
    });

    it("returns 0 when observed_total is negative (defensive)", () => {
        expect(cumulativeOverBudgetPct(-100, 50)).toBe(0);
    });

    it("returns expected percentage for normal data", () => {
        // 100 over / 1000 observed = 10%
        expect(cumulativeOverBudgetPct(1000, 100)).toBe(10);
    });

    it("returns 0% when no over-budget frames yet", () => {
        expect(cumulativeOverBudgetPct(1000, 0)).toBe(0);
    });

    it("clamps Infinity to 0 (defensive)", () => {
        // Manual construction: over/observed = Infinity if observed
        // is somehow nonpositive after the early-return guard.
        // Real-world this can't happen with u64 counters, but the
        // fn must not propagate Infinity to perfColor.
        expect(cumulativeOverBudgetPct(0.00001, Number.MAX_VALUE)).toBe(0);
    });
});

describe("overBudgetPerMinute", () => {
    it("scales 30s window delta to per-minute", () => {
        // window=30s, delta=12 → 12 * 60 / 30 = 24
        expect(overBudgetPerMinute(112, 100, 30)).toBe(24);
    });

    it("scales 60s window delta to per-minute (identity)", () => {
        // window=60s, delta=10 → 10 * 60 / 60 = 10
        expect(overBudgetPerMinute(110, 100, 60)).toBe(10);
    });

    it("returns 0 when delta is 0 (no fresh window)", () => {
        expect(overBudgetPerMinute(100, 100, 30)).toBe(0);
    });

    it("clamps negative delta to 0 (counter rollback shouldn't happen)", () => {
        expect(overBudgetPerMinute(100, 110, 30)).toBe(0);
    });

    it("returns 0 when window is 0 (no division by zero)", () => {
        expect(overBudgetPerMinute(100, 50, 0)).toBe(0);
    });

    it("rounds to integer", () => {
        // window=30s, delta=5 → 5 * 60 / 30 = 10
        expect(overBudgetPerMinute(105, 100, 30)).toBe(10);
        // window=45s, delta=5 → 5 * 60 / 45 = 6.66 → 7
        expect(overBudgetPerMinute(105, 100, 45)).toBe(7);
    });
});

describe("ringPush evicts oldest at capacity", () => {
    it("appends below capacity", () => {
        const ring = [];
        ringPush(ring, 1, 3);
        ringPush(ring, 2, 3);
        expect(ring).toEqual([1, 2]);
    });

    it("evicts at capacity (FIFO)", () => {
        const ring = [];
        ringPush(ring, 1, 3);
        ringPush(ring, 2, 3);
        ringPush(ring, 3, 3);
        ringPush(ring, 4, 3); // evicts 1
        expect(ring).toEqual([2, 3, 4]);
    });

    it("returns the post-push length", () => {
        const ring = [];
        expect(ringPush(ring, 1, 3)).toBe(1);
        expect(ringPush(ring, 2, 3)).toBe(2);
        expect(ringPush(ring, 3, 3)).toBe(3);
        expect(ringPush(ring, 4, 3)).toBe(3); // capped
    });
});

describe("sparklinePoints layout", () => {
    it("returns empty array for empty ring", () => {
        expect(sparklinePoints([], 100, 32)).toEqual([]);
    });

    it("returns single right-edge point for one sample", () => {
        const pts = sparklinePoints([50], 100, 32);
        expect(pts.length).toBe(1);
        expect(pts[0].x).toBe(100);
        // 50% rate at 32px tall → y ≈ height - 0.5 * (h-2) - 1 = 32 - 15 - 1 = 16
        expect(pts[0].y).toBeCloseTo(16, 0);
    });

    it("spaces N samples evenly across width", () => {
        const pts = sparklinePoints([0, 0, 0, 0, 0], 100, 32);
        expect(pts.length).toBe(5);
        expect(pts[0].x).toBe(0);
        expect(pts[4].x).toBe(100);
        // All 0% → y at the bottom (height-1)
        for (const p of pts) {
            expect(p.y).toBeCloseTo(31, 0);
        }
    });

    it("higher values produce lower y (canvas y=0 at top)", () => {
        const pts = sparklinePoints([10, 80], 100, 32);
        expect(pts[0].y).toBeGreaterThan(pts[1].y); // 80% is higher up
    });

    it("clamps over-100 to maxPct cap", () => {
        const pts = sparklinePoints([150], 100, 32, 100);
        // Clamped to 100 → top of chart (y near 1)
        expect(pts[0].y).toBeCloseTo(1, 0);
    });
});

// =====================================================================
// 2. localStorage toggle (QA r2 hard rule #4)
// =====================================================================

describe("perfOverlay localStorage toggle", () => {
    it("defaults to disabled", () => {
        expect(isPerfOverlayEnabled()).toBe(false);
    });

    it("setEnabled(true) → isEnabled() returns true", () => {
        setPerfOverlayEnabled(true);
        expect(isPerfOverlayEnabled()).toBe(true);
    });

    it("setEnabled(false) clears the key", () => {
        setPerfOverlayEnabled(true);
        setPerfOverlayEnabled(false);
        expect(isPerfOverlayEnabled()).toBe(false);
        // Underlying key should be removed (not just set to "0")
        expect(localStorage.getItem(PERF_OVERLAY_TOGGLE_KEY)).toBe(null);
    });
});

// =====================================================================
// 3. Mount/destroy lifecycle (QA r2 hard rules #1, #2)
// =====================================================================

function makeFakeFetcher(stats) {
    return vi.fn(async (signal) => {
        if (signal?.aborted) {
            const err = new Error("aborted");
            err.name = "AbortError";
            throw err;
        }
        return {
            ok: true,
            json: async () => stats,
        };
    });
}

describe("mountPerfOverlay lifecycle", () => {
    it("creates the overlay DOM under parent", () => {
        const fetcher = makeFakeFetcher({
            window_s: 30,
            frames_observed_total: 100,
            frames_over_budget_total: 1,
            fps_avg: 29.8,
            timestamp_unix_s: 1748275200,
        });
        const { destroy } = mountPerfOverlay({
            parent: document.body,
            fetcher,
            pollMs: 1000,
            docHiddenFn: () => false,
            nowFn: () => 1748275205000,
        });
        const root = document.querySelector("[data-perf-overlay]");
        expect(root).not.toBeNull();
        destroy();
    });

    it("destroy() removes the overlay from the DOM", () => {
        const fetcher = makeFakeFetcher({
            window_s: 30,
            frames_observed_total: 100,
            frames_over_budget_total: 1,
            fps_avg: 29.8,
            timestamp_unix_s: 1748275200,
        });
        const { destroy } = mountPerfOverlay({
            parent: document.body,
            fetcher,
            pollMs: 1000,
            docHiddenFn: () => false,
            nowFn: () => 1748275205000,
        });
        expect(document.querySelector("[data-perf-overlay]")).not.toBeNull();
        destroy();
        expect(document.querySelector("[data-perf-overlay]")).toBeNull();
    });

    it("destroy() is idempotent", () => {
        const fetcher = makeFakeFetcher({
            window_s: 30,
            frames_observed_total: 100,
            frames_over_budget_total: 1,
            fps_avg: 29.8,
            timestamp_unix_s: 1748275200,
        });
        const { destroy } = mountPerfOverlay({
            parent: document.body,
            fetcher,
            pollMs: 1000,
            docHiddenFn: () => false,
            nowFn: () => 1748275205000,
        });
        expect(() => {
            destroy();
            destroy();
            destroy();
        }).not.toThrow();
    });

    it("first poll fires immediately on mount (no pollMs delay for the first frame)", async () => {
        const fetcher = makeFakeFetcher({
            window_s: 30,
            frames_observed_total: 100,
            frames_over_budget_total: 1,
            fps_avg: 29.8,
            timestamp_unix_s: 1748275200,
        });
        const { destroy } = mountPerfOverlay({
            parent: document.body,
            fetcher,
            pollMs: 1000,
            docHiddenFn: () => false,
            nowFn: () => 1748275205000,
        });
        // Microtask-flush so the async pollOnce sees its Promise.
        await flushAsync();
        expect(fetcher).toHaveBeenCalledTimes(1);
        destroy();
    });

    it("skips fetch when document.hidden=true (QA r2 hard rule #1)", async () => {
        const fetcher = vi.fn();
        const { destroy } = mountPerfOverlay({
            parent: document.body,
            fetcher,
            pollMs: 1000,
            docHiddenFn: () => true, // tab backgrounded
            nowFn: () => 1748275205000,
        });
        await flushAsync();
        expect(fetcher).not.toHaveBeenCalled();
        destroy();
    });

    it("destroy() aborts an in-flight fetch (QA r2 hard rule #2)", async () => {
        let capturedSignal = null;
        const fetcher = vi.fn(async (signal) => {
            capturedSignal = signal;
            // Never resolves — simulates a stuck request.
            return new Promise(() => {});
        });
        const { destroy } = mountPerfOverlay({
            parent: document.body,
            fetcher,
            pollMs: 1000,
            docHiddenFn: () => false,
            nowFn: () => 1748275205000,
        });
        await flushAsync();
        expect(capturedSignal).not.toBeNull();
        expect(capturedSignal.aborted).toBe(false);
        destroy();
        expect(capturedSignal.aborted).toBe(true);
    });

    it("renders threshold-coded color class on the root element", async () => {
        const fetcher = makeFakeFetcher({
            window_s: 30,
            frames_observed_total: 1000,
            frames_over_budget_total: 200, // 20% — red
            fps_avg: 24.0,
            timestamp_unix_s: 1748275200,
        });
        const { destroy } = mountPerfOverlay({
            parent: document.body,
            fetcher,
            pollMs: 1000,
            docHiddenFn: () => false,
            nowFn: () => 1748275205000,
        });
        await flushAsync();
        const root = document.querySelector("[data-perf-overlay]");
        expect(root.className).toContain("perf-overlay--red");
        destroy();
    });

    it("renders 'no data' state on first 503", async () => {
        const fetcher = vi.fn(async () => ({
            ok: false,
            json: async () => ({ detail: "perf stats sidecar not yet written" }),
        }));
        const { destroy } = mountPerfOverlay({
            parent: document.body,
            fetcher,
            pollMs: 1000,
            docHiddenFn: () => false,
            nowFn: () => 1748275205000,
        });
        await flushAsync();
        const ageEl = document.querySelector("[data-perf-age]");
        expect(ageEl.textContent).toBe("no data");
        destroy();
    });
});

// =====================================================================
// 4. PERF_SPARKLINE_SAMPLES sanity — the dispatch said 60s @ 1Hz
// =====================================================================

describe("PERF_SPARKLINE_SAMPLES", () => {
    it("is 60 (matches 60s at 1Hz poll cadence)", () => {
        expect(PERF_SPARKLINE_SAMPLES).toBe(60);
    });
});
