// Tests for the bg-system module (12 patterns from the
// 2026-05-03 designer handoff). Per QA Batch 2 dispatch + qarl
// canon: backend parity matters since the editor canvas and the
// device renderer must produce visually-equivalent output.
//
// Parity targets (NOT pixel-perfect — same tile sizes / dot
// radii / band spacing):
//   * dots:     tile = round(lerp(48, 4, d)), r = max(2, round(tile*0.22))
//   * halftone: tile = round(lerp(60, 6, d)), r = round(tile*0.34)
//   * stripes:  tile = round(lerp(80, 4, d))
//   * scanlines:tile = round(lerp(16, 2, d))
//   * checker:  tile = round(lerp(60, 4, d))
//   * grid:     tile = round(lerp(120, 4, d)) [+ max(4)]
//   * rings:    tile = round(lerp(120, 6, d)) [+ max(4)]
//   * rays:     slices = 2*round(lerp(2, 24, d)), always even
//   * confetti: count = round(lerp(80, 2000, d))
//   * bricks:   w = round(lerp(140, 16, d)) [+ max(8)]
// Backend mirror: backend/openmarquee/auto_render.py:_render_pattern_*
// uses identical lerp() values. Both call sites round() the same.
//
// Confetti caveat: JS LCG seed 0xC0FFE71 and Python PRNG diverge
// in pixel output BY DESIGN (header line 379 of bg-system.js).
// Determinism PER SURFACE is what's tested, not cross-surface parity.

import { describe, expect, it, vi } from "vitest";
import {
    PATTERN_NAMES,
    PATTERN_LABELS,
    buildBg,
    patternUsesColorB,
    patternUsesDensity,
    densityLabelFor,
    paintPatternOnCanvas,
} from "./bg-system.js";

// Manual stub 2D context — jsdom doesn't ship Canvas 2D, and
// pulling in node-canvas just for these tests is overkill.
// All paint methods become vi.fn() so we can assert call counts
// and fillStyle assignments.
function makeCtx() {
    const ctx = {
        save: vi.fn(),
        restore: vi.fn(),
        fillRect: vi.fn(),
        beginPath: vi.fn(),
        arc: vi.fn(),
        fill: vi.fn(),
        stroke: vi.fn(),
        moveTo: vi.fn(),
        closePath: vi.fn(),
        rotate: vi.fn(),
        addColorStop: vi.fn(),
        // createLinearGradient returns a "gradient" object that has
        // addColorStop; record its calls on the outer ctx for ease
        // of assertion.
        createLinearGradient: vi.fn(),
        _fillStyleHistory: [],
        _lineWidthHistory: [],
        _strokeStyleHistory: [],
    };
    // Track every fillStyle assignment so the test can verify the
    // sequence (e.g., grid paints b first then a).
    Object.defineProperty(ctx, "fillStyle", {
        get() { return this._fillStyle; },
        set(v) { this._fillStyle = v; this._fillStyleHistory.push(v); },
    });
    Object.defineProperty(ctx, "strokeStyle", {
        get() { return this._strokeStyle; },
        set(v) { this._strokeStyle = v; this._strokeStyleHistory.push(v); },
    });
    Object.defineProperty(ctx, "lineWidth", {
        get() { return this._lineWidth; },
        set(v) { this._lineWidth = v; this._lineWidthHistory.push(v); },
    });
    const gradient = { addColorStop: vi.fn() };
    ctx.createLinearGradient.mockReturnValue(gradient);
    ctx._gradient = gradient;
    return ctx;
}

describe("PATTERN_NAMES + PATTERN_LABELS", () => {
    it("has 12 entries (solid + 11 pattern variants)", () => {
        // Backend parity: BackgroundPattern in
        // backend/openmarquee/models.py accepts the same 12 names
        // (per test_pattern_model_accepts_all_pattern_names).
        expect(PATTERN_NAMES).toHaveLength(12);
    });

    it("every PATTERN_NAMES entry has a PATTERN_LABELS entry", () => {
        for (const name of PATTERN_NAMES) {
            expect(PATTERN_LABELS[name]).toBeDefined();
            expect(typeof PATTERN_LABELS[name]).toBe("string");
            expect(PATTERN_LABELS[name].length).toBeGreaterThan(0);
        }
    });

    it("PATTERN_LABELS has no orphans (no labels without a name)", () => {
        for (const name of Object.keys(PATTERN_LABELS)) {
            expect(PATTERN_NAMES).toContain(name);
        }
    });
});

