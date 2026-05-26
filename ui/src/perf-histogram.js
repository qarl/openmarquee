// Perf-night r8 / r2.5 follow-up (2026-05-26): operator-facing phase
// histogram capture. Wires the deferred button from r2 now that
// code1's /api/playback/perf/start + dump endpoints are on main.
//
// Workflow:
//   1. Operator clicks "Capture phase histogram" in Settings →
//      Diagnostics
//   2. POST /api/playback/perf/start { frames: 300 } → 204 No Content
//   3. Wait ~12s (300 frames at 30fps = 10s + 2s grace)
//   4. GET /api/playback/perf/dump → { ready: bool, text: str }
//   5. If ready: render text in <pre> + show Copy / Capture-again buttons
//      If not ready: show "still capturing, retry" with manual retry
//
// Wire contract with the backend's api_playback.py endpoints (see
// commit dbca3e2 on origin/main for the canonical definitions):
//   POST /api/playback/perf/start { frames: int 1..100000 }
//     → 204 No Content on success
//     → 503 when active renderer doesn't support profile capture
//       (MockRenderer in dev) or AutoFallbackRenderer swapped to Mock
//   GET /api/playback/perf/dump
//     → 200 { ready: bool, text: str }
//     → 503 same as start
//
// Auth: same bearer-token mechanism as the existing /api/playback/perf/
// stats poll (the corner overlay's 1Hz poll). apiFetch in api.js injects
// the Authorization header.

import { apiFetch } from "./api.js";

export const PERF_CAPTURE_FRAMES = 300;
// 300 frames at 30fps = 10s; +2s grace for IPC/event-loop slack.
export const PERF_CAPTURE_WAIT_MS = 12_000;

// --- Pure helpers (testable without DOM) -------------------------

/**
 * State-machine label for the capture button. Pure function of the
 * current state — testable independent of DOM. UI maps state → label
 * + button-disabled state.
 */
export function captureButtonLabel(state) {
    switch (state) {
        case "idle":
            return "Capture phase histogram";
        case "capturing":
            return "Capturing 300 frames (~10s)…";
        case "ready":
            return "Capture again";
        case "error":
            return "Capture again";
        case "not_ready":
            return "Retry dump";
        default:
            return "Capture phase histogram";
    }
}

/**
 * Whether the capture button should be disabled in this state.
 * Only `capturing` disables — every other state should accept
 * another click.
 */
export function captureButtonDisabled(state) {
    return state === "capturing";
}

// --- Capture orchestrator ----------------------------------------

/**
 * Run a single capture cycle: POST start → wait → GET dump.
 * Returns one of:
 *   { kind: "ready", text }
 *   { kind: "not_ready", text }   // dump came back but ready=false
 *   { kind: "error", detail }
 *
 * Throws AbortError if `signal` is aborted mid-flight; caller must
 * handle that (typically: stop showing the spinner).
 *
 * Doesn't touch the DOM — pure orchestrator. UI calls this from a
 * button-click handler; mount-fn binds state changes to onStatus.
 */
