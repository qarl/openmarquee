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

    it("text-over-video slot caches the bg video by its referenced id (Phase 5b)", async () => {
        // Phase 5b — SYSTEM_SPEC §5.10. A TextSlide that references a
        // saved VideoSlide as its background composes text over the
        // moving video frames in the inline preview. The bg video is
        // cached on the same `videoCache` the standalone VideoSlide
        // path uses, keyed by the bg video's id (NOT the parent text
        // slide's id) — that lets a single playlist with both a
        // standalone VideoSlide AND a Text-over-Video referencing it
        // share one <video> element instead of double-decoding.
        //
        // Stub getBoundingClientRect so sizeCanvasToStage doesn't bail
        // on jsdom's 0×0 layout; without this, renderOnce → drawSlot
        // → getCachedVideo never fires and the cache stays empty.
        vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
            { width: 200, height: 150, top: 0, left: 0, bottom: 150, right: 200, x: 0, y: 0 },
        );
        const createElementSpy = vi.spyOn(document, "createElement");
        const container = document.createElement("div");
        mountInlinePreview(container, {
            width: 128,
            height: 96,
            outputMode: "hdmi",
            fetchPlaylist: async () => ({
                items: [
                    {
                        item_id: "text-1",
                        transition: "cut",
                        transition_ms: 0,
                        content: {
                            id: "text-1",
                            type: "text_slide",
                            text: "Happy Hour",
                            text_color: "#FFFFFF",
                            font_size_pct: 25,
                            background_video_slide_id: "bgvid-1",
                            duration_ms: 5000,
                            auto_mode: null,
                        },
                    },
                ],
            }),
        });
        await tick();
        await tick();
        await tick();
        // The mount path lazily creates a hidden <video> for the bg
        // video on first frame draw (renderOnce → drawSlot →
        // drawTextOverVideo → getCachedVideo). Confirm a <video>
        // element was created with the bg video's id in the src
        // (NOT the parent text-slide's id — that'd be a 404).
        // Pull every <video> element the spy minted; map calls→results
        // by index so we keep the right pairing (mock.results[i] is the
        // return of mock.calls[i]).
        const createdVideos = createElementSpy.mock.results
            .filter(
                (_, i) =>
                    String(createElementSpy.mock.calls[i][0]).toLowerCase() === "video",
            )
            .map((r) => r.value);
        expect(createdVideos.length).toBeGreaterThan(0);
        // The video's src should reference bgvid-1, not text-1.
        const videoEl = createdVideos[0];
        expect(videoEl.src).toMatch(/\/api\/content\/bgvid-1\/video/);
        expect(videoEl.src).not.toMatch(/text-1/);
    });

    it("standalone video slot still caches the video by content id (regression: getCachedVideo signature took item, now takes id)", async () => {
        // Phase 5b refactored syncActiveVideo / drawVideo / getCachedVideo
        // to take a video-id string instead of a content item, so the
        // text-over-video path can pass the bg slide's id while the
        // standalone video path passes the item's own id. This pins
        // that the standalone path still resolves the cache under the
        // item's id — a regression here would 404 on /video.
        vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
            { width: 200, height: 150, top: 0, left: 0, bottom: 150, right: 200, x: 0, y: 0 },
        );
        const createElementSpy = vi.spyOn(document, "createElement");
        const container = document.createElement("div");
        mountInlinePreview(container, {
            width: 128,
            height: 96,
            outputMode: "hdmi",
            fetchPlaylist: async () => ({
                items: [
                    {
                        item_id: "vid-7",
                        transition: "cut",
                        transition_ms: 0,
                        content: {
                            id: "vid-7",
                            type: "video",
                            duration_ms: 5000,
                            pipeline: "h264_mp4",
                        },
                    },
                ],
            }),
        });
        await tick();
        await tick();
        await tick();
        const createdVideos = createElementSpy.mock.results
            .filter(
                (_, i) =>
                    String(createElementSpy.mock.calls[i][0]).toLowerCase() === "video",
            )
            .map((r) => r.value);
        expect(createdVideos.length).toBeGreaterThan(0);
        expect(createdVideos[0].src).toMatch(/\/api\/content\/vid-7\/video/);
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
