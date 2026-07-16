// Tests for rasterize.js. Currently focused on the brightness/gamma
// post-pass (parity-audit #5 fix 2026-05-14). Pins the math against
// Rust's `apply_brightness_gamma_rgba` CPU-mirror at
// `renderer/src/hdmi_logic.rs:1974-1989` and the corresponding test
// cases at `renderer/src/hdmi_logic.rs:3964-4010`.
//
// jsdom doesn't ship a real Canvas2D context — we use a fake canvas
// that backs an ImageData with a Uint8ClampedArray so the
// getImageData/putImageData round-trip in applyBrightnessGamma
// preserves bytes.

import { describe, expect, it, vi } from "vitest";
import { applyBrightnessGamma, drawCanvas, drawTextOnly } from "./rasterize.js";

function makeCanvas(width, height, initialPixels) {
    // 4 bytes per pixel (RGBA), filled with initialPixels (a flat
    // Uint8ClampedArray) or zero if not supplied.
    const data = new Uint8ClampedArray(width * height * 4);
    if (initialPixels) data.set(initialPixels);
    const ctx = {
        getImageData(x, y, w, h) {
            // We only support full-canvas getImageData calls; the
            // gamma pass uses exactly that shape.
            return { data, width: w, height: h };
        },
        putImageData(img, _x, _y) {
            data.set(img.data);
        },
    };
    return {
        width,
        height,
        getContext() { return ctx; },
        _pixels: data,
    };
}

