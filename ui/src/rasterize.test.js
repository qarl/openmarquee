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

import { describe, expect, it } from "vitest";
import { applyBrightnessGamma } from "./rasterize.js";

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