describe("buildBg -- CSS string output", () => {
    it("solid returns the color_a string verbatim", () => {
        expect(buildBg("solid", "#FF0000", "#00FF00", 0.5)).toBe("#FF0000");
    });

    it("unknown pattern falls back to solid (color_a only)", () => {
        // Forward-compat: backend has the same fallback for
        // schema versions that add patterns ahead of the client.
        expect(buildBg("not-a-real-pattern", "#123456", "#FEDCBA", 0.5))
            .toBe("#123456");
    });

    it("gradient density 0 -> 0deg, density 1 -> 270deg", () => {
        // Backend parity:
        // _render_pattern_gradient(angle_deg = round(lerp(0,270,d))).
        expect(buildBg("gradient", "#000000", "#FFFFFF", 0))
            .toBe("linear-gradient(0deg, #000000, #FFFFFF)");
        expect(buildBg("gradient", "#000000", "#FFFFFF", 1))
            .toBe("linear-gradient(270deg, #000000, #FFFFFF)");
    });

    it("gradient at d=0.5 produces ~135deg", () => {
        expect(buildBg("gradient", "#000000", "#FFFFFF", 0.5))
            .toBe("linear-gradient(135deg, #000000, #FFFFFF)");
    });

    it("dots at d=0.5 — curved-d=0.25; tile = round(lerp(48,4,0.25))=37", () => {
        // qarl-curve 2026-05-12: density slider passes through
        // densityCurve(d)=d^2 BEFORE the lerp for size-bearing
        // patterns. At d=0.5 the effective density is 0.25, so
        // dots tile = round(0.75*48 + 0.25*4) = round(37) = 37.
        // Backend (_render_pattern_dots) applies _density_curve too,
        // so JS + Python land on the same 37.
        const css = buildBg("dots", "#000000", "#FFFFFF", 0.5);
        expect(css).toContain("37px 37px");
    });

    it("dots references both color_a and color_b", () => {
        const css = buildBg("dots", "#AB0000", "#00CD00", 0.5);
        expect(css).toContain("#AB0000");
        expect(css).toContain("#00CD00");
    });

    it("density 0 and density 1 produce different output for tile-driven patterns", () => {
        // Smoke check across all 11 non-solid patterns.
        const nonSolid = PATTERN_NAMES.filter(n => n !== "solid");
        for (const p of nonSolid) {
            const lo = buildBg(p, "#000000", "#FFFFFF", 0);
            const hi = buildBg(p, "#000000", "#FFFFFF", 1);
            expect(lo).not.toBe(hi);
        }
    });

    it("rays slice count is always even (B15)", () => {
        // Verify by extracting the conic-gradient stops.
        // d=0: 2*round(lerp(2,24,0^2=0)) = 2*2 = 4 stops.
        // d=1: 2*round(lerp(2,24,1^2=1)) = 2*24 = 48 stops.
        // d=0.5 (curved->0.25): 2*round(lerp(2,24,0.25))
        //                     = 2*round(7.5) = 2*8 = 16 stops.
        const at0 = buildBg("rays", "#000000", "#FFFFFF", 0);
        const at1 = buildBg("rays", "#000000", "#FFFFFF", 1);
        const at5 = buildBg("rays", "#000000", "#FFFFFF", 0.5);
        const stopCount = css => css.match(/0deg at 50% 50%, (.+)\)$/)?.[1].split(", ").length;
        expect(stopCount(at0)).toBe(4);
        expect(stopCount(at1)).toBe(48);
        expect(stopCount(at5)).toBe(16);
        // All counts even.
        for (const n of [stopCount(at0), stopCount(at1), stopCount(at5)]) {
            expect(n % 2).toBe(0);
        }
    });

    it("lerp clamps t to [0, 1] -- d<0 same as 0, d>1 same as 1", () => {
        // Defensive: prevents UI sliders sending out-of-range
        // values from corrupting tile dimensions to negatives or
        // overflows.
        expect(buildBg("gradient", "#000000", "#FFFFFF", -0.5))
            .toBe(buildBg("gradient", "#000000", "#FFFFFF", 0));
        expect(buildBg("gradient", "#000000", "#FFFFFF", 1.5))
            .toBe(buildBg("gradient", "#000000", "#FFFFFF", 1));
    });
});