describe("applyBrightnessGamma", () => {
    // -- Rust mirror: apply_brightness_gamma_identity_at_b1_g1
    it("is a no-op at brightness=1, gamma=1 (identity)", () => {
        const pixels = new Uint8ClampedArray([
            10, 20, 30, 255,
            40, 50, 60, 128,
        ]);
        const canvas = makeCanvas(2, 1, pixels);
        applyBrightnessGamma(canvas, 1.0, 1.0);
        // Identity transform: bytes unchanged, alpha unchanged.
        expect(Array.from(canvas._pixels)).toEqual([
            10, 20, 30, 255,
            40, 50, 60, 128,
        ]);
    });

    // -- Rust mirror: apply_brightness_gamma_halves_at_b_half
    it("halves RGB at brightness=0.5, gamma=1 (alpha pass-through)", () => {
        const pixels = new Uint8ClampedArray([100, 200, 254, 200]);
        const canvas = makeCanvas(1, 1, pixels);
        applyBrightnessGamma(canvas, 0.5, 1.0);
        // 100*0.5 = 50, 200*0.5 = 100, 254*0.5 = 127 (rounded).
        // gamma=1 means pow(x, 1) = x, so no further change.
        expect(canvas._pixels[0]).toBe(50);
        expect(canvas._pixels[1]).toBe(100);
        expect(canvas._pixels[2]).toBe(127);
        expect(canvas._pixels[3]).toBe(200); // alpha unchanged.
    });

    // -- Rust mirror: apply_brightness_gamma_lightens_at_g_22
    it("lightens RGB at brightness=1, gamma=2.2", () => {
        // pow(x, 1/2.2) > x for x in (0, 1). Confirm the direction
        // and pin a representative value. 128/255 = 0.502; raised
        // to 1/2.2 ≈ 0.4545 power gives ≈ 0.7297 → round(186) = 186.
        const pixels = new Uint8ClampedArray([128, 128, 128, 255]);
        const canvas = makeCanvas(1, 1, pixels);
        applyBrightnessGamma(canvas, 1.0, 2.2);
        // Expected ≈ 186, allow ±1 for float→u8 rounding.
        expect(canvas._pixels[0]).toBeGreaterThanOrEqual(185);
        expect(canvas._pixels[0]).toBeLessThanOrEqual(187);
        expect(canvas._pixels[0]).toBeGreaterThan(128); // gamma 2.2 lightens.
        expect(canvas._pixels[3]).toBe(255);
    });

    // -- Rust mirror: apply_brightness_gamma_clamps_overflow_at_b_2
    it("clamps overflow at brightness=2, gamma=1", () => {
        const pixels = new Uint8ClampedArray([200, 100, 50, 255]);
        const canvas = makeCanvas(1, 1, pixels);
        applyBrightnessGamma(canvas, 2.0, 1.0);
        // 200*2 = 400 → clamped to 255. 100*2 = 200. 50*2 = 100.
        expect(canvas._pixels[0]).toBe(255);
        expect(canvas._pixels[1]).toBe(200);
        expect(canvas._pixels[2]).toBe(100);
        expect(canvas._pixels[3]).toBe(255);
    });

    it("preserves alpha across the gamma pass", () => {
        // Half-translucent pixel. Confirm alpha at i+3 isn't touched
        // even when RGB is dramatically transformed.
        const pixels = new Uint8ClampedArray([255, 0, 128, 64]);
        const canvas = makeCanvas(1, 1, pixels);
        applyBrightnessGamma(canvas, 0.5, 2.2);
        expect(canvas._pixels[3]).toBe(64);
    });

    it("handles zero-size canvas without error", () => {
        const canvas = makeCanvas(0, 0);
        expect(() => applyBrightnessGamma(canvas, 1.0, 2.2)).not.toThrow();
    });

    // -- Operator-set anchors (brightness=0.8 still matches the
    //    schema default; gamma=2.2 is an explicit operator dial
    //    after the gamma default flipped from 2.2 → 1.0). These
    //    two cases pin the function's response at one of the
    //    realistic operator-chosen working points; they don't
    //    claim to mirror schema defaults anymore.
    it("at brightness=0.8 + gamma=2.2: white → ~232 (not 255)", () => {
        const pixels = new Uint8ClampedArray([255, 255, 255, 255]);
        const canvas = makeCanvas(1, 1, pixels);
        applyBrightnessGamma(canvas, 0.8, 2.2);
        // pow(clamp(1.0 * 0.8, 0, 1), 1/2.2) = pow(0.8, 0.4545)
        //   ≈ 0.9036 → round(230.4) = 230. Allow ±2 LSB slack for
        // float64-vs-float32 + rounding direction.
        expect(canvas._pixels[0]).toBeGreaterThanOrEqual(228);
        expect(canvas._pixels[0]).toBeLessThanOrEqual(232);
        expect(canvas._pixels[0]).toBeLessThan(255); // not saturated.
        expect(canvas._pixels[3]).toBe(255);
    });

    it("at brightness=0.8 + gamma=2.2: mid-gray pins the curve", () => {
        const pixels = new Uint8ClampedArray([128, 128, 128, 255]);
        const canvas = makeCanvas(1, 1, pixels);
        applyBrightnessGamma(canvas, 0.8, 2.2);
        // pow(clamp((128/255)*0.8, 0, 1), 1/2.2)
        //   = pow(0.4016, 0.4545) ≈ 0.6611 → round(168.6) = 169.
        // Range allows for float rounding.
        expect(canvas._pixels[0]).toBeGreaterThanOrEqual(167);
        expect(canvas._pixels[0]).toBeLessThanOrEqual(170);
        // Confirms brightness was applied BEFORE gamma (the math order
        // that matches Rust): if we'd gamma'd first, mid-gray would
        // lighten to ~186 then dim to ~149 — the opposite curve.
        expect(canvas._pixels[0]).toBeGreaterThan(128); // gamma still lifts.
    });

    it("clamps gamma to avoid divide-by-zero (matches Rust max(g, 0.001))", () => {
        // gamma=0 would give 1/0=Infinity exponent. Rust clamps to
        // 0.001; this mirror should too.
        const pixels = new Uint8ClampedArray([128, 128, 128, 255]);
        const canvas = makeCanvas(1, 1, pixels);
        expect(() => applyBrightnessGamma(canvas, 1.0, 0)).not.toThrow();
        // With invGamma = 1/0.001 = 1000, pow(0.502, 1000) ≈ 0.
        // Just verify finite output, not specific value.
        for (let i = 0; i < 3; i++) {
            expect(Number.isFinite(canvas._pixels[i])).toBe(true);
        }
    });
});