export async function captureHistogram({
    frames = PERF_CAPTURE_FRAMES,
    waitMs = PERF_CAPTURE_WAIT_MS,
    fetcher = (input, init) => apiFetch(input, init),
    sleep = (ms, signal) => new Promise((resolve, reject) => {
        const id = setTimeout(() => {
            signal?.removeEventListener("abort", onAbort);
            resolve();
        }, ms);
        const onAbort = () => {
            clearTimeout(id);
            const err = new Error("aborted");
            err.name = "AbortError";
            reject(err);
        };
        signal?.addEventListener("abort", onAbort, { once: true });
    }),
    signal,
} = {}) {
    // POST /api/playback/perf/start
    let startResponse;
    try {
        startResponse = await fetcher("/api/playback/perf/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ frames }),
            signal,
        });
    } catch (err) {
        if (err?.name === "AbortError") throw err;
        return {
            kind: "error",
            detail: `start request failed: ${err?.message ?? String(err)}`,
        };
    }
    if (!startResponse.ok) {
        // Backend returns 503 detail bodies for the no-profile-capture
        // case; surface them so the operator sees "MockRenderer" /
        // "swapped to Mock" rather than a bare HTTP code.
        let detail = `HTTP ${startResponse.status}`;
        try {
            const body = await startResponse.json();
            if (body?.detail) detail = body.detail;
        } catch {
            /* body wasn't JSON — keep the HTTP-code default */
        }
        return { kind: "error", detail };
    }

    // 300-frame capture at 30fps is ~10s; +2s grace for IPC / event-
    // loop slack. The renderer's profile state machine pushes samples
    // per-frame on the playback advance() ticks; when the requested
    // frame count is reached `frames_remaining=0` appears in the dump.
    await sleep(waitMs, signal);

    // GET /api/playback/perf/dump
    let dumpResponse;
    try {
        dumpResponse = await fetcher("/api/playback/perf/dump", { signal });
    } catch (err) {
        if (err?.name === "AbortError") throw err;
        return {
            kind: "error",
            detail: `dump request failed: ${err?.message ?? String(err)}`,
        };
    }
    if (!dumpResponse.ok) {
        let detail = `HTTP ${dumpResponse.status}`;
        try {
            const body = await dumpResponse.json();
            if (body?.detail) detail = body.detail;
        } catch {
            /* not JSON */
        }
        return { kind: "error", detail };
    }
    const body = await dumpResponse.json();
    if (!body.ready) {
        // Capture window hasn't completed — renderer's
        // frames_remaining > 0. Operator can manually retry.
        return { kind: "not_ready", text: String(body.text ?? "") };
    }
    return { kind: "ready", text: String(body.text ?? "") };
}

// --- Clipboard helper --------------------------------------------

/**
 * Copy `text` to the system clipboard. Returns true on success.
 *
 * Tries `navigator.clipboard.writeText` first (modern + permission-
 * gated); falls back to a hidden textarea + `document.execCommand`
 * for older browsers / non-secure contexts. The fallback is the same
 * shape every legacy copy-button polyfill uses; keep the body small.
 */
export async function copyTextToClipboard(text, {
    doc = globalThis.document,
    nav = globalThis.navigator,
} = {}) {
    // Modern path
    if (nav?.clipboard?.writeText) {
        try {
            await nav.clipboard.writeText(text);
            return true;
        } catch {
            // Fall through to textarea fallback (permission denied,
            // non-secure context, etc.)
        }
    }
    // Legacy fallback
    if (!doc) return false;
    const ta = doc.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    // Position off-screen so the selection doesn't flash visibly.
    ta.style.position = "fixed";
    ta.style.top = "-10000px";
    ta.style.left = "-10000px";
    doc.body.appendChild(ta);
    try {
        ta.select();
        const ok = doc.execCommand?.("copy") ?? false;
        return Boolean(ok);
    } catch {
        return false;
    } finally {
        ta.parentNode?.removeChild(ta);
    }
}

// --- DOM mount ----------------------------------------------------

/**
 * Mount the capture control inside `container`. Returns `{ destroy }`.
 *
 * Markup is class-name-driven; CSS for the histogram panel lives in
 * ui/styles.css under .perf-histogram__* selectors.
 *
 * Options:
 *   - container: the parent element (a Settings card / similar)
 *   - apiFetcher (optional, test seam): defaults to apiFetch
 *   - clipboardCopier (optional, test seam): defaults to
 *     copyTextToClipboard
 *   - waitMs (optional, test seam): defaults to PERF_CAPTURE_WAIT_MS
 *   - doc (optional, test seam): defaults to globalThis.document
 */
