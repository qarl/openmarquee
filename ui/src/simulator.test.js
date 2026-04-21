// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
    SETTINGS_BROADCAST_CHANNEL,
    applyWindowSizingForMode,
    drawForSkin,
    drawHub75,
    drawPlain,
    drawWs281x,
    pickSkin,
    resolveSimulatorState,
} from "./simulator.js";

// jsdom doesn't implement <canvas>.getContext, but drawPlain creates
// a temporary canvas and calls putImageData on it. Stub with a tiny
// fake so the test exercises the real code path.
beforeEach(() => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
        putImageData: vi.fn(),
        drawImage: vi.fn(),
        fillRect: vi.fn(),
        getImageData: vi.fn(),
    });
});

// --- pickSkin: output_mode → skin ---

describe("pickSkin", () => {
    it("returns 'plain' for hdmi + composite", () => {
        expect(pickSkin("hdmi")).toBe("plain");
        expect(pickSkin("composite")).toBe("plain");
    });
    it("returns 'hub75' for hub75", () => {
        expect(pickSkin("hub75")).toBe("hub75");
    });
    it("returns 'ws281x' for ws281x", () => {
        expect(pickSkin("ws281x")).toBe("ws281x");
    });
    it("falls back to 'plain' for unknown modes", () => {
        expect(pickSkin(null)).toBe("plain");
        expect(pickSkin("potato")).toBe("plain");
    });
});

// --- applyWindowSizingForMode: opens with correct aspect ratio ---

describe("applyWindowSizingForMode", () => {
    it("calls window.resizeTo with panel aspect for hub75", () => {
        const resizeTo = vi.spyOn(window, "resizeTo").mockImplementation(() => {});
        applyWindowSizingForMode("hub75", 128, 96);
        expect(resizeTo).toHaveBeenCalled();
        const [w, h] = resizeTo.mock.calls[0];
        // 128:96 = 4:3 aspect
        expect(w / h).toBeCloseTo(128 / 96, 1);
        resizeTo.mockRestore();
    });

    it("calls window.resizeTo with 16:9 for an HDMI 1920×1080 sign", () => {
        const resizeTo = vi.spyOn(window, "resizeTo").mockImplementation(() => {});
        applyWindowSizingForMode("plain", 1920, 1080);
        const [w, h] = resizeTo.mock.calls[0];
        expect(w / h).toBeCloseTo(16 / 9, 2);
        resizeTo.mockRestore();
    });

    it("swallows resizeTo errors (browsers that refuse on a tab)", () => {
        vi.spyOn(window, "resizeTo").mockImplementation(() => {
            throw new Error("tabs can't resize");
        });
        // Must not throw.
        expect(() =>
            applyWindowSizingForMode("plain", 128, 96),
        ).not.toThrow();
    });
});

// --- skin draw functions: spy on canvas primitives ---

function makeFakeContext() {
    const calls = [];
    const ctx = {
        fillStyle: "",
        imageSmoothingEnabled: true,
        fillRect: vi.fn((...args) => calls.push(["fillRect", args, ctx.fillStyle])),
        drawImage: vi.fn((...args) =>
            calls.push(["drawImage", args, ctx.fillStyle]),
        ),
        beginPath: vi.fn(() => calls.push(["beginPath"])),
        arc: vi.fn((...args) => calls.push(["arc", args, ctx.fillStyle])),
        fill: vi.fn(() => calls.push(["fill", [], ctx.fillStyle])),
        createRadialGradient: vi.fn(() => {
            const grad = { addColorStop: vi.fn() };
            calls.push(["createRadialGradient"]);
            return grad;
        }),
    };
    return { ctx, calls };
}

function makeImageData(pixels, w, h) {
    // pixels: flat array of [r,g,b] triples row-major. Pad to RGBA.
    const data = new Uint8ClampedArray(w * h * 4);
    for (let i = 0; i < w * h; i++) {
        const [r, g, b] = pixels[i] || [0, 0, 0];
        data[i * 4] = r;
        data[i * 4 + 1] = g;
        data[i * 4 + 2] = b;
        data[i * 4 + 3] = 255;
    }
    return { data, width: w, height: h };
}

describe("drawHub75", () => {
    it("fills the dark panel backdrop then one square per source pixel", () => {
        const { ctx, calls } = makeFakeContext();
        const src = makeImageData(
            [
                [255, 0, 0], [0, 255, 0],
                [0, 0, 255], [0, 0, 0],  // last pixel is dark → dim cell
            ],
            2, 2,
        );
        drawHub75(ctx, 100, 100, src, 2, 2);
        // First fillRect is the panel backdrop (0,0,100,100) in dark gray.
        expect(calls[0][0]).toBe("fillRect");
        expect(calls[0][1]).toEqual([0, 0, 100, 100]);
        expect(calls[0][2]).toBe("#0a0a0a");
        // Then one fillRect per source pixel (4 of them).
        const pixelCalls = calls.filter(
            (c) => c[0] === "fillRect" && c[2] !== "#0a0a0a",
        );
        expect(pixelCalls).toHaveLength(4);
        // The first LED is red at cell (0, 0).
        const firstLed = pixelCalls[0];
        expect(firstLed[2]).toBe("rgb(255,0,0)");
        // The last (dark) LED uses the "off" color, not pure black.
        const lastLed = pixelCalls[3];
        expect(lastLed[2]).toBe("#111113");
    });
});