// Pin the new line-height layout (parity-audit P0 follow-up to
// b445aa5). Canvas2D's paintLayer now uses integer `Math.round(
// fontSize * 1.1)` line stride + `actualBoundingBoxAscent/Descent`
// for ink extent + `textBaseline="alphabetic"` baseline anchoring —
// matching Rust's hdmi_logic.rs:269 + :481 formulas.
describe("paintLayer line-height (Rust-canonical layout)", () => {
    // jsdom doesn't ship Canvas2D, so we stub a context with a
    // measureText that returns deterministic ink bounds and tracks
    // every fillText call so the test can assert layout positions.
    function makeStubbedCanvas() {
        const calls = [];
        let textBaseline = null;
        const ctx = {
            save: vi.fn(), restore: vi.fn(), translate: vi.fn(),
            scale: vi.fn(), fillRect: vi.fn(),
            fillText: vi.fn((line, x, y) => calls.push({ line, x, y })),
            measureText: vi.fn((str) => ({
                width: str.length * 30,
                // ASCII-ish glyph: ascent = 0.75*size, descent = 0.25*size
                actualBoundingBoxAscent: 75,
                actualBoundingBoxDescent: 25,
            })),
            set fillStyle(_v) {}, set font(_v) {},
            set textAlign(_v) {}, set textBaseline(v) { textBaseline = v; },
            set globalAlpha(_v) {}, set globalCompositeOperation(_v) {},
        };
        return {
            width: 1000, height: 1000, getContext: () => ctx,
            _calls: calls, get _textBaseline() { return textBaseline; },
        };
    }

    it("uses integer line-height = round(fontSize * 1.1) as the per-line stride", () => {
        // The CRITICAL invariant is the inter-line stride: each baseline
        // must be exactly `round(fontSize * 1.1)` below the previous so
        // multi-line text packs at the same cadence as Rust's
        // `line_h_px = (size_px * 1.1).round()`. Absolute first-line y
        // depends on the back-compat flat-shape layer construction
        // (text + box → constructed TextLayer), which centers around
        // a different anchor than the layered path — assert stride only
        // here, baseline absolutes in the next test.
        const canvas = makeStubbedCanvas();
        drawCanvas(canvas, {
            text: "AAA\nBBB\nCCC",
            fontSize: 100,
            box: { x: 0, y: 0, w: 1, h: 1 },
        });
        const calls = canvas._calls;
        expect(calls.length).toBe(3);
        // Stride is exactly 110 (integer, matching Rust line_h_px).
        // Pre-fix the stride was 110 too (1.1 * 100 = 110 exactly), so
        // this test wouldn't have caught the bug — but it pins the
        // integer-vs-float distinction for fontSize where 1.1 doesn't
        // land on a whole number (e.g. 54 * 1.1 = 59.4 → round to 59).
        expect(calls[1].y - calls[0].y).toBeCloseTo(110, 5);
        expect(calls[2].y - calls[1].y).toBeCloseTo(110, 5);
        // Lines must be monotonically increasing in y (top → bottom).
        expect(calls[0].y).toBeLessThan(calls[1].y);
        expect(calls[1].y).toBeLessThan(calls[2].y);
    });

    it("vertical anchor top/center/bottom shifts the block (matches renderer valign)", () => {
        // v1.0 §5.10a: layer.anchor places the text block at the box top,
        // center, or bottom — mirroring the renderer's box_to_ndc_quad
        // valign offset. Single line "X": ink extent = ascent(75)+descent(25)
        // = 100 in a 1000px-tall box (no squish).
        const baselineFor = (anchor) => {
            const canvas = makeStubbedCanvas();
            drawCanvas(canvas, {
                text: "X",
                fontSize: 100,
                box: { x: 0, y: 0, w: 1, h: 1 },
                anchor,
            });
            return canvas._calls[0].y;
        };
        const top = baselineFor("top");
        const center = baselineFor("center");
        const bottom = baselineFor("bottom");
        // Monotonic top → bottom.
        expect(top).toBeLessThan(center);
        expect(center).toBeLessThan(bottom);
        // Exact placements: top block-top at box-top (baseline = ascent 75);
        // center baseline at 525; bottom block-bottom at box-bottom
        // (baseline = 1000 - descent 25 = 975).
        expect(top).toBeCloseTo(75, 0);
        expect(center).toBeCloseTo(525, 0);
        expect(bottom).toBeCloseTo(975, 0);
    });

    it("uses integer rounding so 1.1 * fontSize is snapped (size=54 → 59)", () => {
        // size 54 * 1.1 = 59.4. Pre-fix used the float; Rust rounds.
        // After fix, line stride is exactly 59 (not 59.4) per
        // `Math.round(fontSize * 1.1)`.
        const canvas = makeStubbedCanvas();
        drawCanvas(canvas, {
            text: "AAA\nBBB",
            fontSize: 54,
            box: { x: 0, y: 0, w: 1, h: 1 },
        });
        const calls = canvas._calls;
        expect(calls.length).toBe(2);
        // Stride must be 59 exactly, not 59.4.
        expect(calls[1].y - calls[0].y).toBe(59);
    });

    it("uses textBaseline=alphabetic so vertical anchor is the glyph baseline", () => {
        // Pre-fix Canvas2D set textBaseline=middle, which keys off the
        // font-bounding-box (engine-specific for tall em-box fonts like
        // VT323). Rust anchors at the glyph baseline + max_ascent; the
        // JS-side equivalent is textBaseline=alphabetic + manual baseline
        // placement.
        const canvas = makeStubbedCanvas();
        drawCanvas(canvas, { text: "X", font_size_px: 50 });
        expect(canvas._textBaseline).toBe("alphabetic");
    });

    it("single-line baseline lands inside the box (not above/below)", () => {
        // Single-line text with full-canvas box should put the
        // baseline somewhere in the box's vertical span — not the
        // exact box center (that's the OLD textBaseline=middle
        // behavior) but offset by the descender below.
        const canvas = makeStubbedCanvas();
        drawCanvas(canvas, {
            text: "X",
            fontSize: 100,
            box: { x: 0, y: 0, w: 1, h: 1 },
        });
        const y = canvas._calls[0].y;
        // For full-canvas box of 1000, ascent=75, descent=25, the
        // baseline sits at boxCenterY (500) + (ascent - descent)/2 = 525.
        // Range allows for the back-compat constructed layer's
        // internal anchor variance.
        expect(y).toBeGreaterThan(400);
        expect(y).toBeLessThan(600);
    });
});