describe("buildBg -- per-pattern tile-size parity smoke", () => {
    // Each subtest pins ONE concrete tile value computed from the
    // lerp(...) constants AFTER the qarl-curve 2026-05-12 transform
    // (densityCurve(d) = d^2 before lerp for size-bearing patterns).
    // At d=0.5 the effective density is 0.25.

    it("halftone d=0.5 (curved 0.25) tile = round(lerp(60,6,0.25))=47", () => {
        // lerp(60,6,0.25) = 46.5 exactly. JS Math.round is half-up
        // (-> 47); Python round() is banker's half-to-even (-> 46).
        // 1-pixel JS/Python disagreement at this specific d; not a
        // regression vs the pre-curve world (the old d=0.5 tile was
        // 33, no half-tie). Documented for future parity-harness
        // sanity passes.
        expect(buildBg("halftone", "#000000", "#FFFFFF", 0.5))
            .toContain("47px 47px");
    });

    it("stripes d=0.5 (curved 0.25) tile = round(lerp(80,4,0.25))=61", () => {
        expect(buildBg("stripes", "#000000", "#FFFFFF", 0.5))
            .toContain("61px");  // half = 30.5
    });

    it("checker d=0.5 (curved 0.25) tile = round(lerp(60,4,0.25))=46", () => {
        expect(buildBg("checker", "#000000", "#FFFFFF", 0.5))
            .toContain("46px 46px");
    });

    it("bricks d=0.5 (curved 0.25) w = round(lerp(140,16,0.25))=109", () => {
        expect(buildBg("bricks", "#000000", "#FFFFFF", 0.5))
            .toContain("109px");
    });
});

describe("patternUsesColorB / patternUsesDensity / densityLabelFor", () => {
    it("patternUsesColorB is false ONLY for solid", () => {
        expect(patternUsesColorB("solid")).toBe(false);
        for (const p of PATTERN_NAMES.filter(n => n !== "solid")) {
            expect(patternUsesColorB(p)).toBe(true);
        }
    });

    it("patternUsesDensity is false ONLY for solid", () => {
        expect(patternUsesDensity("solid")).toBe(false);
        for (const p of PATTERN_NAMES.filter(n => n !== "solid")) {
            expect(patternUsesDensity(p)).toBe(true);
        }
    });

    it("densityLabelFor returns 'Angle' for gradient, 'Density' else", () => {
        expect(densityLabelFor("gradient")).toBe("Angle");
        for (const p of PATTERN_NAMES.filter(n => n !== "gradient")) {
            expect(densityLabelFor(p)).toBe("Density");
        }
    });
});

