// Unit tests for canvas-motion.js — the JS port of compose_motion_frame.
// jsdom can't render canvas pixels, so coverage focuses on the
// math (effect frequencies, phase wrapping, deterministic shake) and
// the dispatch shape (paintFn called once for static, twice for
// ticker, zero times for blink-off).

import { describe, expect, it, vi } from "vitest";

import {
    anyLayerAnimated,
    computePhase,
    effectFreq,
    paintLayerWithMotion,
} from "./canvas-motion.js";

function fakeCanvas(w = 100, h = 50) {
    return { width: w, height: h };
}

function fakeCtx() {
    // Just enough to record save / restore / transform / clip without
    // doing any actual rendering.
    const calls = [];
    const ctx = {
        globalAlpha: 1,
        save: vi.fn(() => calls.push(["save"])),
        restore: vi.fn(() => calls.push(["restore"])),
        beginPath: vi.fn(() => calls.push(["beginPath"])),
        rect: vi.fn((...a) => calls.push(["rect", ...a])),
        clip: vi.fn(() => calls.push(["clip"])),
        translate: vi.fn((x, y) => calls.push(["translate", x, y])),
        scale: vi.fn((x, y) => calls.push(["scale", x, y])),
    };
    ctx._calls = calls;
    return ctx;
}

// --- effectFreq ---

describe("effectFreq", () => {
    it("returns 1 Hz for breathe / pulse / bounce regardless of intensity", () => {
        for (const m of ["breathe", "pulse", "bounce"]) {
            expect(effectFreq(m, 0)).toBe(1);
            expect(effectFreq(m, 50)).toBe(1);
            expect(effectFreq(m, 100)).toBe(1);
        }
    });

    it("returns 10 Hz for shake", () => {
        expect(effectFreq("shake", 0)).toBe(10);
        expect(effectFreq("shake", 100)).toBe(10);
    });

    it("ticker frequency is inverse seconds — slow at low intensity, fast at high", () => {
        const slow = effectFreq("ticker", 0);
        const mid = effectFreq("ticker", 50);
        const fast = effectFreq("ticker", 100);
        expect(slow).toBeLessThan(mid);
        expect(mid).toBeLessThan(fast);
        // Spec: 6s cycle at 0, ~3s at 50, 1s at 100. Frequencies: 1/6, 1/3.5, 1/1.
        expect(1 / slow).toBeCloseTo(6, 1);
        expect(1 / fast).toBeCloseTo(1, 1);
    });

    it("blink frequency is piecewise: 0.5 Hz at 0, 1 Hz at 50, 4 Hz at 100", () => {
        expect(effectFreq("blink", 0)).toBeCloseTo(0.5, 5);
        expect(effectFreq("blink", 50)).toBeCloseTo(1.0, 5);
        expect(effectFreq("blink", 100)).toBeCloseTo(4.0, 5);
    });
});

// --- computePhase ---

describe("computePhase", () => {
    it("returns 0 at elapsed=0, motion_phase=0", () => {
        expect(computePhase(0, 1, 0)).toBe(0);
    });

    it("wraps at cycle boundary", () => {
        expect(computePhase(1, 1, 0)).toBeCloseTo(0, 9);
    });

    it("motion_phase shifts the cycle", () => {
        expect(computePhase(0, 1, 0.5)).toBeCloseTo(0.5, 9);
    });

    it("two layers with motion_phase 0 vs 0.5 stay in opposition", () => {
        const a = computePhase(0.3, 1, 0);
        const b = computePhase(0.3, 1, 0.5);
        const diff = ((a - b) % 1 + 1) % 1;
        expect(Math.abs(diff - 0.5)).toBeLessThan(1e-9);
    });
});

// --- anyLayerAnimated ---

describe("anyLayerAnimated", () => {
    it("returns false for empty / undefined input", () => {
        expect(anyLayerAnimated([])).toBe(false);
        expect(anyLayerAnimated(undefined)).toBe(false);
        expect(anyLayerAnimated(null)).toBe(false);
    });

    it("returns false when all layers are static", () => {
        expect(anyLayerAnimated([
            { motion: "static" },
            { motion: "static" },
        ])).toBe(false);
    });

    it("returns true when any visible layer has non-static motion", () => {
        expect(anyLayerAnimated([
            { motion: "static" },
            { motion: "breathe" },
        ])).toBe(true);
    });

    it("ignores hidden animated layers (visible: false doesn't drive rAF)", () => {
        expect(anyLayerAnimated([
            { motion: "ticker", visible: false },
            { motion: "static" },
        ])).toBe(false);
    });

    it("treats absent motion as static", () => {
        expect(anyLayerAnimated([{}, { text: "x" }])).toBe(false);
    });
});

// --- paintLayerWithMotion dispatch shape ---