// r51: outline + drop_shadow are applied via drawTextLineWithEffects
// which mutates ctx.strokeStyle / lineWidth / shadow* state around the
// fillText call. The stubbed canvas needs to track those.
describe("paintLayer text effects (r51 outline + drop_shadow)", () => {
    function makeStubbedCanvasWithEffects() {
        const fillCalls = [];
        const strokeCalls = [];
        const events = [];
        let strokeStyle = null;
        let lineWidth = null;
        let shadowOffsetX = 0;
        let shadowOffsetY = 0;
        let shadowBlur = 0;
        let shadowColor = null;
        const ctx = {
            save: vi.fn(), restore: vi.fn(), translate: vi.fn(),
            scale: vi.fn(), fillRect: vi.fn(),
            fillText: vi.fn((line, x, y) => {
                fillCalls.push({
                    line, x, y,
                    shadowOffsetX, shadowOffsetY, shadowBlur, shadowColor,
                });
                events.push({ kind: "fill", shadowOffsetX, shadowOffsetY });
            }),
            strokeText: vi.fn((line, x, y) => {
                strokeCalls.push({
                    line, x, y, strokeStyle, lineWidth,
                    shadowOffsetX, shadowOffsetY,
                });
                events.push({
                    kind: "stroke", strokeStyle, lineWidth,
                    shadowOffsetX, shadowOffsetY,
                });
            }),
            measureText: vi.fn((str) => ({
                width: str.length * 30,
                actualBoundingBoxAscent: 75,
                actualBoundingBoxDescent: 25,
            })),
            set fillStyle(_v) {}, set font(_v) {},
            set textAlign(_v) {}, set textBaseline(_v) {},
            set globalAlpha(_v) {}, set globalCompositeOperation(_v) {},
            set strokeStyle(v) { strokeStyle = v; },
            set lineWidth(v) { lineWidth = v; },
            set shadowOffsetX(v) { shadowOffsetX = v; },
            set shadowOffsetY(v) { shadowOffsetY = v; },
            set shadowBlur(v) { shadowBlur = v; },
            set shadowColor(v) { shadowColor = v; },
        };
        return {
            width: 1000, height: 1000, getContext: () => ctx,
            _fillCalls: fillCalls, _strokeCalls: strokeCalls,
            _events: events,
        };
    }

    it("default layer (no effects) only calls fillText with no shadow + no stroke", () => {
        const canvas = makeStubbedCanvasWithEffects();
        drawCanvas(canvas, { text: "X", font_size_px: 50 });
        expect(canvas._strokeCalls).toHaveLength(0);
        expect(canvas._fillCalls).toHaveLength(1);
        expect(canvas._fillCalls[0].shadowOffsetX).toBe(0);
        expect(canvas._fillCalls[0].shadowBlur).toBe(0);
    });

    it("outline=true calls strokeText before fillText with black + scaled lineWidth", () => {
        const canvas = makeStubbedCanvasWithEffects();
        drawCanvas(canvas, {
            text: "X",
            font_size_px: 100,
            outline: true,
        });
        expect(canvas._events.map((e) => e.kind)).toEqual(["stroke", "fill"]);
        expect(canvas._strokeCalls[0].strokeStyle).toBe("#000000");
        // lineWidth scales with font: 100 * 0.05 = 5
        expect(canvas._strokeCalls[0].lineWidth).toBe(5);
        // Stroke has no shadow regardless of drop_shadow state.
        expect(canvas._strokeCalls[0].shadowOffsetX).toBe(0);
    });

    it("drop_shadow=true sets shadow* before fillText then resets after", () => {
        const canvas = makeStubbedCanvasWithEffects();
        drawCanvas(canvas, {
            text: "X",
            font_size_px: 100,
            drop_shadow: true,
        });
        // 100 * 0.04 = 4 offset; 100 * 0.06 = 6 blur
        expect(canvas._fillCalls[0].shadowOffsetX).toBe(4);
        expect(canvas._fillCalls[0].shadowOffsetY).toBe(4);
        expect(canvas._fillCalls[0].shadowBlur).toBe(6);
        expect(canvas._fillCalls[0].shadowColor).toMatch(/rgba\(0,\s*0,\s*0,\s*0\.7\)/);
    });

    it("outline + drop_shadow: stroke first (no shadow), then fill (with shadow)", () => {
        const canvas = makeStubbedCanvasWithEffects();
        drawCanvas(canvas, {
            text: "X",
            font_size_px: 100,
            outline: true,
            drop_shadow: true,
        });
        const kinds = canvas._events.map((e) => e.kind);
        expect(kinds).toEqual(["stroke", "fill"]);
        // Stroke has no shadow regardless of drop_shadow state
        expect(canvas._strokeCalls[0].shadowOffsetX).toBe(0);
        // Fill carries the shadow at offset (0.04 em)
        expect(canvas._fillCalls[0].shadowOffsetX).toBe(4);
    });

    it("accepts camelCase aliases dropShadow + outlineEnabled", () => {
        const canvas = makeStubbedCanvasWithEffects();
        drawCanvas(canvas, {
            text: "X",
            font_size_px: 100,
            outlineEnabled: true,
            dropShadow: true,
        });
        expect(canvas._strokeCalls).toHaveLength(1);
        expect(canvas._fillCalls[0].shadowOffsetX).toBe(4);
    });

    // Lock the back-compat synthetic-layer field forwarding contract so
    // a future tightening of layersForDraw (e.g. switching back to an
    // explicit allowlist) can't silently drop an effect/font field —
    // the r51 regression that motivated the spread fix.
    it("back-compat synthetic layer forwards every paintLayer field (regression: r51-style silent drop)", () => {
        const canvas = makeStubbedCanvasWithEffects();
        drawCanvas(canvas, {
            text: "X",
            font_size_px: 100,
            outline: true,
            drop_shadow: true,
        });
        // Effect flags reach paintLayer (stroke + shadow visible)
        expect(canvas._strokeCalls).toHaveLength(1);
        expect(canvas._strokeCalls[0].lineWidth).toBe(5); // 100 * 0.05
        expect(canvas._fillCalls[0].shadowOffsetX).toBe(4); // 100 * 0.04
        // And the camelCase aliases too
        const canvas2 = makeStubbedCanvasWithEffects();
        drawCanvas(canvas2, {
            text: "X",
            font_size_px: 100,
            outlineEnabled: true,
            dropShadow: true,
        });
        expect(canvas2._strokeCalls).toHaveLength(1);
        expect(canvas2._fillCalls[0].shadowOffsetX).toBe(4);
    });
});

