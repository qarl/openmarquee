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

// 15s cadence — qarl 2026-04-30: "they can update once every 15 seconds."
// Faster ticking is wasted work at thumbnail scale; the current value
// is shown at attach so operators see something fresh immediately, and
// 15s drift on a tile preview is well under what they'd notice.
const TICK_INTERVAL_MS = 15_000;

// Tile re-renders replace the parent's innerHTML; orphaned overlay
// nodes get GC'd, but they linger in OVERLAYS until we notice they've
// left the document. Two sites prune lazily: every tick + at attach
// time (so the registry doesn't carry dead refs across the 15s gap).
function pruneOrphans() {
    for (const el of [...OVERLAYS]) {
        if (!el.isConnected) OVERLAYS.delete(el);
    }
}

// Single source of truth for tickTimer state. Called after any change
// to OVERLAYS.size (attach, tick-time prune) so start/stop decisions
// have one obvious hook -- future detach paths only need to call this.
function syncTicker() {
    if (OVERLAYS.size === 0 && tickTimer !== null) {
        clearInterval(tickTimer);
        tickTimer = null;
    } else if (OVERLAYS.size > 0 && tickTimer === null) {
        tickTimer = setInterval(tickOnce, TICK_INTERVAL_MS);
    }
}

function renderOne(el, now) {
    const text = formatAutoText(
        el.dataset.autoMode,
        el.dataset.autoFormat || null,
        now,
    );
    if (text) el.textContent = text;
}

function tickOnce() {
    pruneOrphans();
    const now = new Date();
    for (const el of OVERLAYS) renderOne(el, now);
    syncTicker();
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
    // §5.10a v3: auto_mode + auto_format moved from the slide root onto
    // each TextLayer. Read off layer[0] for the thumbnail token — multi-
    // layer auto-text would need a layered overlay, but that's out of
    // scope for the tile preview. Tests that pass auto_mode at the slide
    // root keep working via the second-arg fallback.
    const layer = item?.text_layers?.[0];
    const autoMode = layer?.auto_mode ?? item?.auto_mode ?? null;
    const autoFormat = layer?.auto_format ?? item?.auto_format ?? null;
    if (!autoMode) return null;
    const overlay = document.createElement("div");
    overlay.className = "om-auto-text-overlay";
    overlay.dataset.autoMode = autoMode;
    if (autoFormat) overlay.dataset.autoFormat = autoFormat;
    parent.appendChild(overlay);
    OVERLAYS.add(overlay);
    // Initial render goes through the same renderOne the ticker uses
    // -- guarantees at-attach value matches the ticker's per-frame
    // value, no risk of silent divergence if formatAutoText's call
    // shape changes.
    renderOne(overlay, new Date());
    pruneOrphans();
    syncTicker();
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
