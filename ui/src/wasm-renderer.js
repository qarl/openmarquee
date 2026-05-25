// Canvas2D-side fontdue text rasterizer. Wraps the renderer-wasm
// crate (compiled to wasm32 + post-processed by wasm-bindgen) so
// `paintLayer` can rasterize text via fontdue — the SAME rasterizer
// the Pi renderer uses — instead of `ctx.fillText` (Canvas-native AA
// curves + measureText kerning diverged in 4cbd08b; this closes that
// gap at the source).
//
// # Lifecycle
//
// 1. App boot calls `await initWasmRenderer()` ONCE. Module instances
//    the wasm + parses the registered font bytes; ~10-50 ms cold.
// 2. App boot calls `await registerFont(name, url)` for each font
//    the operator might use. TTF bytes fetched lazily (browser HTTP
//    cache handles re-fetches).
// 3. `rasterizeText(text, fontName, sizePx, color)` is synchronous
//    after init + register — returns ImageData ready to putImageData,
//    or null on (a) empty text, (b) all-whitespace text, (c) font
//    not registered.
//
// # esbuild
//
// The .wasm artifact is imported as a binary Uint8Array via
// `--loader:.wasm=binary` (configured in package.json). esbuild
// inlines the bytes into the bundle as base64; production builds
// don't need a separate fetch for the wasm. Bundle inflation is
// ~50 KiB gzipped (the wasm blob itself).
//
// # Test mocks
//
// jsdom doesn't ship Canvas2D OR WebAssembly URL resolution in a
// useful way. `vi.mock("./wasm-renderer.js")` in tests replaces
// `initWasmRenderer` / `rasterizeText` with stubs returning known-
// shape Uint8Arrays so tests verify the wire shape + positioning
// math without exercising the wasm itself.

import init, {
    register_font as wasmRegisterFont,
    rasterize_text_named as wasmRasterizeTextNamed,
} from "../../renderer-wasm/pkg/renderer_wasm.js";
import { LruByteCache } from "./lru-byte-cache.js";

// Runtime fetch of the .wasm artifact (NOT bundle-inlined).
//
// Original Phase 1a design tried esbuild's --loader:.wasm=binary to
// inline the bytes as base64 in main.js. That works for bundled code
// (main.js / welcome.js) but BREAKS the parity-harness.html path —
// the harness loads ESM modules directly via the static file server,
// and the browser rejects .wasm files as ES module imports under
// strict MIME-type checking (HTML spec). Fetching at runtime works
// for both: bundled main.js + unbundled parity-harness.html share
// the same code path. Dev: served via SimpleHTTPRequestHandler at
// REPO root. Production: copied to ui/dist/ by build script.
//
// Cost: one extra HTTP request at app boot (~100 KiB raw / 50 KiB
// gzipped) instead of inlining into the bundle. With HTTP/2 + the
// browser's HTTP cache, the steady-state cost is zero.
const WASM_URL = new URL(
    "../../renderer-wasm/pkg/renderer_wasm_bg.wasm",
    import.meta.url,
);

let initialized = false;
let initPromise = null;
const registeredFonts = new Set();

/**
 * Initialize the WASM module. Idempotent — subsequent calls return
 * the same in-flight Promise (so concurrent callers all await the
 * same init).
 *
 * @returns {Promise<void>}
 */
export function initWasmRenderer() {
    if (initialized) return Promise.resolve();
    if (initPromise) return initPromise;
    initPromise = init({ module_or_path: WASM_URL }).then(() => {
        initialized = true;
    }).catch((err) => {
        // Round 26: clear the cached promise BEFORE re-throwing so a
        // transient init failure (network blip fetching the .wasm,
        // MIME mismatch on a misconfigured server, OOM during
        // instantiation) doesn't poison the slot for the rest of
        // the page lifetime. Pre-fix every subsequent caller got
        // the same rejected promise and slides fell back to
        // ctx.fillText for the entire session even after the network
        // recovered. The original caller still receives the
        // rejection (re-thrown); the next caller gets a fresh
        // init attempt.
        initPromise = null;
        throw err;
    });
    return initPromise;
}