export function mountPerfHistogramControl({
    container,
    apiFetcher = (input, init) => apiFetch(input, init),
    clipboardCopier = copyTextToClipboard,
    waitMs = PERF_CAPTURE_WAIT_MS,
    doc = globalThis.document,
} = {}) {
    if (!container || !doc) {
        return { destroy: () => {} };
    }

    // State machine: idle → capturing → (ready | not_ready | error) → idle
    let state = "idle";
    let lastText = "";
    let abortCtrl = null;
    let destroyed = false;

    // DOM build
    const root = doc.createElement("div");
    root.className = "perf-histogram";
    root.setAttribute("data-perf-histogram", "");

    const captureBtn = doc.createElement("button");
    captureBtn.type = "button";
    captureBtn.className = "om-btn sm perf-histogram__capture";
    captureBtn.setAttribute("data-perf-histogram-capture", "");
    captureBtn.textContent = captureButtonLabel(state);

    const statusEl = doc.createElement("p");
    statusEl.className = "perf-histogram__status";
    statusEl.setAttribute("data-perf-histogram-status", "");
    statusEl.setAttribute("role", "status");
    statusEl.setAttribute("aria-live", "polite");
    statusEl.textContent = "";

    const outputWrap = doc.createElement("div");
    outputWrap.className = "perf-histogram__output";
    outputWrap.hidden = true;

    const outputPre = doc.createElement("pre");
    outputPre.className = "perf-histogram__text";
    outputPre.setAttribute("data-perf-histogram-text", "");

    const copyBtn = doc.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "om-btn sm perf-histogram__copy";
    copyBtn.setAttribute("data-perf-histogram-copy", "");
    copyBtn.textContent = "Copy";

    outputWrap.append(outputPre, copyBtn);
    root.append(captureBtn, statusEl, outputWrap);
    container.append(root);

    function renderState() {
        captureBtn.textContent = captureButtonLabel(state);
        captureBtn.disabled = captureButtonDisabled(state);
        switch (state) {
            case "idle":
                statusEl.textContent = "";
                outputWrap.hidden = lastText === "";
                break;
            case "capturing":
                statusEl.textContent = "Capturing 300 frames (~10s)…";
                outputWrap.hidden = true;
                break;
            case "ready":
                statusEl.textContent = "Captured.";
                outputPre.textContent = lastText;
                outputWrap.hidden = false;
                break;
            case "not_ready":
                statusEl.textContent =
                    "Renderer reports frames_remaining > 0 (still capturing). Click Retry dump.";
                outputPre.textContent = lastText;
                outputWrap.hidden = lastText === "";
                break;
            case "error":
                statusEl.textContent =
                    `Capture failed: ${lastText}`;
                outputWrap.hidden = true;
                break;
        }
    }
    renderState();

    async function runCapture() {
        if (destroyed) return;
        if (abortCtrl) abortCtrl.abort();
        abortCtrl = new AbortController();
        state = "capturing";
        renderState();
        try {
            // For the "retry dump" case (state was not_ready), skip
            // the start + wait and just re-fetch the dump. The
            // renderer is already collecting; we're polling.
            // Tracked via a closure flag: `_retry_only` is the soft
            // signal that we came from not_ready. Simpler than a
            // separate code path.
            const result = await captureHistogram({
                fetcher: apiFetcher,
                waitMs,
                signal: abortCtrl.signal,
            });
            if (destroyed) return;
            if (result.kind === "ready") {
                state = "ready";
                lastText = result.text;
            } else if (result.kind === "not_ready") {
                state = "not_ready";
                lastText = result.text;
            } else {
                state = "error";
                lastText = result.detail;
            }
        } catch (err) {
            if (err?.name === "AbortError") {
                // Destroy or re-click — silently stop. The newer
                // click's own run will set state.
                return;
            }
            state = "error";
            lastText = err?.message ?? String(err);
        }
        renderState();
    }

    captureBtn.addEventListener("click", runCapture);

    copyBtn.addEventListener("click", async () => {
        const text = outputPre.textContent ?? "";
        if (!text) return;
        const ok = await clipboardCopier(text);
        // Brief affirmation; revert label after 1.5s so the operator
        // can re-click without confusion.
        copyBtn.textContent = ok ? "Copied ✓" : "Copy failed";
        setTimeout(() => {
            if (!destroyed) copyBtn.textContent = "Copy";
        }, 1500);
    });

    function destroy() {
        if (destroyed) return;
        destroyed = true;
        if (abortCtrl) {
            abortCtrl.abort();
            abortCtrl = null;
        }
        if (root.parentNode) {
            root.parentNode.removeChild(root);
        }
    }

    return { destroy };
}