// ── Per-LETTER shake: base-size condense (qarl 2026-07-16) ─────────
// Sacred review 2026-07-16 caught this the hard way: the per-char
// shake path bypasses BOTH horizontal clamps (fillText's maxWidth and
// the WASM path's min(naturalW, boxW)), and the FYS shake layers are
// authored font_size_pct=100 *specifically* to overflow and be
// squished (seed.py:601-603). Without an explicit condense, "UNCAGE"
// renders ~4.5x oversize (measured 7778px into a 1728px box → ~1.5
// letters visible). The whole UI suite was green through that bug —
// the WASM stub pins isWasmReady=false and nothing exercised the
// clamp. This test exists to fail on exactly that.
//
// NOT about jitter: qarl explicitly wants letters to shake out of
// frame ("that's expected and fine"), and nothing here clamps the
// offsets — this pins the LINE's base size only.
function fakeTextCanvas(width, height, charW) {
    const calls = { fillText: [], scale: [], translate: [] };
    const ctx = {
        font: "", textAlign: "center", textBaseline: "middle",
        fillStyle: "", strokeStyle: "", lineWidth: 1, globalAlpha: 1,
        shadowOffsetX: 0, shadowOffsetY: 0, shadowBlur: 0, shadowColor: "",
        globalCompositeOperation: "source-over",
        save: vi.fn(), restore: vi.fn(),
        clearRect: vi.fn(), fillRect: vi.fn(),
        beginPath: vi.fn(), rect: vi.fn(), clip: vi.fn(),
        drawImage: vi.fn(), strokeText: vi.fn(),
        translate: vi.fn((x, y) => calls.translate.push([x, y])),
        scale: vi.fn((x, y) => calls.scale.push([x, y])),
        // Every char is `charW` wide → an over-wide run is easy to force.
        measureText: (s) => ({
            width: [...String(s)].length * charW,
            actualBoundingBoxAscent: 50,
            actualBoundingBoxDescent: 12,
        }),
        fillText: vi.fn((t, x, y, mw) => calls.fillText.push([t, x, y, mw])),
    };
    return { canvas: { width, height, getContext: () => ctx }, ctx, calls };
}