describe("paintLayerWithMotion", () => {
    it("calls paintFn once for static (no opts.elapsed_s consulted)", () => {
        const ctx = fakeCtx();
        const paintFn = vi.fn();
        paintLayerWithMotion(ctx, fakeCanvas(), { motion: "static" }, paintFn, {
            elapsed_s: 0.5,
        });
        expect(paintFn).toHaveBeenCalledTimes(1);
        expect(ctx.save).not.toHaveBeenCalled(); // no clip / transform setup
    });

    it("calls paintFn once when opts.elapsed_s is undefined (legacy callers)", () => {
        const ctx = fakeCtx();
        const paintFn = vi.fn();
        paintLayerWithMotion(ctx, fakeCanvas(), { motion: "ticker" }, paintFn, {});
        expect(paintFn).toHaveBeenCalledTimes(1);
        expect(ctx.save).not.toHaveBeenCalled();
    });

    it("calls paintFn TWICE for ticker (two copies for the wrap effect)", () => {
        const ctx = fakeCtx();
        const paintFn = vi.fn();
        paintLayerWithMotion(
            ctx, fakeCanvas(),
            { motion: "ticker", motion_intensity: 50, motion_phase: 0 },
            paintFn,
            { elapsed_s: 0.5 },
        );
        expect(paintFn).toHaveBeenCalledTimes(2);
    });

    it("blink phase < 0.5 calls paintFn (visible)", () => {
        const ctx = fakeCtx();
        const paintFn = vi.fn();
        // 1 Hz blink, elapsed=0 → phase=0 → visible.
        paintLayerWithMotion(
            ctx, fakeCanvas(),
            { motion: "blink", motion_intensity: 50, motion_phase: 0 },
            paintFn,
            { elapsed_s: 0 },
        );
        expect(paintFn).toHaveBeenCalledTimes(1);
    });

    it("blink phase >= 0.5 does NOT call paintFn (off-half)", () => {
        const ctx = fakeCtx();
        const paintFn = vi.fn();
        // 1 Hz blink at elapsed=0.6 → phase=0.6 → off.
        paintLayerWithMotion(
            ctx, fakeCanvas(),
            { motion: "blink", motion_intensity: 50, motion_phase: 0 },
            paintFn,
            { elapsed_s: 0.6 },
        );
        expect(paintFn).not.toHaveBeenCalled();
    });

    it("breathe at quarter-phase applies a scale transform around box center", () => {
        const ctx = fakeCtx();
        const paintFn = vi.fn();
        // intensity=100, phase=0.25 → scale 1.20 around box center.
        paintLayerWithMotion(
            ctx, fakeCanvas(100, 100),
            {
                motion: "breathe",
                motion_intensity: 100,
                motion_phase: 0.25,  // sin(2π·0.25) = 1 → max scale
                box: { x: 0, y: 0, w: 1, h: 1 },
            },
            paintFn,
            { elapsed_s: 0 },
        );
        // ctx.scale should have been called with 1.2 (within rounding).
        const scaleCall = ctx.scale.mock.calls[0];
        expect(scaleCall[0]).toBeCloseTo(1.2, 5);
        expect(scaleCall[1]).toBeCloseTo(1.2, 5);
    });

    it("ticker at phase=0 starts unshifted (translate(0, 0) then translate(boxW, 0))", () => {
        const ctx = fakeCtx();
        const paintFn = vi.fn();
        paintLayerWithMotion(
            ctx, fakeCanvas(100, 50),
            {
                motion: "ticker",
                motion_intensity: 50,
                motion_phase: 0,
                box: { x: 0, y: 0, w: 1, h: 1 },
            },
            paintFn,
            { elapsed_s: 0 },
        );
        // Two translates: first by shift=0, then by box width=100.
        const translates = ctx.translate.mock.calls;
        expect(translates.length).toBe(2);
        // toBeCloseTo on each axis to dodge JS -0 vs 0 (phase=0 → shift=-0).
        expect(translates[0][0]).toBeCloseTo(0, 9);
        expect(translates[0][1]).toBeCloseTo(0, 9);
        expect(translates[1][0]).toBeCloseTo(100, 9);
        expect(translates[1][1]).toBeCloseTo(0, 9);
    });

    it("pulse at intensity=100 phase=0.75 multiplies globalAlpha by 0 (full extinction)", () => {
        const ctx = fakeCtx();
        ctx.globalAlpha = 1;
        const paintFn = vi.fn(() => {
            // At paintFn time, globalAlpha should reflect the modulation.
            expect(ctx.globalAlpha).toBeCloseTo(0, 5);
        });
        paintLayerWithMotion(
            ctx, fakeCanvas(),
            {
                motion: "pulse",
                motion_intensity: 100,
                motion_phase: 0,
                box: { x: 0, y: 0, w: 1, h: 1 },
            },
            paintFn,
            { elapsed_s: 0.75 }, // sin(2π·0.75) = -1 → minimum alpha
        );
        expect(paintFn).toHaveBeenCalledTimes(1);
    });

    it("unknown motion value falls through to static (forward-compat)", () => {
        const ctx = fakeCtx();
        const paintFn = vi.fn();
        paintLayerWithMotion(
            ctx, fakeCanvas(),
            { motion: "wave", motion_intensity: 50, motion_phase: 0 }, // not a real effect
            paintFn,
            { elapsed_s: 0.5 },
        );
        // Unknown motion falls through to a single static paint
        // (no clip — only ticker clips post-parity-Bug-3).
        expect(paintFn).toHaveBeenCalledTimes(1);
    });

    it("ticker installs the box clip (its two-copy wrap mechanism needs it)", () => {
        const ctx = fakeCtx();
        paintLayerWithMotion(
            ctx, fakeCanvas(100, 50),
            {
                motion: "ticker",
                motion_intensity: 50,
                motion_phase: 0,
                box: { x: 0.1, y: 0.2, w: 0.5, h: 0.6 },
            },
            vi.fn(),
            { elapsed_s: 0 },
        );
        // The rect call should match the box's pixel rect.
        const rectCall = ctx.rect.mock.calls[0];
        expect(rectCall).toEqual([10, 10, 50, 30]); // 0.1*100, 0.2*50, 0.5*100, 0.6*50
        expect(ctx.clip).toHaveBeenCalled();
    });

    it("displacement effects do NOT clip to the box (parity Bug 3)", () => {
        // Parity Bug 3 (2026-05-19): the Rust device renderer never
        // clips text to the layer box — displaced text spills past
        // the box, bounded only by the screen. shake / breathe /
        // bounce in the editor preview must match that: no clip.
        // Only ticker keeps the clip (its wrap mechanism needs it).
        for (const motion of ["shake", "breathe", "bounce"]) {
            const ctx = fakeCtx();
            paintLayerWithMotion(
                ctx, fakeCanvas(100, 50),
                {
                    motion,
                    motion_intensity: 80,
                    motion_phase: 0,
                    box: { x: 0.1, y: 0.2, w: 0.5, h: 0.6 },
                },
                vi.fn(),
                { elapsed_s: 0.3 },
            );
            expect(ctx.clip, `${motion} must not clip`).not.toHaveBeenCalled();
        }
    });

    it("shake at intensity=0 produces no translate (regression)", () => {
        const ctx = fakeCtx();
        const paintFn = vi.fn();
        paintLayerWithMotion(
            ctx, fakeCanvas(100, 100),
            { motion: "shake", motion_intensity: 0, motion_phase: 0 },
            paintFn,
            { elapsed_s: 0.5 },
        );
        // intensity=0 → no translate call.
        expect(ctx.translate).not.toHaveBeenCalled();
        expect(paintFn).toHaveBeenCalledTimes(1);
    });

    it("shake is deterministic — same layerKey + phase + step → same translate", () => {
        const layer = {
            motion: "shake",
            motion_intensity: 100,
            motion_phase: 0.3,
            box: { x: 0, y: 0, w: 1, h: 1 },
        };
        const ctxA = fakeCtx();
        const ctxB = fakeCtx();
        paintLayerWithMotion(ctxA, fakeCanvas(100, 100), layer, vi.fn(), {
            elapsed_s: 0.05, layerKey: "slide-1:0",
        });
        paintLayerWithMotion(ctxB, fakeCanvas(100, 100), layer, vi.fn(), {
            elapsed_s: 0.05, layerKey: "slide-1:0",
        });
        // Translate calls should be identical.
        expect(ctxA.translate.mock.calls).toEqual(ctxB.translate.mock.calls);
    });

    it("shake produces DIFFERENT offsets for different layerKey at the same phase", () => {
        const layer = {
            motion: "shake",
            motion_intensity: 100,
            motion_phase: 0.3,
            box: { x: 0, y: 0, w: 1, h: 1 },
        };
        const ctxA = fakeCtx();
        const ctxB = fakeCtx();
        paintLayerWithMotion(ctxA, fakeCanvas(100, 100), layer, vi.fn(), {
            elapsed_s: 0.05, layerKey: "slide-1:0",
        });
        paintLayerWithMotion(ctxB, fakeCanvas(100, 100), layer, vi.fn(), {
            elapsed_s: 0.05, layerKey: "slide-1:1",  // different layer index
        });
        // At least one of the two translate calls (dx, dy) should differ.
        // (Tiny chance they collide; the FNV-1a + Box-Muller chain makes
        // that essentially impossible for distinct seeds.)
        expect(ctxA.translate.mock.calls).not.toEqual(ctxB.translate.mock.calls);
    });
});
