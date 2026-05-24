// Cross-renderer parity harness (commit 2 / qa/cross-renderer-parity-design.md).
// Exposes window.__parityCapture(itemJson, tickSeconds, transition?)
// → Promise<base64-png>. Playwright (scripts/parity/run.py)
// drives the page, evaluates the helper per fixture, and
// diffs the result against renderer/tests/golden/<name>.png.
import { drawCanvas } from "./src/rasterize.js";
import { stateFromItem } from "./src/state-from-item.js";
import { initWasmRenderer, registerFont } from "./src/wasm-renderer.js";

// Phase 1b parity fix 2026-05-14: paintLayer's wasm path
// wants the rasterizer initialized + every CSS font registered
// by the same family name before the first capture. Keep this
// table in sync with the @font-face rules in styles.css —
// registerFont's `name` is the lookup key paintLayer hits via
// `isFontRegistered(fontFamily)`, so the names must match the
// CSS family names verbatim.
const __WASM_FONTS = [
    ["Inter",                "./fonts/inter.ttf"],
    ["Oswald",               "./fonts/oswald.ttf"],
    ["Bebas Neue",           "./fonts/bebas-neue.ttf"],
    ["Roboto Slab",          "./fonts/roboto-slab.ttf"],
    ["Caveat Brush",         "./fonts/caveat-brush.ttf"],
    ["Permanent Marker",     "./fonts/permanent-marker.ttf"],
    ["Cinzel",               "./fonts/cinzel.ttf"],
    ["UnifrakturCook",       "./fonts/unifrakturcook.ttf"],
    ["Rye",                  "./fonts/rye.ttf"],
    ["Pacifico",             "./fonts/pacifico.ttf"],
    ["Sedgwick Ave Display", "./fonts/sedgwick-ave-display.ttf"],
    ["Bowlby One SC",        "./fonts/bowlby-one-sc.ttf"],
    ["Anton",                "./fonts/anton.ttf"],
    ["Archivo Black",        "./fonts/archivo-black.ttf"],
    ["Alfa Slab One",        "./fonts/alfa-slab-one.ttf"],
    ["Playfair Display",     "./fonts/playfair-display.ttf"],
    ["DM Serif Display",     "./fonts/dm-serif-display.ttf"],
    ["VT323",                "./fonts/vt323.ttf"],
    ["JetBrains Mono",       "./fonts/jetbrains-mono.ttf"],
    ["Space Mono",           "./fonts/space-mono.ttf"],
];
const __wasmReady = (async () => {
    await initWasmRenderer();
    await Promise.all(__WASM_FONTS.map(([n, u]) => registerFont(n, u)));
})().catch((err) => {
    console.warn("[parity-harness] wasm init failed; fillText fallback:", err);
});

const canvas = document.getElementById("parity-canvas");
const status = document.getElementById("parity-status");

function canvasToBase64(c) {
    return c.toDataURL("image/png").split(",")[1];
}

// Synchronous. The outer __parityCapture is async only because
// transition captures want to be -- keep the same call shape
// so the caller doesn't branch.
function captureSingle(item, tick) {
    const state = stateFromItem(item);
    drawCanvas(canvas, state, { elapsed_s: tick });
}

// Render `item` into a fresh ImageData. Helper for transition
// midpoints: we paint each side onto the shared canvas, snapshot
// via getImageData, then composite into a third ImageData.
function renderSlideImageData(item) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    drawCanvas(canvas, stateFromItem(item), { elapsed_s: 0 });
    return ctx.getImageData(0, 0, canvas.width, canvas.height);
}

// Bilinear sample of an ImageData at fractional uv (0..1).
// GL_LINEAR semantics with clamp-to-edge: the Rust transition
// shaders sample texture2D against u_src_a / u_src_b which use
// GL_LINEAR + GL_CLAMP_TO_EDGE. Off-image uv values clamp to
// border pixels (NOT wrap), so transitions that sample beyond
// the unit square (slide, scroll) read the edge color.
// GLSL `smoothstep(edge0, edge1, x)` — clamped Hermite
// interpolation 0→1 between edge0 and edge1. Matches the
// Rust shaders used by FS_SCANLINE (band brightness) and
// FS_PUSH (projector blade) verbatim. Cheap helper hoisted
// here so each transition can spell it out the same way.
function smoothstep(edge0, edge1, x) {
    const e = Math.max(0, Math.min(1, (x - edge0) / (edge1 - edge0)));
    return e * e * (3 - 2 * e);
}