const SHAKE_ITEM = {
    id: "fys-uncage",
    text_layers: [{
        text: "UNCAGE",
        motion: "shake",
        motion_intensity: 100,
        motion_phase: 0,
        font_size_pct: 100,
        font_family: "sans-serif",
        text_color: "#FFFFFF",
        box: { x: 0.05, y: 0.3, w: 0.9, h: 0.4 },
    }],
};

describe("per-letter shake — base-size condense", () => {
    it("condenses an over-wide shaking line to the box (x-scale < 1)", () => {
        // charW=400 → "UNCAGE" (6 chars) = 2400px of advances against a
        // 0.9*1920 = 1728px box → must condense.
        const { canvas, calls } = fakeTextCanvas(1920, 1080, 400);
        drawTextOnly(canvas, SHAKE_ITEM, { elapsed_s: 0.05 });
        // Per-char draws happened (the shake path ran at all).
        expect(calls.fillText.length).toBeGreaterThan(1);
        // The condense x-scale must be applied. Without it the run
        // draws at full 2400px into a 1728px box (the shipped-bug
        // shape) and no sub-1 x-scale is ever requested.
        const squeezes = calls.scale
            .map(([sx]) => sx)
            .filter((sx) => sx > 0 && sx < 0.999);
        expect(squeezes.length).toBeGreaterThan(0);
        // And it must condense to ~the box, not some arbitrary amount:
        // 1728/2400 = 0.72.
        expect(Math.min(...squeezes)).toBeCloseTo(1728 / 2400, 2);
    });

    it("does NOT condense a line that already fits (x-scale stays 1)", () => {
        // charW=100 → 600px of advances, well inside the 1728px box.
        const { canvas, calls } = fakeTextCanvas(1920, 1080, 100);
        drawTextOnly(canvas, SHAKE_ITEM, { elapsed_s: 0.05 });
        expect(calls.fillText.length).toBeGreaterThan(1);
        const squeezes = calls.scale
            .map(([sx]) => sx)
            .filter((sx) => sx > 0 && sx < 0.999);
        expect(squeezes).toEqual([]);
    });
});

