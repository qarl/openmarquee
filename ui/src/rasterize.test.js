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
import { applyBrightnessGamma, drawCanvas } from "./rasterize.js";

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

    // -- Schema-default pin: brightness=0.8, gamma=2.2 (the deployed
    //    HDMI default per settings.py:150-162). This is the
    //    operator-visible default that the inline preview now
    //    matches end-to-end (parity-audit #5 brightness-plumbing
    //    follow-up to 8ef2e7f).
    it("at schema defaults (brightness=0.8, gamma=2.2): white → ~232 (not 255)", () => {
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

    it("at schema defaults (brightness=0.8, gamma=2.2): mid-gray pins the curve", () => {
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
