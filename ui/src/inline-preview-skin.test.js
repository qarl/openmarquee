// Boundary tests for the skin-draw family (drawHub75 today; drawWs281x
// shares the LED_DARK_SUM threshold via the exported constant).
//
// These tests pass a custom mock ctx directly to drawHub75 — they
// bypass jsdom's getContext stub entirely, so this file deliberately
// runs in the default node environment (no `// @vitest-environment
// jsdom` directive). That keeps them runnable when the jsdom package
// is not resolvable in node_modules (e.g. virtiofs partial-install on
// macOS dev), and the boundary assertions don't need a DOM anyway.
//
// What's locked:
//   1. LED_DARK_SUM == 18 boundary: R+G+B == 17 is OFF; == 18 is ON.
//      This is the off-by-one that bare-eye verification misses; the
//      threshold is shared with drawWs281x, so the lock here covers
//      drift in both consumers.
//   2. Cell-inset math: with signW=signH=1 the bezel inset equals
//      cellW * HUB75_CELL_GAP / 2 on each side.
import { describe, expect, it } from "vitest";
import {
    drawHub75,
    HUB75_BACKDROP,
    HUB75_CELL_GAP,
    HUB75_DARK_COLOR,
    LED_DARK_SUM,
} from "./inline-preview.js";

function makeSrc(r, g, b) {
    const data = new Uint8ClampedArray(4);
    data[0] = r;
    data[1] = g;
    data[2] = b;
    data[3] = 255;
    return { data, width: 1, height: 1 };
}

// Mock ctx that records the SEQUENCE of (fillStyle assignment, fillRect
// call) pairs. drawHub75's contract is:
//   1. backdrop fill: ctx.fillStyle = HUB75_BACKDROP; ctx.fillRect(0,0,w,h)
//   2. for each cell: ctx.fillStyle = <per-pixel>; ctx.fillRect(...)
// The mock captures the fillStyle value AT THE TIME fillRect was called,
// not at the time of assignment, which is what matters for color-per-rect
// assertions.
function makeRecorder() {
    let currentFillStyle = null;
    const fills = [];
    const ctx = {
        get fillStyle() {
            return currentFillStyle;
        },
        set fillStyle(v) {
            currentFillStyle = v;
        },
        fillRect(x, y, w, h) {
            fills.push({ color: currentFillStyle, x, y, w, h });
        },
        // drawHub75 doesn't use these paths but the function-table is
        // here for completeness in case the implementation evolves.
        beginPath() {},
        arc() {},
        fill() {},
        createRadialGradient() {
            return { addColorStop() {} };
        },
    };
    return { ctx, fills };
}

describe("drawHub75 boundaries", () => {
    it("R+G+B == 17 is treated as off (HUB75_DARK_COLOR)", () => {
        const { ctx, fills } = makeRecorder();
        // 6 + 6 + 5 = 17, just below the threshold.
        drawHub75(ctx, 20, 20, makeSrc(6, 6, 5), 1, 1);
        // First fill is the backdrop covering the whole canvas; the
        // single cell is the second fill.
        expect(fills[0]).toEqual({ color: HUB75_BACKDROP, x: 0, y: 0, w: 20, h: 20 });
        expect(fills[1].color).toBe(HUB75_DARK_COLOR);
    });

    it("R+G+B == 18 is treated as on (rgb color from source)", () => {
        const { ctx, fills } = makeRecorder();
        // 6 + 6 + 6 = 18, exactly at the threshold (strict <, so this is ON).
        drawHub75(ctx, 20, 20, makeSrc(6, 6, 6), 1, 1);
        expect(fills[1].color).toBe("rgb(6,6,6)");
    });

    it("cell rect inset matches HUB75_CELL_GAP / 2 on each side", () => {
        const { ctx, fills } = makeRecorder();
        drawHub75(ctx, 20, 20, makeSrc(255, 255, 255), 1, 1);
        // signW=signH=1, w=h=20 → cellW=cellH=20.
        // drawW = drawH = cellW * (1 - HUB75_CELL_GAP) = 20 * 0.82 = 16.4
        // offX = offY = (cellW - drawW) / 2 = (20 - 16.4) / 2 = 1.8
        const expectedDraw = 20 * (1 - HUB75_CELL_GAP);
        const expectedOff = (20 - expectedDraw) / 2;
        expect(fills[1].x).toBeCloseTo(expectedOff, 6);
        expect(fills[1].y).toBeCloseTo(expectedOff, 6);
        expect(fills[1].w).toBeCloseTo(expectedDraw, 6);
        expect(fills[1].h).toBeCloseTo(expectedDraw, 6);
    });
});

describe("LED_DARK_SUM constant", () => {
    // Lock the value at module level so a future tweak surfaces here
    // rather than silently shifting both skins' contrast.
    it("equals 18", () => {
        expect(LED_DARK_SUM).toBe(18);
    });
});
