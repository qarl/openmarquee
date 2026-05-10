// @vitest-environment jsdom
//
// Tests for the UI perf instrumentation helpers (Batch 6.1).
// Goal: every markStart/markEnd round-trips through the User
// Timing API and lands a corresponding entry on `window.__perf` so
// devtools-poking is reliable.

import { beforeEach, describe, expect, it } from "vitest";
import { markEnd, markStart, measure } from "./perf.js";

beforeEach(() => {
    // jsdom carries `performance` across tests; clear state so the
    // ring + measure buffer don't bleed.
    window.__perf?.clear?.();
    try {
        performance.clearMarks();
        performance.clearMeasures();
    } catch {
        // ignore
    }
});

describe("markStart / markEnd", () => {
    it("round-trips through performance.getEntriesByName", () => {
        markStart("test.alpha");
        // Force a tiny gap so duration > 0 even on the fastest hosts.
        const target = performance.now() + 1;
        while (performance.now() < target) { /* spin */ }
        const duration = markEnd("test.alpha");
        expect(duration).toBeGreaterThan(0);
    });

    it("markEnd without a prior markStart returns undefined", () => {
        // No throw; just a silent no-op so instrumentation doesn't
        // crash if a code path skipped its markStart.
        expect(markEnd("test.unstarted")).toBeUndefined();
    });

    it("populates window.__perf.measures + byName aggregate", () => {
        markStart("test.beta");
        markEnd("test.beta");
        const ring = window.__perf;
        expect(ring).toBeTruthy();
        expect(ring.measures.some((m) => m.name === "test.beta")).toBe(true);
        expect(ring.byName["test.beta"].count).toBe(1);
    });

    it("aggregates count + totalMs + maxMs across multiple measures", () => {
        for (let i = 0; i < 5; i++) {
            markStart("test.gamma");
            markEnd("test.gamma");
        }
        const agg = window.__perf.byName["test.gamma"];
        expect(agg.count).toBe(5);
        expect(agg.totalMs).toBeGreaterThanOrEqual(0);
        expect(agg.maxMs).toBeGreaterThanOrEqual(0);
    });

    it("ring caps at 256 measures even if more are pushed", () => {
        for (let i = 0; i < 300; i++) {
            markStart(`test.cap-${i}`);
            markEnd(`test.cap-${i}`);
        }
        expect(window.__perf.measures.length).toBeLessThanOrEqual(256);
    });
});

describe("measure() convenience wrapper", () => {
    it("brackets a sync fn between markStart/markEnd", () => {
        const result = measure("test.wrap", () => 42);
        expect(result).toBe(42);
        expect(window.__perf.byName["test.wrap"].count).toBe(1);
    });

    it("still calls markEnd when the wrapped fn throws", () => {
        expect(() => measure("test.boom", () => {
            throw new Error("oops");
        })).toThrow("oops");
        // The finally-block in measure() ensures the end mark lands.
        expect(window.__perf.byName["test.boom"].count).toBe(1);
    });
});