function sampleBilinear(data, w, h, u, v, out, outOff) {
    // Clamp uv to [0,1] then convert to pixel space with the
    // GL center-of-pixel convention (uv 0 = pixel 0 center
    // minus 0.5, etc.).
    const cu = Math.max(0, Math.min(1, u));
    const cv = Math.max(0, Math.min(1, v));
    const fx = cu * w - 0.5;
    const fy = cv * h - 0.5;
    const x0 = Math.max(0, Math.min(w - 1, Math.floor(fx)));
    const y0 = Math.max(0, Math.min(h - 1, Math.floor(fy)));
    const x1 = Math.max(0, Math.min(w - 1, x0 + 1));
    const y1 = Math.max(0, Math.min(h - 1, y0 + 1));
    const wx = Math.max(0, Math.min(1, fx - x0));
    const wy = Math.max(0, Math.min(1, fy - y0));
    const i00 = (y0 * w + x0) * 4;
    const i10 = (y0 * w + x1) * 4;
    const i01 = (y1 * w + x0) * 4;
    const i11 = (y1 * w + x1) * 4;
    for (let c = 0; c < 3; c++) {
        const top = data[i00 + c] * (1 - wx) + data[i10 + c] * wx;
        const bot = data[i01 + c] * (1 - wx) + data[i11 + c] * wx;
        out[outOff + c] = top * (1 - wy) + bot * wy;
    }
}