describe("drawWs281x", () => {
    it("draws a glow + LED core per lit pixel, dim dot per dark pixel", () => {
        const { ctx, calls } = makeFakeContext();
        const src = makeImageData([[255, 0, 0], [0, 0, 0]], 2, 1);
        drawWs281x(ctx, 100, 50, src, 2, 1);
        // Backdrop first.
        expect(calls[0][0]).toBe("fillRect");
        expect(calls[0][2]).toBe("#050505");
        // Lit pixel: createRadialGradient for the glow, then an arc for
        // the core. Off pixel: one arc in dim color, no gradient.
        const gradients = calls.filter((c) => c[0] === "createRadialGradient");
        expect(gradients).toHaveLength(1);
        const arcCalls = calls.filter((c) => c[0] === "arc");
        // Two arcs: one for the lit core, one for the dim off-LED.
        expect(arcCalls).toHaveLength(2);
    });
});

describe("drawPlain", () => {
    it("disables image smoothing and uses drawImage for the scale-up", () => {
        const { ctx, calls } = makeFakeContext();
        const src = makeImageData([[128, 128, 128]], 1, 1);
        // jsdom doesn't fully implement createElement("canvas").getContext,
        // but for drawPlain we just verify the outer ctx calls.
        drawPlain(ctx, 100, 100, src, 1, 1);
        expect(ctx.imageSmoothingEnabled).toBe(false);
        expect(calls[0][0]).toBe("fillRect");  // clear first
        expect(calls[0][2]).toBe("#000");
        // drawImage is the scale-up op.
        expect(calls.some((c) => c[0] === "drawImage")).toBe(true);
    });
});

describe("drawForSkin dispatch", () => {
    it("routes 'hub75' to drawHub75", () => {
        const { ctx, calls } = makeFakeContext();
        const src = makeImageData([[10, 20, 30]], 1, 1);
        drawForSkin("hub75", ctx, 50, 50, src, 1, 1);
        expect(calls[0][2]).toBe("#0a0a0a"); // hub75 backdrop
    });
    it("routes 'ws281x' to drawWs281x", () => {
        const { ctx, calls } = makeFakeContext();
        const src = makeImageData([[10, 20, 30]], 1, 1);
        drawForSkin("ws281x", ctx, 50, 50, src, 1, 1);
        expect(calls[0][2]).toBe("#050505"); // ws281x backdrop
    });
    it("routes unknown / 'plain' to drawPlain", () => {
        const { ctx, calls } = makeFakeContext();
        const src = makeImageData([[10, 20, 30]], 1, 1);
        drawForSkin("plain", ctx, 50, 50, src, 1, 1);
        expect(calls[0][2]).toBe("#000"); // plain backdrop
    });
});

// --- resolveSimulatorState: maps settings payload → draw-state ---

describe("resolveSimulatorState", () => {
    it("derives skin + dims from the settings fetch", async () => {
        const fetchSettings = vi.fn().mockResolvedValue({
            output_mode: "hub75",
            display_width: 128,
            display_height: 96,
            display_rotation: 0,
        });
        const s = await resolveSimulatorState(fetchSettings);
        expect(s).toEqual({
            skin: "hub75",
            signW: 128,
            signH: 96,
            outputMode: "hub75",
        });
    });

    it("swaps dims for portrait rotations", async () => {
        const fetchSettings = vi.fn().mockResolvedValue({
            output_mode: "hdmi",
            display_width: 1920,
            display_height: 1080,
            display_rotation: 90,
        });
        const s = await resolveSimulatorState(fetchSettings);
        expect(s.signW).toBe(1080);
        expect(s.signH).toBe(1920);
    });

    it("falls back to sane defaults when the settings fetch throws", async () => {
        const fetchSettings = vi.fn().mockRejectedValue(new Error("boom"));
        const s = await resolveSimulatorState(fetchSettings);
        expect(s.skin).toBe("plain");
        expect(s.outputMode).toBe("hdmi");
        expect(s.signW).toBe(128);
        expect(s.signH).toBe(96);
    });
});

// --- broadcast channel constant ---

describe("SETTINGS_BROADCAST_CHANNEL", () => {
    it("is the same same-origin channel name both windows agree on", () => {
        // Imported in main.js + simulator.js; test pins it so a rename
        // on one side can't silently desync them.
        expect(SETTINGS_BROADCAST_CHANNEL).toBe("openmarquee-settings");
    });
});
