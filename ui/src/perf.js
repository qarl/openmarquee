// UI perf instrumentation (Batch 6.1).
//
// Thin wrappers around the browser's User Timing API
// (performance.mark / measure). Goal: any hot top-level path
// (inline-preview tick, playlist-track refresh, editor list-content
// fetch) can bracket its work with `markStart(name)` /
// `markEnd(name)` and the measure shows up in DevTools' Performance
// panel AND on `window.__perf` for ad-hoc inspection without an
// extension installed.
//
// The aggregate ring on `window.__perf` is bounded so a long-running
// preview tab can't leak memory the way an unbounded performance
// entry buffer would. Devs query via console; nothing in the build
// pipeline reads window.__perf, so dropping it later is free.

const MARK_PREFIX = "om/";
const MAX_AGGREGATES = 256;

function ensureGlobalRing() {
    if (typeof window === "undefined") return null;
    if (!window.__perf) {
        window.__perf = {
            measures: [],
            byName: {},
            clear() {
                this.measures = [];
                this.byName = {};
            },
        };
    }
    return window.__perf;
}

/**
 * Stamp the start of a perf measurement. Pairs with markEnd(name).
 * No-op when performance.mark isn't available (jsdom on older nodes,
 * stripped-down environments).
 */
export function markStart(name) {
    if (typeof performance === "undefined" || !performance.mark) return;
    try {
        performance.mark(`${MARK_PREFIX}${name}:start`);
    } catch {
        // mark() can throw on duplicate names; the User Timing L3
        // spec allows replacing, but older shims still error. Silent
        // failure is OK -- instrumentation shouldn't crash a paint.
    }
}

/**
 * Close a perf measurement started by markStart. Pushes the measure
 * into the global ring (best-effort) and returns the measured
 * duration in ms (or undefined when the API is unavailable).
 */
export function markEnd(name) {
    if (typeof performance === "undefined" || !performance.mark) return undefined;
    const startMark = `${MARK_PREFIX}${name}:start`;
    const endMark = `${MARK_PREFIX}${name}:end`;
    const measureName = `${MARK_PREFIX}${name}`;
    try {
        performance.mark(endMark);
        performance.measure(measureName, startMark, endMark);
    } catch {
        // Most often: start mark missing (caller forgot markStart).
        return undefined;
    }
    const entries = performance.getEntriesByName(measureName, "measure");
    const last = entries[entries.length - 1];
    if (!last) return undefined;

    const ring = ensureGlobalRing();
    if (ring) {
        ring.measures.push({
            name,
            startTime: last.startTime,
            duration: last.duration,
        });
        if (ring.measures.length > MAX_AGGREGATES) {
            ring.measures.shift();
        }
        // Aggregate per-name: count, total, max -- the common shape
        // a console-poking dev wants to see.
        const agg = ring.byName[name] || { count: 0, totalMs: 0, maxMs: 0 };
        agg.count += 1;
        agg.totalMs += last.duration;
        if (last.duration > agg.maxMs) agg.maxMs = last.duration;
        ring.byName[name] = agg;
    }

    // Cleanup so the browser's User Timing buffer doesn't grow
    // unbounded across long-running tabs.
    try {
        performance.clearMarks(startMark);
        performance.clearMarks(endMark);
        performance.clearMeasures(measureName);
    } catch {
        // ignore
    }
    return last.duration;
}

/**
 * Run `fn` between markStart/markEnd. Convenience for sync work.
 * Async callers should call markStart/markEnd manually since wrapping
 * with await would still time the work but the syntactic affordance
 * here doesn't carry its weight.
 */
export function measure(name, fn) {
    markStart(name);
    try {
        return fn();
    } finally {
        markEnd(name);
    }
}