// The FYS shake layers actually take the SQUISH branch (yScale<1):
// seed.py:598-608 authors box h=0.2167 with font_size_pct=100, so real
// Alfa Slab ink (~1642px) vastly exceeds boxH (234px) → yScale ≈ 0.14.
// The fake above pins a constant ascent, so it only ever exercised the
// yScale==1 branch — leaving the dy/yScale compensation (which keeps
// vertical jitter from being damped to ~14% of intent) with NO test.
// This fake scales ink with the font so the squish branch is reached.
function fakeSquishCanvas(width, height, charW) {
    const calls = { fillText: [], scale: [], translate: [] };
    const ctx = {
        font: "", textAlign: "center", textBaseline: "middle",
        fillStyle: "", strokeStyle: "", lineWidth: 1, globalAlpha: 1,
        shadowOffsetX: 0, shadowOffsetY: 0, shadowBlur: 0, shadowColor: "",
        globalCompositeOperation: "source-over",
        save: vi.fn(), restore: vi.fn(),
        clearRect: vi.fn(), fillRect: vi.fn(),
        beginPath: vi.fn(), rect: vi.fn(), clip: vi.fn(),
        drawImage: vi.fn(), strokeText: vi.fn(),
        translate: vi.fn((x, y) => calls.translate.push([x, y])),
        scale: vi.fn((x, y) => calls.scale.push([x, y])),
        measureText(s) {
            // Ink height tracks the active font size → forces yScale<1.
            const px = parseFloat(String(this.font)) || 16;
            return {
                width: [...String(s)].length * charW,
                actualBoundingBoxAscent: px * 0.75,
                actualBoundingBoxDescent: px * 0.25,
            };
        },
        fillText: vi.fn((t, x, y, mw) => calls.fillText.push([t, x, y, mw])),
    };
    return { canvas: { width, height, getContext: () => ctx }, ctx, calls };
}