// Generic transition-midpoint compositor. Picks the per-pixel
// sample-and-mix function for the named transition and runs it
// over the 1920×1080 canvas. UV convention: v_uv.x = pixel x /
// width, v_uv.y = pixel y / height (matches the Rust transition
// SP-tier vertex's v_uv.y = y / 1.0 mapping where y=0 is TOP --
// confirmed by the qarl-bug-2026-05-12 scroll fix).
//
// Each transition function takes (uv_x, uv_y, t) and produces
// either (a) a 2-element pair of [u, v, source] sampling
// coordinates per side, plus a mix factor (m: 0=A, 1=B), or
// (b) a per-pixel mix factor with both sides sampled at the
// same uv. We use a simple inline-switch dispatch for clarity
// -- transition count is small (6) and per-pixel cost dominates.
const TRANSITION_FNS = {
    cut(pixels, w, h, A, B, t) {
        // FS_CUT picks A side at t<0.5, B side at t>=0.5.
        const src = t < 0.5 ? A : B;
        pixels.set(src.data);
        pixels[3] = 255;
    },
    fade(pixels, w, h, A, B, t) {
        const inv = 1 - t;
        const a = A.data, b = B.data;
        for (let i = 0; i < pixels.length; i += 4) {
            pixels[i + 0] = Math.round(a[i + 0] * inv + b[i + 0] * t);
            pixels[i + 1] = Math.round(a[i + 1] * inv + b[i + 1] * t);
            pixels[i + 2] = Math.round(a[i + 2] * inv + b[i + 2] * t);
            pixels[i + 3] = 255;
        }
    },
    wipe(pixels, w, h, A, B, t) {
        // mask = step(uv.x, t). x<t -> B, else A.
        const a = A.data, b = B.data;
        for (let y = 0; y < h; y++) {
            const tCol = Math.floor(t * w);
            for (let x = 0; x < w; x++) {
                const i = (y * w + x) * 4;
                const src = x < tCol ? b : a;
                pixels[i + 0] = src[i + 0];
                pixels[i + 1] = src[i + 1];
                pixels[i + 2] = src[i + 2];
                pixels[i + 3] = 255;
            }
        }
    },
    slide(pixels, w, h, A, B, t) {
        // FS_SLIDE: seam=1-t; onTo=step(seam, uv.x); A.uv=(x+t,y);
        // B.uv=(x-seam, y). At t=0.5 the seam is at uv.x=0.5,
        // A's image has scrolled right by t, B's by -seam.
        const seam = 1 - t;
        const tmp = [0, 0, 0];
        for (let y = 0; y < h; y++) {
            const v = (y + 0.5) / h;
            for (let x = 0; x < w; x++) {
                const u = (x + 0.5) / w;
                const i = (y * w + x) * 4;
                const useB = u >= seam;
                const srcUVx = useB ? u - seam : u + t;
                sampleBilinear(useB ? B.data : A.data, w, h, srcUVx, v, tmp, 0);
                pixels[i + 0] = Math.round(tmp[0]);
                pixels[i + 1] = Math.round(tmp[1]);
                pixels[i + 2] = Math.round(tmp[2]);
                pixels[i + 3] = 255;
            }
        }
    },
    scroll(pixels, w, h, A, B, t) {
        // FS_SCROLL has a Y-axis convention twist relative to
        // single-slide / horizontal transitions: the Rust path
        // runs in GL coords where v_uv.y=0 is the visual
        // BOTTOM of the screen (NDC y=-1 maps to uv.y=0). Our
        // JS canvas + drawCanvas use y=0 = TOP. Same shader
        // math gives mirrored output if you don't convert.
        //
        // Visual semantics (matching the qarl-bug-2026-05-12
        // fix in the Rust shader at hdmi_logic.rs:896): B
        // rises from below, A scrolls off the top. At t=0.5
        // the bottom half of the screen shows the top half of
        // B; the top half shows the bottom half of A. The
        // formulas below are derived in canvas-y-down terms
        // directly, not by post-flipping v_uv.y, so they're
        // easier to verify against on-screen behavior.
        //   B fills v >= 1 - t (the lower-y, bottom region)
        //   A fills v <  1 - t (the upper-y, top region)
        //   A samples at v + t          (A scrolled UP by t)
        //   B samples at v + t - 1      (B risen by 1 - t)
        const tmp = [0, 0, 0];
        const seam = 1 - t;
        for (let y = 0; y < h; y++) {
            const v = (y + 0.5) / h;
            const useB = v >= seam;
            for (let x = 0; x < w; x++) {
                const u = (x + 0.5) / w;
                const i = (y * w + x) * 4;
                const srcUVy = useB ? v - seam : v + t;
                sampleBilinear(useB ? B.data : A.data, w, h, u, srcUVy, tmp, 0);
                pixels[i + 0] = Math.round(tmp[0]);
                pixels[i + 1] = Math.round(tmp[1]);
                pixels[i + 2] = Math.round(tmp[2]);
                pixels[i + 3] = 255;
            }
        }
    },
    pixelate(pixels, w, h, A, B, t) {
        // FS_PIXELATE: wave = 1 - 4·(t-0.5)². At t=0.5 wave=1,
        // blockSize = 0.0425. cell = floor(uv/bs)*bs + 0.5*bs.
        // Both A and B sampled at cell-center then mixed with t.
        const wave = 1 - 4 * (t - 0.5) * (t - 0.5);
        const blockSize = 0.0025 + 0.04 * wave;
        const inv = 1 - t;
        const tmpA = [0, 0, 0], tmpB = [0, 0, 0];
        for (let y = 0; y < h; y++) {
            const v = (y + 0.5) / h;
            const cellV = Math.floor(v / blockSize) * blockSize + 0.5 * blockSize;
            for (let x = 0; x < w; x++) {
                const u = (x + 0.5) / w;
                const cellU = Math.floor(u / blockSize) * blockSize + 0.5 * blockSize;
                const i = (y * w + x) * 4;
                sampleBilinear(A.data, w, h, cellU, cellV, tmpA, 0);
                sampleBilinear(B.data, w, h, cellU, cellV, tmpB, 0);
                pixels[i + 0] = Math.round(tmpA[0] * inv + tmpB[0] * t);
                pixels[i + 1] = Math.round(tmpA[1] * inv + tmpB[1] * t);
                pixels[i + 2] = Math.round(tmpA[2] * inv + tmpB[2] * t);
                pixels[i + 3] = 255;
            }
        }
    },
    // --- H4 (2026-05-23): close 7 BROWSER-SKIP fixtures by
    // translating FS_IRIS / FS_SCANLINE / FS_GLITCH /
    // FS_PUSH / FS_FLIP / FS_MARQUEE / FS_SHUTTER from
    // hdmi_logic.rs to the same (pixels, w, h, A, B, t)
    // pattern as the 6 above. Dispatch order matches the
    // Rust shader declaration order for review-friendliness.
    iris(pixels, w, h, A, B, t) {
        // FS_IRIS: mask = step(distance(uv, vec2(0.5)),
        // u_t * 0.71). 0.71 ≈ sqrt(0.5) — half-diagonal —
        // so the disk fully covers the canvas at t=1.
        // Symmetric in y → no canvas-y-down conversion needed.
        const a = A.data, b = B.data;
        const rSq = (t * 0.71) * (t * 0.71);
        for (let y = 0; y < h; y++) {
            const dy = (y + 0.5) / h - 0.5;
            for (let x = 0; x < w; x++) {
                const dx = (x + 0.5) / w - 0.5;
                const i = (y * w + x) * 4;
                const src = (dx * dx + dy * dy) <= rSq ? b : a;
                pixels[i + 0] = src[i + 0];
                pixels[i + 1] = src[i + 1];
                pixels[i + 2] = src[i + 2];
                pixels[i + 3] = 255;
            }
        }
    },
    scanline(pixels, w, h, A, B, t) {
        // FS_SCANLINE: step(v_uv.y, sweep) mask + a bright
        // band 0.015-UV-half-wide around the sweep line,
        // brightness 0.7 (mixed toward white). Re-derived in
        // canvas-y-down terms (same convention the existing
        // scroll() uses): GL v_uv.y = 1 - canvas_v, so the
        // GL `step(v_uv.y, sweep)` becomes useB when
        // canvas_v >= 1 - sweep; the band is brightest at
        // canvas_v = 1 - sweep. Visually, B fills the BOTTOM
        // (high canvas_v) region as t grows; the bright band
        // line sweeps from canvas_v=1 (bottom) at t=0 to
        // canvas_v=0 (top) at t=1.
        const a = A.data, b = B.data;
        const bandHalf = 0.015;
        const seam = 1 - t;
        for (let y = 0; y < h; y++) {
            const v = (y + 0.5) / h;
            const useB = v >= seam;
            const band = 1 - smoothstep(0, bandHalf, Math.abs(v - seam));
            const bandMix = band * 0.7;
            const inv = 1 - bandMix;
            for (let x = 0; x < w; x++) {
                const i = (y * w + x) * 4;
                const src = useB ? b : a;
                pixels[i + 0] = Math.round(src[i + 0] * inv + 255 * bandMix);
                pixels[i + 1] = Math.round(src[i + 1] * inv + 255 * bandMix);
                pixels[i + 2] = Math.round(src[i + 2] * inv + 255 * bandMix);
                pixels[i + 3] = 255;
            }
        }
    },
    glitch(pixels, w, h, A, B, t) {
        // FS_GLITCH: per-row horizontal jitter of magnitude
        // ±0.05·t (so zero at t=0, max at t=1) seeded by
        // hash(row, frame_seed); linear A→B cross-fade at
        // the jittered uv.x; occasional cyan tear rows
        // (top 5% of hash distribution at coarser-row
        // binning, mixed toward cyan by 0.5·t).
        //
        // Hash is `fract(sin(dot(p, vec2(12.9898, 78.233))) *
        // 43758.5453)`. Rust uses `precision highp float`
        // because vc4 mediump collapses the sin-times-large
        // arithmetic; JS doubles cover the precision either way.
        //
        // Re-derived in canvas-y-down terms: Rust's
        // `row = floor(v_uv.y * 1080)` becomes
        // `floor((1 - canvas_v) * 1080)`. Same for tear row
        // (1080 → 60).
        const tmp = [0, 0, 0];
        const inv = 1 - t;
        const frameSeed = Math.floor(t * 30);
        function hash(p1, p2) {
            const d = p1 * 12.9898 + p2 * 78.233;
            const s = Math.sin(d) * 43758.5453;
            return s - Math.floor(s);
        }
        for (let y = 0; y < h; y++) {
            const v = (y + 0.5) / h;
            const glV = 1 - v;
            const row = Math.floor(glV * 1080);
            const jitter = (hash(row, frameSeed) - 0.5) * 0.1 * t;
            const tearRow = Math.floor(glV * 60);
            const tearOn = hash(tearRow, frameSeed + 1) >= 0.95 ? 1 : 0;
            const tearMix = tearOn * 0.5 * t;
            const baseInv = 1 - tearMix;
            for (let x = 0; x < w; x++) {
                const u = (x + 0.5) / w;
                const i = (y * w + x) * 4;
                const srcU = u + jitter;
                sampleBilinear(A.data, w, h, srcU, v, tmp, 0);
                const aR = tmp[0], aG = tmp[1], aB = tmp[2];
                sampleBilinear(B.data, w, h, srcU, v, tmp, 0);
                const r = aR * inv + tmp[0] * t;
                const g = aG * inv + tmp[1] * t;
                const bl = aB * inv + tmp[2] * t;
                // Cyan tear: (0, 255, 255) mixed in by tearMix.
                pixels[i + 0] = Math.round(r * baseInv);
                pixels[i + 1] = Math.round(g * baseInv + 255 * tearMix);
                pixels[i + 2] = Math.round(bl * baseInv + 255 * tearMix);
                pixels[i + 3] = 255;
            }
        }
    },
    push(pixels, w, h, A, B, t) {
        // FS_PUSH: B enters from LEFT pushing A off RIGHT.
        // onTo = step(v_uv.x, t) → useB when canvas_u <= t.
        //   A samples at (u - t, v)         (A scrolled LEFT by t)
        //   B samples at (u + (1 - t), v)   (B entered by 1-t from right)
        // Bright projector-blade smoothstep'd 0.001-UV wide,
        // brightness 0.8, white, at the seam (canvas_u == t).
        // x-only math → no canvas-y-down conversion.
        const tmp = [0, 0, 0];
        const bladeHalf = 0.001;
        for (let y = 0; y < h; y++) {
            const v = (y + 0.5) / h;
            for (let x = 0; x < w; x++) {
                const u = (x + 0.5) / w;
                const i = (y * w + x) * 4;
                const useB = u <= t;
                const srcU = useB ? u + (1 - t) : u - t;
                sampleBilinear(useB ? B.data : A.data, w, h, srcU, v, tmp, 0);
                const blade = 1 - smoothstep(0, bladeHalf, Math.abs(u - t));
                const bladeMix = blade * 0.8;
                const inv = 1 - bladeMix;
                pixels[i + 0] = Math.round(tmp[0] * inv + 255 * bladeMix);
                pixels[i + 1] = Math.round(tmp[1] * inv + 255 * bladeMix);
                pixels[i + 2] = Math.round(tmp[2] * inv + 255 * bladeMix);
                pixels[i + 3] = 255;
            }
        }
    },
    flip(pixels, w, h, A, B, t) {
        // FS_FLIP: 2D card-flip. scaleX = |2t - 1| (1→0 in
        // first half, 0→1 in second). useTo = step(0.5, t).
        // src_x = (u - 0.5) / max(scaleX, 1e-3) + 0.5.
        // Branchless inside-mask in Rust:
        //   inside = step(0.001, scaleX) * step(0, src_x) * step(src_x, 1)
        // Out-of-card pixels emit BLACK (rgb * inside, alpha 1).
        // Inside-card pixels: mix(A, B, useTo) at (src_x, v).
        // y-axis is untouched (sample passes canvas_v through) →
        // no convention conversion needed for the sampler.
        const tmp = [0, 0, 0];
        const scaleX = Math.abs(2 * t - 1);
        const useB = t >= 0.5;
        const denom = Math.max(scaleX, 1e-3);
        const cardCollapsed = scaleX < 0.001;
        for (let y = 0; y < h; y++) {
            const v = (y + 0.5) / h;
            for (let x = 0; x < w; x++) {
                const u = (x + 0.5) / w;
                const i = (y * w + x) * 4;
                const srcU = (u - 0.5) / denom + 0.5;
                const inside = !cardCollapsed && srcU >= 0 && srcU <= 1;
                if (!inside) {
                    pixels[i + 0] = 0;
                    pixels[i + 1] = 0;
                    pixels[i + 2] = 0;
                    pixels[i + 3] = 255;
                    continue;
                }
                sampleBilinear(useB ? B.data : A.data, w, h, srcU, v, tmp, 0);
                pixels[i + 0] = Math.round(tmp[0]);
                pixels[i + 1] = Math.round(tmp[1]);
                pixels[i + 2] = Math.round(tmp[2]);
                pixels[i + 3] = 255;
            }
        }
    },
    marquee(pixels, w, h, A, B, t) {
        // FS_MARQUEE: tickertape wraparound. gap_uv = 0.125
        // black gap zone between A's exit and B's entry,
        // with a centered white dot (radius 0.074 in UV)
        // passing through the gap. scroll = t·(1+gap);
        // cx = scroll + canvas_u. Three regions:
        //   in_from (cx <= 1):           A at (cx, v)
        //   in_gap  (1 < cx < 1+gap):    black + dot
        //   in_to   (cx >= 1+gap):       B at (cx - 1 - gap, v)
        // y enters only as |canvas_v - 0.5| for the dot →
        // symmetric, no convention conversion.
        const tmp = [0, 0, 0];
        const gap = 0.125;
        const dotR = 0.074;
        const scroll = t * (1 + gap);
        for (let y = 0; y < h; y++) {
            const v = (y + 0.5) / h;
            const dy = v - 0.5;
            for (let x = 0; x < w; x++) {
                const u = (x + 0.5) / w;
                const cx = scroll + u;
                const i = (y * w + x) * 4;
                if (cx <= 1) {
                    sampleBilinear(A.data, w, h, cx, v, tmp, 0);
                    pixels[i + 0] = Math.round(tmp[0]);
                    pixels[i + 1] = Math.round(tmp[1]);
                    pixels[i + 2] = Math.round(tmp[2]);
                } else if (cx >= 1 + gap) {
                    sampleBilinear(B.data, w, h, cx - 1 - gap, v, tmp, 0);
                    pixels[i + 0] = Math.round(tmp[0]);
                    pixels[i + 1] = Math.round(tmp[1]);
                    pixels[i + 2] = Math.round(tmp[2]);
                } else {
                    // Gap region: black + centered white dot.
                    const gapLocalX = (cx - 1) / gap;
                    const dxUV = (gapLocalX - 0.5) * gap;
                    const dist = Math.sqrt(dxUV * dxUV + dy * dy);
                    const c = dist <= dotR ? 255 : 0;
                    pixels[i + 0] = c;
                    pixels[i + 1] = c;
                    pixels[i + 2] = c;
                }
                pixels[i + 3] = 255;
            }
        }
    },
    shutter(pixels, w, h, A, B, t) {
        // FS_SHUTTER: hexagonal aperture grows from a point
        // at t=0 to fully covering the canvas at t=1. Aspect-
        // corrected to 16:9 (d.x *= 16/9) so the hex stays
        // regular at 1080p. k = cos(30°) ≈ 0.866025. Three
        // edge expressions (c1, c2, c3); hex_d = max(...);
        // mask = step(hex_d, 1.5 · t). Symmetric in y →
        // no convention conversion.
        const a = A.data, b = B.data;
        const k = 0.866025;
        const aspect = 16 / 9;
        const inscribed = 1.5 * t;
        for (let y = 0; y < h; y++) {
            const dy = (y + 0.5) / h - 0.5;
            for (let x = 0; x < w; x++) {
                const dx = ((x + 0.5) / w - 0.5) * aspect;
                const c1 = Math.abs(dx * k + dy * 0.5);
                const c2 = Math.abs(dy);
                const c3 = Math.abs(dx * k - dy * 0.5);
                const hexD = Math.max(c1, Math.max(c2, c3));
                const i = (y * w + x) * 4;
                const src = hexD <= inscribed ? b : a;
                pixels[i + 0] = src[i + 0];
                pixels[i + 1] = src[i + 1];
                pixels[i + 2] = src[i + 2];
                pixels[i + 3] = 255;
            }
        }
    },
};

