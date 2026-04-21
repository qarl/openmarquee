// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountLivePreview } from "./live-preview.js";

// Drive state transitions via explicit handle.refresh() calls so the
// tests don't depend on setInterval timing. Each test stops the
// returned handle in an afterEach closure so stray timers don't leak
// into the next test.
const openHandles = [];

function mount(container, fetchState) {
    const handle = mountLivePreview(container, {
        width: 128,
        height: 96,
        fetchState,
        // Very long interval so setInterval effectively never fires during
        // a test — refresh() is invoked manually below.
        pollIntervalMs: 1_000_000,
    });
    openHandles.push(handle);
    return handle;
}

afterEach(() => {
    while (openHandles.length) openHandles.pop().stop();
});

describe("mountLivePreview", () => {
    it("renders the idle state when playback is not running", async () => {
        const fetchState = vi.fn().mockResolvedValue({
            is_running: false,
            current_item_id: null,
            current_item_type: null,
            current_playlist_name: null,
        });
        const container = document.createElement("div");
        const handle = mount(container, fetchState);
        await handle.refresh();
        expect(container.querySelector(".live-preview-idle")).not.toBeNull();
        expect(container.querySelector(".live-preview-media")).toBeNull();
    });

    it("renders an <img> for text_slide / image types", async () => {
        const fetchState = vi.fn().mockResolvedValue({
            is_running: true,
            current_item_id: "abc-123",
            current_item_type: "text_slide",
            current_playlist_name: "default",
        });
        const container = document.createElement("div");
        const handle = mount(container, fetchState);
        await handle.refresh();
        const img = container.querySelector("img.live-preview-media");
        expect(img).not.toBeNull();
        expect(img.getAttribute("src")).toBe("/api/content/abc-123/asset");
        expect(container.querySelector("video")).toBeNull();
        expect(container.querySelector(".live-preview-caption").textContent).toBe(
            "Playing: default",
        );
    });

    it("renders a <video> for video types and hits the video endpoint", async () => {
        const fetchState = vi.fn().mockResolvedValue({
            is_running: true,
            current_item_id: "vid-42",
            current_item_type: "video",
            current_playlist_name: "default",
        });
        const container = document.createElement("div");
        const handle = mount(container, fetchState);
        await handle.refresh();
        const video = container.querySelector("video.live-preview-media");
        expect(video).not.toBeNull();
        expect(video.getAttribute("src")).toBe("/api/content/vid-42/video");
        expect(video.hasAttribute("autoplay")).toBe(true);
        expect(video.hasAttribute("muted")).toBe(true);
        expect(video.hasAttribute("loop")).toBe(true);
        expect(container.querySelector("img")).toBeNull();
    });

    it("only swaps the media element when the current_item_id changes", async () => {
        // Queue-backed fake: the constructor kicks off one auto-refresh,
        // so we push the "steady state" twice — once for the mount's
        // internal call, once for the first explicit refresh — before
        // the transition.
        const queue = [
            { id: "a", type: "video" },
            { id: "a", type: "video" },
            { id: "a", type: "video" },
            { id: "b", type: "image" },
        ].map((s) => ({
            is_running: true,
            current_item_id: s.id,
            current_item_type: s.type,
            current_playlist_name: "default",
        }));
        const fetchState = vi.fn().mockImplementation(async () => {
            return queue.shift() ?? queue[queue.length - 1];
        });
        const container = document.createElement("div");
        const handle = mount(container, fetchState);

        await handle.refresh();
        const firstVideo = container.querySelector("video");
        expect(firstVideo).not.toBeNull();

        // Same id → element is reused (critical — otherwise video playback
        // would reset every poll).
        await handle.refresh();
        expect(container.querySelector("video")).toBe(firstVideo);

        // New id + type → swap to <img>.
        await handle.refresh();
        expect(container.querySelector("video")).toBeNull();
        const img = container.querySelector("img.live-preview-media");
        expect(img).not.toBeNull();
        expect(img.getAttribute("src")).toBe("/api/content/b/asset");
    });

    it("shows a friendly caption when the state endpoint is unreachable", async () => {
        const fetchState = vi.fn().mockRejectedValue(new Error("ECONN"));
        const container = document.createElement("div");
        const handle = mount(container, fetchState);
        await handle.refresh();
        expect(container.querySelector(".live-preview-caption").textContent).toMatch(
            /Preview paused/,
        );
    });

    it("stop() halts polling so future ticks don't trigger fetches", async () => {
        const fetchState = vi.fn().mockResolvedValue({
            is_running: false,
            current_item_id: null,
            current_item_type: null,
            current_playlist_name: null,
        });
        const container = document.createElement("div");
        const handle = mount(container, fetchState);
        await handle.refresh();
        const before = fetchState.mock.calls.length;
        handle.stop();
        await handle.refresh();
        expect(fetchState.mock.calls.length).toBe(before);
    });
});
