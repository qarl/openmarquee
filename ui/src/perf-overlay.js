// Perf-night r2 (2026-05-26) — operator-facing perf overlay.
//
// Polls /api/playback/perf/stats every 1s, renders a small fixed-
// position corner overlay with:
//   - current fps (from the renderer's 30s ipc.soak window)
//   - session-cumulative frames-over-budget rate (% over 36ms)
//   - 60-sample sparkline of the cumulative rate
//   - threshold-coded color (green <5%, yellow <15%, red >=15%)
//
// Toggleable via the Settings panel; toggle persists in localStorage
// as `om.perf.show`. Poll is gated on (enabled && !document.hidden)
// so an idle background tab doesn't burn cycles.
//
// Wire contract with the backend (api_playback.py:RendererPerfStats)
// and the renderer (renderer/src/ipc_main.rs:PerfStatsJson). All
// three sides must move in lockstep when fields are added/renamed.

import { apiFetch } from "./api.js";

export const PERF_OVERLAY_TOGGLE_KEY = "om.perf.show";
export const PERF_OVERLAY_POLL_MS = 1000;
export const PERF_SPARKLINE_SAMPLES = 60;

// --- Pure helpers (testable without DOM) -------------------------

/**
 * Threshold-to-color mapping per the QA-greenlit r2 spec:
 *   < 5%   → green
 *   < 15%  → yellow
 *   >= 15% → red
 *
 * Boundaries are strict-less-than at 5 and 15. A rate of exactly 5
 * lands in yellow; a rate of exactly 15 lands in red. Mirrors the
 * "exact-boundary lives at the next tier" convention from r1's
 * frame_pacing.rs over_budget_ms strict-> compare.
 *
 * NaN / Infinity / negatives clamp to green (the safest visual
 * default — "we don't have valid data, don't panic the operator").
 */
export function perfColor(overBudgetPct) {
    if (!Number.isFinite(overBudgetPct) || overBudgetPct < 0) return "green";
    if (overBudgetPct < 5) return "green";
    if (overBudgetPct < 15) return "yellow";
    return "red";
}

/**
 * Session-cumulative over-budget rate as a percentage. Divides
 * frames_over_budget_total by frames_observed_total. Returns 0 when
 * observed_total is 0 (no data yet — common in the first ipc.soak
 * window before any frames have been observed).
 */
export function cumulativeOverBudgetPct(observedTotal, overBudgetTotal) {
    if (!observedTotal || observedTotal <= 0) return 0;
    const pct = (overBudgetTotal / observedTotal) * 100;
    return Number.isFinite(pct) ? pct : 0;
}

/**
 * Best-effort "frames over budget per minute" from a window's worth
 * of data. The renderer emits totals; we convert to a per-minute
 * rate by scaling the per-window delta. With a 30s window, a delta
 * of 12 becomes 24/min.
 *
 * Returns 0 when no fresh window has arrived yet (delta = 0).
 */
export function overBudgetPerMinute(currTotal, prevTotal, windowSeconds) {
    const delta = Math.max(0, currTotal - prevTotal);
    if (windowSeconds <= 0) return 0;
    return Math.round((delta * 60) / windowSeconds);
}

/**
 * Push a sample into a fixed-size ring buffer, evicting the oldest
 * if full. Mutates `ring` in place; returns the new length.
 *
 * Pure for test (apart from the in-place mutation, which the caller
 * owns the array of). The sparkline reuses one Array allocated at
 * mount; this is the only mutation path.
 */
export function ringPush(ring, value, capacity) {
    ring.push(value);
    while (ring.length > capacity) ring.shift();
    return ring.length;
}

/**
 * Polyline points for the sparkline given a ring of percentages.
 * Pure data; returns an array of {x, y} pairs that the caller can
 * stroke onto a Canvas2D context.
 *
 * Coordinate system: x in [0, width], y in [0, height] with y=0 at
 * top (canvas convention). Higher percentage = lower y (climbs up
 * the chart).
 *
 * Allocation note (QA r2 hard rule #3): this DOES allocate one
 * Array of N small {x,y} objects per call. At 1Hz poll cadence
 * with N≤60 that's a negligible GC load compared with the canvas
 * stroke + JSON.parse cost upstream — V8's young-gen scavenger
 * eats it for breakfast. If hard-rule-strict reading ever forces
 * zero-alloc, pre-allocate two parallel Float32Arrays at mount and
 * reuse, but the readability cost outweighs the perf win here.
 */