async function captureTransitionMid(fromItem, toItem, transitionName, t) {
    const ctx = canvas.getContext("2d");
    const A = renderSlideImageData(fromItem);
    const B = renderSlideImageData(toItem);
    const out = ctx.createImageData(canvas.width, canvas.height);
    const fn = TRANSITION_FNS[transitionName];
    if (!fn) throw new Error(`unknown transition: ${transitionName}`);
    fn(out.data, canvas.width, canvas.height, A, B, t);
    ctx.putImageData(out, 0, 0);
}

// Promise resolves when all @font-face fonts referenced by
// any fixture are ready. Without this the first capture
// races font loading and produces a system-sans fallback
// render that doesn't match the Rust path.
window.__parityFontsReady = (async () => {
    // Force every bundled @font-face to actually load by
    // measuring a glyph in each family. document.fonts.load
    // is per-family + size; pick a representative size.
    const families = [
        "Inter", "Oswald", "Bebas Neue", "Roboto Slab",
        "Caveat Brush", "Permanent Marker", "Cinzel",
        "UnifrakturCook", "Rye", "Pacifico",
        "Sedgwick Ave Display", "Bowlby One SC",
        "Anton", "Archivo Black", "Alfa Slab One",
        "Playfair Display", "DM Serif Display",
        "VT323", "JetBrains Mono", "Space Mono",
    ];
    await Promise.all(
        families.map((fam) => document.fonts.load(`700 100px "${fam}"`)),
    );
    await document.fonts.ready;
})();

window.__parityCapture = async function (params) {
    await window.__parityFontsReady;
    await __wasmReady;
    const { kind } = params;
    if (kind === "single") {
        captureSingle(params.item, params.tick || 0);
    } else if (kind === "transition_mid") {
        await captureTransitionMid(
            params.fromItem,
            params.toItem,
            params.transition,
            params.transitionT == null ? 0.5 : params.transitionT,
        );
    } else {
        throw new Error(`unknown parity kind: ${kind}`);
    }
    return canvasToBase64(canvas);
};

status.textContent = "parity-harness: ready";