describe("per-letter shake — squish branch (the FYS-critical path)", () => {
    // FYS-shaped: short box height forces the yScale<1 squish branch.
    const FYS_ITEM = {
        id: "fys-uncage-squish",
        text_layers: [{
            text: "UNCAGE",
            motion: "shake",
            motion_intensity: 100,
            motion_phase: 0,
            font_size_pct: 100,
            font_family: "sans-serif",
            text_color: "#FFF1B0",
            box: { x: 0.05, y: 0.15, w: 0.9, h: 0.2167 },
        }],
    };

    it("takes the squish branch and compensates dy so jitter is not damped", () => {
        // Render the SAME layer + tick two ways so the per-letter dy
        // values are identical, and only the branch differs:
        //   control  — constant-ink fake → totalInkExtent < boxH → yScale == 1
        //   squished — ink tracks font   → totalInkExtent >> boxH → yScale < 1
        // Under the squish branch's outer ctx.scale(1, yScale), a LOCAL
        // dy renders as dy*yScale. The fix divides by yScale so the
        // SCREEN offset matches intent — which shows up as a local y
        // spread ~1/yScale (≈7x here) LARGER than the control's.
        // Without the fix the two spreads are equal → this fails.
        const ctl = fakeTextCanvas(1920, 1080, 100);
        drawTextOnly(ctl.canvas, FYS_ITEM, { elapsed_s: 0.05 });
        const sq = fakeSquishCanvas(1920, 1080, 100);
        drawTextOnly(sq.canvas, FYS_ITEM, { elapsed_s: 0.05 });

        // Guard: control really is un-squished, squished really is.
        const ctlYScales = ctl.calls.scale.map(([, sy]) => sy).filter((sy) => sy > 0 && sy < 0.999);
        expect(ctlYScales).toEqual([]);
        const sqYScales = sq.calls.scale.map(([, sy]) => sy).filter((sy) => sy > 0 && sy < 0.999);
        expect(sqYScales.length).toBeGreaterThan(0);

        // Guard: both actually drew the same letters (else spreads are
        // incomparable and the ratio below would be meaningless).
        const ctlYs = ctl.calls.fillText.map(([, , y]) => y);
        const sqYs = sq.calls.fillText.map(([, , y]) => y);
        expect(ctlYs.length).toBeGreaterThan(1);
        expect(sqYs.length).toBe(ctlYs.length);

        const spread = (ys) => Math.max(...ys) - Math.min(...ys);
        const ctlSpread = spread(ctlYs);
        expect(ctlSpread).toBeGreaterThan(0);   // there IS jitter to compare
        // Measured: compensated spread ≈ 2.99x the control (the exact
        // ratio is 1/yScale modulated by the integer rounding of dy).
        // Uncompensated it is EXACTLY 1x (same dy, same branch math) —
        // verified by mutation: deleting the `yScaleForOffset: yScale`
        // pass-through drops this from 38.9 to 13 (== control). Assert
        // 2x: unreachable without the fix, comfortably clear with it.
        expect(spread(sqYs)).toBeGreaterThan(ctlSpread * 2);
    });

    it("pins the condensed line's left edge to the box (centring)", () => {
        // The fake's measureText is linear, so kerned == unkerned and a
        // drift-right regression can't show up in x directly — pin the
        // origin instead. The squish branch draws in LOCAL space under
        // an outer translate(anchorX=960, ...), so an over-wide centred
        // line (condensed to exactly maxWidth=boxW=1728) must originate
        // at local -864 → absolute 960-864 = 96 = boxX (0.05*1920).
        const { canvas, calls } = fakeSquishCanvas(1920, 1080, 400);
        drawTextOnly(canvas, FYS_ITEM, { elapsed_s: 0.05 });
        expect(calls.translate).toContainEqual([960, expect.any(Number)]);
        expect(calls.translate).toContainEqual([-864, 0]);
    });
});