export function sparklinePoints(ring, width, height, maxPct = 100) {
    const n = ring.length;
    if (n === 0) return [];
    if (n === 1) {
        // One sample → single point at right edge.
        const y = height - (Math.min(maxPct, Math.max(0, ring[0])) / maxPct) * (height - 2) - 1;
        return [{ x: width, y }];
    }
    const stepX = width / (n - 1);
    const out = new Array(n);
    for (let i = 0; i < n; i++) {
        const clamped = Math.min(maxPct, Math.max(0, ring[i]));
        out[i] = {
            x: i * stepX,
            y: height - (clamped / maxPct) * (height - 2) - 1,
        };
    }
    return out;
}

// --- localStorage toggle ----------------------------------------

export function isPerfOverlayEnabled() {
    try {
        return globalThis.localStorage?.getItem(PERF_OVERLAY_TOGGLE_KEY) === "1";
    } catch {
        return false;
    }
}

export function setPerfOverlayEnabled(enabled) {
    try {
        if (enabled) {
            globalThis.localStorage?.setItem(PERF_OVERLAY_TOGGLE_KEY, "1");
        } else {
            globalThis.localStorage?.removeItem(PERF_OVERLAY_TOGGLE_KEY);
        }
    } catch {
        // localStorage may be disabled (private browsing, etc.). The
        // overlay degrades to "always off" — acceptable since the
        // toggle isn't a load-bearing feature.
    }
}

// --- DOM mount --------------------------------------------------

/**
 * Build the overlay DOM. Returns { rootEl, fpsEl, overEl, ageEl, canvas, ctx }.
 *
 * Markup is kept minimal + class-name-driven. The CSS lives in
 * ui/styles.css (sibling commit) under .perf-overlay__* selectors
 * so this module only owns the DOM shape, not the visual styling.
 * Splitting JS-vs-CSS means a future tweak to colors or spacing
 * doesn't churn this file.
 */
function buildOverlayDom(doc) {
    const root = doc.createElement("div");
    root.className = "perf-overlay perf-overlay--green";
    root.setAttribute("data-perf-overlay", "");

    const header = doc.createElement("div");
    header.className = "perf-overlay__header";
    const title = doc.createElement("span");
    title.className = "perf-overlay__title";
    title.textContent = "Perf";
    const age = doc.createElement("span");
    age.className = "perf-overlay__age";
    age.setAttribute("data-perf-age", "");
    age.textContent = "—";
    header.append(title, age);

    const body = doc.createElement("div");
    body.className = "perf-overlay__body";

    const fpsMetric = doc.createElement("div");
    fpsMetric.className = "perf-overlay__metric";
    const fpsLabel = doc.createElement("span");
    fpsLabel.className = "perf-overlay__label";
    fpsLabel.textContent = "fps";
    const fpsValue = doc.createElement("span");
    fpsValue.className = "perf-overlay__value";
    fpsValue.setAttribute("data-perf-fps", "");
    fpsValue.textContent = "—";
    fpsMetric.append(fpsLabel, fpsValue);

    const overMetric = doc.createElement("div");
    overMetric.className = "perf-overlay__metric";
    const overLabel = doc.createElement("span");
    overLabel.className = "perf-overlay__label";
    overLabel.textContent = "over/min";
    const overValue = doc.createElement("span");
    overValue.className = "perf-overlay__value";
    overValue.setAttribute("data-perf-over", "");
    overValue.textContent = "—";
    overMetric.append(overLabel, overValue);

    const canvas = doc.createElement("canvas");
    canvas.className = "perf-overlay__sparkline";
    canvas.setAttribute("data-perf-sparkline", "");
    canvas.width = 120;
    canvas.height = 32;

    body.append(fpsMetric, overMetric, canvas);
    root.append(header, body);

    return {
        rootEl: root,
        fpsEl: fpsValue,
        overEl: overValue,
        ageEl: age,
        canvas,
        // getContext is the only stateful op here; cached once per
        // mount per the image-upload.js reuse pattern. JSDOM stubs
        // this to a no-op object so tests don't allocate per-frame.
        ctx: canvas.getContext("2d"),
    };
}

function repaintSparkline(ctx, canvas, ring, color) {
    if (!ctx) return; // jsdom test path may stub getContext to null.
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    const pts = sparklinePoints(ring, w, h);
    if (pts.length === 0) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) {
        ctx.lineTo(pts[i].x, pts[i].y);
    }
    ctx.stroke();
}

function ageString(timestampUnixS, nowMs) {
    if (!timestampUnixS) return "no data";
    const ageS = Math.max(0, Math.round(nowMs / 1000) - timestampUnixS);
    if (ageS < 60) return `${ageS}s ago`;
    const ageM = Math.floor(ageS / 60);
    return `${ageM}m ago`;
}