describe("paintPatternOnCanvas", () => {
    it("solid path: single base fillRect, no other draw calls", () => {
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 100, 50, "solid", "#AB0000", "#00CD00", 0.5);
        expect(ctx.save).toHaveBeenCalledTimes(1);
        expect(ctx.restore).toHaveBeenCalledTimes(1);
        expect(ctx.fillRect).toHaveBeenCalledTimes(1);
        expect(ctx.fillRect).toHaveBeenCalledWith(0, 0, 100, 50);
        // Only color_a touched.
        expect(ctx._fillStyleHistory).toEqual(["#AB0000"]);
        expect(ctx.arc).not.toHaveBeenCalled();
    });

    it("unknown pattern paints color_a base then returns (forward-compat)", () => {
        const ctx = makeCtx();
        paintPatternOnCanvas(
            ctx, 100, 50, "future-pattern", "#AB0000", "#00CD00", 0.5,
        );
        // Same effect as solid -- a single base fill of color_a,
        // no further drawing, save/restore symmetric.
        expect(ctx.fillRect).toHaveBeenCalledTimes(1);
        expect(ctx._fillStyleHistory).toEqual(["#AB0000"]);
        expect(ctx.save).toHaveBeenCalledTimes(1);
        expect(ctx.restore).toHaveBeenCalledTimes(1);
    });

    it("gradient path: createLinearGradient + 2 stops + filled rect", () => {
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 200, 100, "gradient", "#000000", "#FFFFFF", 0.5);
        expect(ctx.createLinearGradient).toHaveBeenCalledTimes(1);
        expect(ctx._gradient.addColorStop).toHaveBeenCalledTimes(2);
        expect(ctx._gradient.addColorStop).toHaveBeenNthCalledWith(1, 0, "#000000");
        expect(ctx._gradient.addColorStop).toHaveBeenNthCalledWith(2, 1, "#FFFFFF");
    });

    it("gradient at angleDeg=0: line goes BOTTOM→TOP, matching CSS (a@bottom, b@top)", () => {
        // Round-19 regression-lock for the gradient direction fix
        // (`dy = -Math.cos(rad)`). At density=0 the angleDeg is
        // round(lerp(0, 270, 0)) = 0deg. Per CSS spec,
        // `linear-gradient(0deg, a, b)` puts a at BOTTOM, b at TOP
        // (gradient line points up). createLinearGradient endpoints
        // must therefore be (cx, BOTTOM)→(cx, TOP) so stop 0 (=a)
        // lands at the bottom and stop 1 (=b) at the top.
        //
        // Pre-fix (dy = +cos(rad)): the line went (cx, TOP)→(cx,
        // BOTTOM), putting a@TOP/b@BOTTOM. The CSS picker thumbnail
        // showed the opposite of the canvas slide → operator-visible
        // WYSIWYG break.
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 200, 100, "gradient", "#AA0000", "#0000BB", 0);
        const [x0, y0, x1, y1] = ctx.createLinearGradient.mock.calls[0];
        // Vertical line through center: x0 === x1 === 100.
        expect(x0).toBeCloseTo(100, 5);
        expect(x1).toBeCloseTo(100, 5);
        // y0 (stop 0 / a) at BOTTOM (y=100), y1 (stop 1 / b) at TOP (y=0).
        expect(y0).toBeCloseTo(100, 5);
        expect(y1).toBeCloseTo(0, 5);
    });

    it("rays at slices=4: ctx.arc starts at 12 o'clock (-π/2 offset), matching CSS conic", () => {
        // Round-19 regression-lock for the rays angle-origin fix.
        // CSS `conic-gradient(from 0deg)` starts the first slice at
        // 12 o'clock and sweeps clockwise. Canvas ctx.arc measures
        // from +x (3 o'clock); without the -π/2 offset, slice 0
        // would start at 3 o'clock, rotating the pattern 90° from
        // CSS.
        //
        // At density=0, slices = 2*round(lerp(2, 24, 0)) = 4 total.
        // The paint loop fills the ODD-indexed slices only (1 and
        // 3); slices 0 and 2 stay color_a from the initial fillRect.
        // Each step = 2π/4 = π/2. With angleOffset = -π/2:
        //   i=1: a0=-π/2 + π/2 = 0 (3 o'clock); a1=-π/2 + π = π/2 (6 o'clock)
        //   i=3: a0=-π/2 + 3π/2 = π (9 o'clock); a1=-π/2 + 2π = 3π/2 (12 o'clock)
        // Equivalently in CSS terms: slice 0 (a) spans 12→3 o'clock;
        // slice 1 (b) 3→6; slice 2 (a) 6→9; slice 3 (b) 9→12. The
        // -π/2 offset is what makes "slice 0 starts at 12" hold.
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 200, 200, "rays", "#000000", "#FFFFFF", 0);
        expect(ctx.arc).toHaveBeenCalledTimes(2);
        const step = Math.PI / 2;
        const offset = -Math.PI / 2;
        // i=1: a0 = offset + 1*step = 0; a1 = offset + 2*step = π/2.
        const [, , , a0_1, a1_1] = ctx.arc.mock.calls[0];
        expect(a0_1).toBeCloseTo(offset + 1 * step, 5);
        expect(a1_1).toBeCloseTo(offset + 2 * step, 5);
        // i=3: a0 = offset + 3*step = π; a1 = offset + 4*step = 3π/2.
        const [, , , a0_3, a1_3] = ctx.arc.mock.calls[1];
        expect(a0_3).toBeCloseTo(offset + 3 * step, 5);
        expect(a1_3).toBeCloseTo(offset + 4 * step, 5);
    });

    it("dots path: arc() called for every dot in the grid", () => {
        // d=0.5 (curved 0.25) -> tile=37. Canvas 100x100, dots
        // stride starts at tile/2=18 -> grid positions 18, 55, 92
        // (x 3). Same in y. So 3*3 = 9 arcs.
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 100, 100, "dots", "#000000", "#FFFFFF", 0.5);
        expect(ctx.arc).toHaveBeenCalledTimes(9);
        // Last fillStyle assigned is color_b (the dot color).
        expect(ctx._fillStyleHistory).toContain("#FFFFFF");
    });

    it("confetti is deterministic: same density -> same arc count", () => {
        // PRNG seed is fixed (0xC0FFE71), so two paints at the
        // same density produce identical arc-call counts.
        // d=0.5 (curved 0.25) -> count = round(lerp(80,2000,0.25)) = 560.
        const ctx1 = makeCtx();
        const ctx2 = makeCtx();
        paintPatternOnCanvas(ctx1, 400, 400, "confetti", "#000000", "#FFFFFF", 0.5);
        paintPatternOnCanvas(ctx2, 400, 400, "confetti", "#000000", "#FFFFFF", 0.5);
        expect(ctx1.arc).toHaveBeenCalledTimes(560);
        expect(ctx2.arc).toHaveBeenCalledTimes(560);
    });

    it("rays path: even slice count at d=0.5 (curved 0.25 -> 16 slices, 8 fills)", () => {
        // 2*round(lerp(2,24,0.25)) = 16 total slices, half are
        // color_b filled (the odd-indexed ones), so 8 fills.
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 200, 200, "rays", "#000000", "#FFFFFF", 0.5);
        expect(ctx.fill).toHaveBeenCalledTimes(8);
    });

    it("grid path: repaints base to color_b, then draws color_a lines", () => {
        // After the unconditional color_a base fill, the grid
        // branch immediately repaints with color_b, then sets
        // fillStyle to color_a for the line draws.
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 200, 200, "grid", "#AB0000", "#00CD00", 0.5);
        // History should be: color_a (base), color_b (paper),
        // color_a (lines).
        expect(ctx._fillStyleHistory[0]).toBe("#AB0000");
        expect(ctx._fillStyleHistory[1]).toBe("#00CD00");
        expect(ctx._fillStyleHistory).toContain("#AB0000");
    });

    it("rings path: stroke (not fill) with color_b + lineWidth=2", () => {
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 200, 200, "rings", "#000000", "#FFFFFF", 0.5);
        expect(ctx._strokeStyleHistory).toContain("#FFFFFF");
        expect(ctx._lineWidthHistory).toContain(2);
        expect(ctx.stroke).toHaveBeenCalled();
    });

    it("save/restore pair around the pattern branch (no leaks)", () => {
        // Even on an error, the finally-block in
        // paintPatternOnCanvas keeps save/restore counts in sync.
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 50, 50, "stripes", "#000000", "#FFFFFF", 0.5);
        expect(ctx.save).toHaveBeenCalledTimes(2);     // outer + inner rotate
        expect(ctx.restore).toHaveBeenCalledTimes(2);
    });
});