/**
 * Returns whether `initWasmRenderer` has resolved. Callers that need
 * to gate paint-path behavior (drawCanvas falling back to fillText
 * during cold boot) check this. Once `initialized = true`, the wasm
 * exports can be called synchronously.
 *
 * @returns {boolean}
 */
export function isWasmReady() {
    return initialized;
}

/**
 * Register a font in the wasm side's named-font registry. `name` is
 * the lookup key for subsequent `rasterizeText` calls (use the same
 * CSS font-family name the editor passes — e.g. "VT323", "Inter").
 * `urlOrBytes` is either an absolute / relative URL (fetched as
 * ArrayBuffer) or a pre-loaded Uint8Array. Idempotent — registering
 * twice with the same name replaces the cached parse (cheap, < few
 * KiB).
 *
 * @param {string} name
 * @param {string | Uint8Array} urlOrBytes
 * @returns {Promise<boolean>}
 */
export async function registerFont(name, urlOrBytes) {
    if (!initialized) {
        throw new Error("wasm-renderer: call initWasmRenderer() before registerFont()");
    }
    let bytes;
    if (urlOrBytes instanceof Uint8Array) {
        bytes = urlOrBytes;
    } else {
        const resp = await fetch(urlOrBytes);
        if (!resp.ok) {
            console.warn(`[wasm-renderer] failed to fetch font ${name}: ${resp.status}`);
            return false;
        }
        bytes = new Uint8Array(await resp.arrayBuffer());
    }
    const ok = wasmRegisterFont(name, bytes);
    if (ok) registeredFonts.add(name);
    else console.warn(`[wasm-renderer] register_font(${name}) returned false`);
    return ok;
}

/**
 * Returns whether a font has been registered + parsed successfully.
 * Callers (paintLayer) use this to decide whether to use the wasm
 * path or fall back to ctx.fillText.
 *
 * @param {string} name
 * @returns {boolean}
 */
export function isFontRegistered(name) {
    return registeredFonts.has(name);
}

// Rasterized-bitmap cache. Keyed by `${text}|${font}|${size}|${color}`.
// Avoids re-rasterizing the same string repeatedly across rAF ticks.
//
// r27: bounded by BYTE VOLUME, not entry count. Pre-r27 cap was 256
// entries, each holding an OffscreenCanvas up to 8192×8192 = 256 MiB.
// A long-ticker slide with frame-changing text (clock seconds,
// counters, typewriter) populated 256 distinct multi-megapixel
// canvases before LRU evicted — multi-GiB worst case on a low-RAM
// Pi-class device that gets OOM-killed mid-show.
//
// Budget: 32 MiB total across all cached bitmaps. Per-entry cap of
// 4 MiB rejects single huge bitmaps (1024×1024 RGBA) outright —
// caching a single 16 MiB bitmap would force eviction of EVERY
// smaller entry and still leave the cache above budget. The
// rejected-entry's miss path re-rasterizes via the wasm side; not
// caching it is cheaper than thrashing.
//
// The byte-arithmetic + LRU eviction loop is encapsulated in
// LruByteCache (./lru-byte-cache.js) for testability — the original
// inline cache lived behind the resolve.alias stub for jsdom.
const CACHE_BYTE_BUDGET = 32 * 1024 * 1024;     // 32 MiB
const CACHE_ENTRY_BYTE_CAP = 4 * 1024 * 1024;   // 4 MiB

const cache = new LruByteCache({
    byteBudget: CACHE_BYTE_BUDGET,
    entryByteCap: CACHE_ENTRY_BYTE_CAP,
});

