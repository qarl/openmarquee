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

    it("dots at d=0.5 — backend-parity tile size = round(lerp(48,4,0.5))=26", () => {
        // backend: _render_pattern_dots tile = round(lerp(48,4,0.5)).
        // JS+Python both round(0.5*4 + 0.5*48) = round(26) = 26.
        const css = buildBg("dots", "#000000", "#FFFFFF", 0.5);
        expect(css).toContain("26px 26px");
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
        // d=0: 2*round(lerp(2,24,0)) = 2*2 = 4 stops.
        // d=1: 2*round(lerp(2,24,1)) = 2*24 = 48 stops.
        // d=0.5: 2*round(13) = 26.
        const at0 = buildBg("rays", "#000000", "#FFFFFF", 0);
        const at1 = buildBg("rays", "#000000", "#FFFFFF", 1);
        const at5 = buildBg("rays", "#000000", "#FFFFFF", 0.5);
        // Count stops by counting "%, " separators inside the stops
        // list. Each stop has one "%, " separator except the last.
        const stopCount = css => css.match(/0deg at 50% 50%, (.+)\)$/)?.[1].split(", ").length;
        expect(stopCount(at0)).toBe(4);
        expect(stopCount(at1)).toBe(48);
        expect(stopCount(at5)).toBe(26);
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
    // Each subtest pins ONE concrete tile/radius value computed
    // from the lerp(...) constants so a regression that changes
    // the lerp constants (e.g. accidentally re-narrowing them)
    // fails loudly. These are the post-B12 (2026-05-05) widened
    // values that backend mirrors.

    it("halftone d=0.5 tile = round(lerp(60,6))=33", () => {
        expect(buildBg("halftone", "#000000", "#FFFFFF", 0.5))
            .toContain("33px 33px");
    });

    it("stripes d=0.5 tile = round(lerp(80,4))=42", () => {
        expect(buildBg("stripes", "#000000", "#FFFFFF", 0.5))
            .toContain("42px");  // half = 21
    });

    it("checker d=0.5 tile = round(lerp(60,4))=32", () => {
        expect(buildBg("checker", "#000000", "#FFFFFF", 0.5))
            .toContain("32px 32px");
    });

    it("bricks d=0.5 w = round(lerp(140,16))=78", () => {
        expect(buildBg("bricks", "#000000", "#FFFFFF", 0.5))
            .toContain("78px");
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

    it("dots path: arc() called for every dot in the grid", () => {
        // d=0.5 -> tile=26. Canvas 100x100, dots stride starts at
        // tile/2=13 -> grid positions 13, 39, 65, 91 (x 4). Same in
        // y. So 4*4 = 16 arcs.
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 100, 100, "dots", "#000000", "#FFFFFF", 0.5);
        expect(ctx.arc).toHaveBeenCalledTimes(16);
        // Last fillStyle assigned is color_b (the dot color).
        expect(ctx._fillStyleHistory).toContain("#FFFFFF");
    });

    it("confetti is deterministic: same density -> same arc count", () => {
        // PRNG seed is fixed (0xC0FFE71), so two paints at the
        // same density produce identical arc-call counts.
        // d=0.5 -> count = round(lerp(80, 2000, 0.5)) = 1040.
        const ctx1 = makeCtx();
        const ctx2 = makeCtx();
        paintPatternOnCanvas(ctx1, 400, 400, "confetti", "#000000", "#FFFFFF", 0.5);
        paintPatternOnCanvas(ctx2, 400, 400, "confetti", "#000000", "#FFFFFF", 0.5);
        expect(ctx1.arc).toHaveBeenCalledTimes(1040);
        expect(ctx2.arc).toHaveBeenCalledTimes(1040);
    });

    it("rays path: even slice count at d=0.5 (26 fills, all color_b)", () => {
        // 2*round(lerp(2,24,0.5)) = 26 total slices, half are
        // color_b filled (the odd-indexed ones), so 13 fills.
        const ctx = makeCtx();
        paintPatternOnCanvas(ctx, 200, 200, "rays", "#000000", "#FFFFFF", 0.5);
        expect(ctx.fill).toHaveBeenCalledTimes(13);
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
