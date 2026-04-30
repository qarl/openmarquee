// Auto-mode text overlay for tile thumbnails (slide-browser, playlist-
// track block, pallet tile). Mirrors inline-preview's overlay pattern:
// the static asset.png renders underneath, and a positioned div on top
// shows the live formatted token (HH:MM, weekday, etc.) — refreshed
// every second by a single shared ticker.
//
// Why a single ticker: a setInterval per overlay would mean N timers
// when an operator opens a long pallet of auto-mode slides. The ticker
// stays at most one regardless of overlay count, and stops itself when
// the last overlay is detached or its element leaves the DOM.

import { formatAutoText } from "./auto-format.js";

const OVERLAYS = new Set();
let tickTimer = null;

function tickOnce() {
    const now = new Date();
    for (const el of [...OVERLAYS]) {
        if (!el.isConnected) {
            // Tile re-renders replace the parent's innerHTML; orphaned
            // overlay nodes get GC'd, but they linger in OVERLAYS until
            // we notice they've left the document. Prune lazily here.
            OVERLAYS.delete(el);
            continue;
        }
        const text = formatAutoText(
            el.dataset.autoMode,
            el.dataset.autoFormat || null,
            now,
        );
        if (text) el.textContent = text;
    }
    if (OVERLAYS.size === 0) {
        clearInterval(tickTimer);
        tickTimer = null;
    }
}

// 15s cadence — qarl 2026-04-30: "they can update once every 15 seconds."
// Faster ticking is wasted work at thumbnail scale; the current value
// is shown at attach so operators see something fresh immediately, and
// 15s drift on a tile preview is well under what they'd notice.
const TICK_INTERVAL_MS = 15_000;

function ensureTicker() {
    if (tickTimer === null && OVERLAYS.size > 0) {
        tickTimer = setInterval(tickOnce, TICK_INTERVAL_MS);
    }
}

/**
 * Append an auto-mode overlay to `parent` if `item` has auto_mode set.
 * Returns the overlay element (or null when no overlay was attached).
 * Caller doesn't need to remember the return value — the shared ticker
 * prunes overlays whose elements have left the document.
 *
 * @param {HTMLElement} parent — the thumb wrap that should host the overlay.
 * @param {object} item — content item ({type, auto_mode, auto_format, ...}).
 */
export function attachAutoTextOverlay(parent, item) {
    if (!item || !item.auto_mode) return null;
    const overlay = document.createElement("div");
    overlay.className = "om-auto-text-overlay";
    overlay.dataset.autoMode = item.auto_mode;
    if (item.auto_format) overlay.dataset.autoFormat = item.auto_format;
    overlay.textContent =
        formatAutoText(item.auto_mode, item.auto_format || null, new Date()) ||
        "";
    parent.appendChild(overlay);
    OVERLAYS.add(overlay);
    // Tile re-renders leave orphaned overlays in the registry until the
    // next 15s tick prunes them. Sweep at attach time so the registry
    // doesn't carry dead refs across the gap.
    for (const el of [...OVERLAYS]) {
        if (!el.isConnected) OVERLAYS.delete(el);
    }
    ensureTicker();
    return overlay;
}

/** Test-only — drains the registry and stops the ticker between cases. */
export function _resetOverlayRegistryForTests() {
    OVERLAYS.clear();
    if (tickTimer !== null) {
        clearInterval(tickTimer);
        tickTimer = null;
    }
}

/** Test-only — exposes the live ticker tick path so tests can advance
 *  the displayed value without waiting on real wall-clock seconds. */
export function _tickOverlaysForTests() {
    tickOnce();
}