/**
 * Rasterize `text` at `sizePx` in `fontName` with `colorRgba`.
 * Returns an OffscreenCanvas-or-HTMLCanvasElement ready for
 * `ctx.drawImage(canvas, x, y)`. The canvas internally holds the
 * straight-alpha RGBA bitmap from the wasm rasterizer; drawImage
 * performs proper source-over compositing onto the destination
 * canvas (whereas `putImageData` would REPLACE the bg pixels with
 * the bitmap's alpha=0 transparency in non-inked regions, blowing
 * away the slide background — the Phase 1b boot_sxs surfaced this
 * as 17,376 black pixels in the badge region).
 *
 * Returns `null` on (a) empty text, (b) all-whitespace text, (c)
 * font not registered.
 *
 * Color is `[r, g, b, a]` as 0-255 ints. The wasm rasterizer emits
 * STRAIGHT-alpha RGBA: text-color where coverage > 0, transparent
 * elsewhere. drawImage's default source-over composition correctly
 * preserves the destination's bg in transparent regions.
 *
 * @param {string} text
 * @param {string} fontName
 * @param {number} sizePx
 * @param {[number, number, number, number]} colorRgba
 * @returns {{ image: OffscreenCanvas | HTMLCanvasElement, width: number, height: number, ascent: number } | null}
 */
export function rasterizeText(text, fontName, sizePx, colorRgba) {
    if (!initialized) return null;
    if (!text || !registeredFonts.has(fontName)) return null;

    const sizeKey = Math.round(sizePx * 100) / 100; // 2-decimal stable key
    const key = `${text}|${fontName}|${sizeKey}|${colorRgba.join(",")}`;
    const cached = cache.get(key);
    if (cached) return cached;

    const buf = wasmRasterizeTextNamed(
        text, fontName, sizePx,
        colorRgba[0], colorRgba[1], colorRgba[2], colorRgba[3],
    );
    if (!buf) return null;

    // Header is 12 bytes LE: width u32, height u32, ascent u32.
    const dv = new DataView(buf.buffer, buf.byteOffset, 12);
    const width = dv.getUint32(0, true);
    const height = dv.getUint32(4, true);
    const ascent = dv.getUint32(8, true);
    if (width === 0 || height === 0) return null;
    // Chromium's Canvas2D backing-store cap is ~16384 px per side
    // and ~268M total pixels; OffscreenCanvas throws synchronously
    // past that. fontdue can emit arbitrarily wide bitmaps for long
    // single-line text at high font_size_px. Guard with a generous
    // 8192-per-side cap so paintLayer's null-result branch falls
    // back to ctx.fillText for the rare oversized line.
    if (width > 8192 || height > 8192) return null;

    // Copy pixel bytes into an owned ImageData (the wasm Vec<u8> can
    // be GC'd after this scope). The ImageData is then drawn into a
    // dedicated offscreen canvas so drawImage can blit it with
    // source-over composition.
    const pixelStart = buf.byteOffset + 12;
    const pixelLen = width * height * 4;
    const owned = new Uint8ClampedArray(
        new Uint8ClampedArray(buf.buffer, pixelStart, pixelLen),
    );
    const imageData = new ImageData(owned, width, height);

    // Use OffscreenCanvas where available (most modern browsers + the
    // Playwright Chromium the parity harness drives); fall back to a
    // detached HTMLCanvasElement otherwise. Both expose getContext +
    // putImageData and both work as drawImage sources.
    let canvas;
    if (typeof OffscreenCanvas === "function") {
        canvas = new OffscreenCanvas(width, height);
    } else {
        canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
    }
    canvas.getContext("2d").putImageData(imageData, 0, 0);

    const result = { image: canvas, width, height, ascent };
    // RGBA bitmap byte cost = width × height × 4. cache.set rejects
    // single entries over the per-entry cap (their miss path re-
    // rasterizes via the wasm side; not caching them keeps the rest
    // of the cache from thrashing).
    cache.set(key, result, width * height * 4);
    return result;
}

/**
 * Clear the rasterized-bitmap cache. Called on settings changes that
 * affect rasterization (font registration, brightness change post-
 * gamma-pass). The next paint repopulates from the wasm side.
 */
export function clearRasterizeCache() {
    cache.clear();
}
