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

// Inlined as Uint8Array via esbuild's --loader:.wasm=binary. The
// import-by-extension hook avoids any runtime fetch for the wasm.
import wasmBytes from "../../renderer-wasm/pkg/renderer_wasm_bg.wasm";

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
    initPromise = init({ module_or_path: wasmBytes }).then(() => {
        initialized = true;
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
// Bounded at 256 entries with LRU eviction so a slide with many
// distinct strings doesn't grow memory unbounded.
const CACHE_LIMIT = 256;
const cache = new Map();

function cacheGet(key) {
    const v = cache.get(key);
    if (v !== undefined) {
        // LRU: move to end.
        cache.delete(key);
        cache.set(key, v);
    }
    return v;
}

function cacheSet(key, value) {
    if (cache.size >= CACHE_LIMIT) {
        // Evict oldest (Map iteration order = insertion order).
        const firstKey = cache.keys().next().value;
        cache.delete(firstKey);
    }
    cache.set(key, value);
}

/**
 * Rasterize `text` at `sizePx` in `fontName` with `colorRgba`.
 * Returns an ImageData ready for `ctx.putImageData(image, x, y)` plus
 * the bitmap's internal baseline offset (ascent in pixels) so the
 * caller can position by baseline rather than top-left.
 *
 * Returns `null` on (a) empty text, (b) all-whitespace text, (c)
 * font not registered.
 *
 * Color is `[r, g, b, a]` as 0-255 ints. Output ImageData has
 * STRAIGHT-alpha RGBA — putImageData on a Canvas2D context performs
 * source-over compositing automatically.
 *
 * @param {string} text
 * @param {string} fontName
 * @param {number} sizePx
 * @param {[number, number, number, number]} colorRgba
 * @returns {{ image: ImageData, width: number, height: number, ascent: number } | null}
 */
export function rasterizeText(text, fontName, sizePx, colorRgba) {
    if (!initialized) return null;
    if (!text || !registeredFonts.has(fontName)) return null;

    const sizeKey = Math.round(sizePx * 100) / 100; // 2-decimal stable key
    const key = `${text}|${fontName}|${sizeKey}|${colorRgba.join(",")}`;
    const cached = cacheGet(key);
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

    // ImageData wants a Uint8ClampedArray view over the pixel slice
    // (NOT a copy — wasm-bindgen returns the Vec<u8> as a fresh
    // Uint8Array, so this clamped view shares its backing buffer).
    const pixelStart = buf.byteOffset + 12;
    const pixelLen = width * height * 4;
    const pixels = new Uint8ClampedArray(buf.buffer, pixelStart, pixelLen);
    // ImageData constructor copies the underlying data in some
    // browsers; doing the copy explicitly is safer + makes the
    // backing Uint8Array eligible for GC immediately.
    const owned = new Uint8ClampedArray(pixels);
    const image = new ImageData(owned, width, height);

    const result = { image, width, height, ascent };
    cacheSet(key, result);
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
