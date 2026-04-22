// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formatSec, mountInlinePreview, pickSkin } from "./inline-preview.js";

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

beforeEach(() => {
    // jsdom doesn't implement canvas getContext; the preview's
    // drawImage / putImageData paths need at least a no-op ctx.
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
        putImageData: vi.fn(),
        drawImage: vi.fn(),
        fillRect: vi.fn(),
        getImageData: vi.fn(() => ({ data: new Uint8ClampedArray(0) })),
        createRadialGradient: vi.fn(() => ({ addColorStop: vi.fn() })),
        beginPath: vi.fn(),
        arc: vi.fn(),
        fill: vi.fn(),
        set globalAlpha(_v) {},
        set fillStyle(_v) {},
        set imageSmoothingEnabled(_v) {},
    });
});

afterEach(() => {
    vi.restoreAllMocks();
});

describe("pickSkin", () => {
    it("maps output_mode to skin", () => {
        expect(pickSkin("hdmi")).toBe("plain");
        expect(pickSkin("composite")).toBe("plain");
        expect(pickSkin("hub75")).toBe("hub75");
        expect(pickSkin("ws281x")).toBe("ws281x");
        expect(pickSkin(null)).toBe("plain");
    });
});

describe("formatSec", () => {
    it("formats seconds as M:SS", () => {
        expect(formatSec(0)).toBe("0:00");
        expect(formatSec(5)).toBe("0:05");
        expect(formatSec(65)).toBe("1:05");
        expect(formatSec(3600)).toBe("60:00");
    });
    it("handles nulls + negatives gracefully", () => {
        expect(formatSec(null)).toBe("0:00");
        expect(formatSec(-10)).toBe("0:00");
    });
});

describe("mountInlinePreview", () => {
    it("renders the idle message when the playlist is empty", async () => {
        const container = document.createElement("div");
        mountInlinePreview(container, {
            width: 128,
            height: 96,
            outputMode: "hdmi",
            fetchPlaylist: async () => ({ items: [] }),
        });
        await tick();
        const idle = container.querySelector(".inline-preview-idle");
        expect(idle).not.toBeNull();
        expect(idle.hidden).toBe(false);
        // Transport controls render regardless.
        expect(container.querySelector(".inline-preview-play")).not.toBeNull();
        expect(container.querySelector(".inline-preview-scrub")).not.toBeNull();
        expect(container.querySelector(".inline-preview-time")).not.toBeNull();
    });

    it("hides the idle message when the playlist has items", async () => {
        const container = document.createElement("div");
        mountInlinePreview(container, {
            width: 128,
            height: 96,
            outputMode: "hdmi",
            fetchPlaylist: async () => ({
                items: [
                    {
                        item_id: "a",
                        transition: "cut",
                        transition_ms: 0,
                        content: {
                            id: "a",
                            type: "text_slide",
                            duration_ms: 5000,
                            auto_mode: null,
                        },
                    },
                ],
            }),
        });
        await tick();
        await tick();
        expect(container.querySelector(".inline-preview-idle").hidden).toBe(
            true,
        );
    });

    it("exposes total duration on the scrub slider (sum of item durations)", async () => {
        const container = document.createElement("div");
        mountInlinePreview(container, {
            width: 128,
            height: 96,
            outputMode: "hdmi",
            fetchPlaylist: async () => ({
                items: [
                    {
                        item_id: "a",
                        transition: "cut",
                        transition_ms: 0,
                        content: {
                            id: "a",
                            type: "text_slide",
                            duration_ms: 5000,
                            auto_mode: null,
                        },
                    },
                    {
                        item_id: "b",
                        transition: "cut",
                        transition_ms: 0,
                        content: {
                            id: "b",
                            type: "image",
                            duration_ms: 3000,
                        },
                    },
                ],
            }),
        });
        await tick();
        await tick();
        const slider = container.querySelector(".inline-preview-scrub");
        // 5s + 3s = 8s
        expect(Number(slider.max)).toBeCloseTo(8, 1);
    });

    it("play button toggles aria-label between play/pause", async () => {
        const container = document.createElement("div");
        mountInlinePreview(container, {
            width: 128,
            height: 96,
            outputMode: "hdmi",
            fetchPlaylist: async () => ({ items: [] }),
        });
        await tick();
        const btn = container.querySelector(".inline-preview-play");
        expect(btn.getAttribute("aria-label")).toBe("play or pause");
        btn.click();
        expect(btn.getAttribute("aria-label")).toBe("pause");
        btn.click();
        expect(btn.getAttribute("aria-label")).toBe("play");
    });

    it("stop() halts playback and removes the resize listener", async () => {
        const removeSpy = vi.spyOn(window, "removeEventListener");
        const container = document.createElement("div");
        const handle = mountInlinePreview(container, {
            width: 128,
            height: 96,
            outputMode: "hdmi",
            fetchPlaylist: async () => ({ items: [] }),
        });
        await tick();
        handle.stop();
        expect(removeSpy).toHaveBeenCalledWith("resize", expect.any(Function));
    });
});