/**
 * Mount the perf overlay. Returns `{ destroy }`.
 *
 * Options:
 *   - parent: the DOM element to append the overlay to (default
 *     `document.body` for fixed-position floating mode)
 *   - doc / nowFn / docHiddenFn / fetcher: test injection seams
 *     (default to globals)
 */
export function mountPerfOverlay({
    parent,
    doc = globalThis.document,
    nowFn = () => Date.now(),
    docHiddenFn = () => Boolean(doc?.hidden),
    fetcher = (signal) => apiFetch("/api/playback/perf/stats", { signal }),
    pollMs = PERF_OVERLAY_POLL_MS,
} = {}) {
    if (!doc) {
        // Headless / non-DOM context — no-op destroy.
        return { destroy: () => {} };
    }
    const mountParent = parent || doc.body;
    const elements = buildOverlayDom(doc);
    mountParent.appendChild(elements.rootEl);

    // Ring buffer of session-cumulative over-budget percentages.
    // Allocated once at mount; reused across all polls (no per-poll
    // allocations beyond a single number push). Capacity matches the
    // sparkline sample target.
    const ring = [];
    let lastSnapshot = null; // { observed_total, over_budget_total, timestamp_unix_s }
    let abortCtrl = null;
    let pollTimer = null;
    let destroyed = false;

    async function pollOnce() {
        if (destroyed) return;
        if (docHiddenFn()) {
            // Backgrounded tab — skip this iteration. The next visible
            // tick picks up where we left off. Per QA r2 hard rule:
            // poll doesn't run when panel hidden.
            return;
        }
        if (abortCtrl) abortCtrl.abort();
        abortCtrl = new AbortController();
        let stats;
        try {
            const response = await fetcher(abortCtrl.signal);
            if (!response.ok) {
                renderNoData(elements, "no data");
                return;
            }
            stats = await response.json();
        } catch (err) {
            // AbortError on unmount → silent. Other fetch errors →
            // render "no data" rather than throwing into the timer.
            if (err?.name === "AbortError") return;
            renderNoData(elements, "no data");
            return;
        }
        if (destroyed) return;

        // Drop a sample into the ring. Push the cumulative rate
        // every poll so the sparkline has 1Hz resolution even though
        // the underlying ipc.soak window is 30s.
        const obs = stats.frames_observed_total ?? 0;
        const over = stats.frames_over_budget_total ?? 0;
        const cumPct = cumulativeOverBudgetPct(obs, over);
        ringPush(ring, cumPct, PERF_SPARKLINE_SAMPLES);

        // Per-minute over-budget rate from the renderer's window
        // (window_s ~30). Only computes a meaningful delta when a
        // FRESH window has arrived; otherwise reports 0.
        let perMin = 0;
        const ws = stats.window_s ?? 0;
        if (
            lastSnapshot &&
            lastSnapshot.timestamp_unix_s !== stats.timestamp_unix_s
        ) {
            perMin = overBudgetPerMinute(
                over,
                lastSnapshot.over_budget_total,
                ws,
            );
        }
        lastSnapshot = {
            observed_total: obs,
            over_budget_total: over,
            timestamp_unix_s: stats.timestamp_unix_s,
        };

        // Color picks from the cumulative rate (sparkline-aligned).
        const color = perfColor(cumPct);
        const colorCssClass = `perf-overlay--${color}`;
        elements.rootEl.className = `perf-overlay ${colorCssClass}`;

        elements.fpsEl.textContent = (stats.fps_avg ?? 0).toFixed(1);
        elements.overEl.textContent = String(perMin);
        elements.ageEl.textContent = ageString(stats.timestamp_unix_s, nowFn());

        // Stroke color matches the threshold tier for at-a-glance
        // visual consistency between background + sparkline.
        const strokeColor = color === "green"
            ? "#3a9b3a"
            : color === "yellow"
              ? "#c4a000"
              : "#c43a3a";
        repaintSparkline(elements.ctx, elements.canvas, ring, strokeColor);
    }

    function renderNoData(els, label) {
        els.fpsEl.textContent = "—";
        els.overEl.textContent = "—";
        els.ageEl.textContent = label;
    }

    // Kick off the first poll immediately (don't wait pollMs for the
    // operator's first frame of data), then schedule.
    pollOnce();
    pollTimer = setInterval(pollOnce, pollMs);

    function destroy() {
        if (destroyed) return;
        destroyed = true;
        if (pollTimer !== null) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
        if (abortCtrl) {
            abortCtrl.abort();
            abortCtrl = null;
        }
        if (elements.rootEl.parentNode) {
            elements.rootEl.parentNode.removeChild(elements.rootEl);
        }
    }

    return { destroy };
}
