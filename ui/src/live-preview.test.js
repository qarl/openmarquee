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
            current_item_pipeline: "h264_mp4",
            current_playlist_name: "default",
        });
        const container = document.createElement("div");
        const handle = mount(container, fetchState);
        await handle.refresh();
        const video = container.querySelector("video.live-preview-media");
        expect(video).not.toBeNull();
        expect(video.getAttribute("src")).toBe("/api/content/vid-42/video");
        // Property-based: createElement("video").autoplay is true.
        expect(video.autoplay).toBe(true);
        expect(video.muted).toBe(true);
        expect(video.loop).toBe(true);
        expect(container.querySelector("img")).toBeNull();
    });

    it("falls back to the thumbnail <img> for raw_frames videos", async () => {
        const fetchState = vi.fn().mockResolvedValue({
            is_running: true,
            current_item_id: "panel-7",
            current_item_type: "video",
            current_item_pipeline: "raw_frames",
            current_playlist_name: "default",
        });
        const container = document.createElement("div");
        const handle = mount(container, fetchState);
        await handle.refresh();
        expect(container.querySelector("video")).toBeNull();
        const img = container.querySelector("img.live-preview-media");
        expect(img).not.toBeNull();
        expect(img.getAttribute("src")).toBe("/api/content/panel-7/asset");
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

    it("cross-fades between items when the outgoing transition is 'fade'", async () => {
        // Three states: mount auto-refresh (item A, fade), first explicit
        // refresh (item A again), second explicit refresh (item B) — so
        // the id changes AFTER lastTransition has captured A's 'fade'.
        const queue = [
            {
                is_running: true,
                current_item_id: "a",
                current_item_type: "image",
                current_item_pipeline: null,
                current_item_transition: "fade",
                current_item_transition_ms: 500,
                current_playlist_name: "default",
            },
            {
                is_running: true,
                current_item_id: "a",
                current_item_type: "image",
                current_item_pipeline: null,
                current_item_transition: "fade",
                current_item_transition_ms: 500,
                current_playlist_name: "default",
            },
            {
                is_running: true,
                current_item_id: "b",
                current_item_type: "image",
                current_item_pipeline: null,
                current_item_transition: "cut",
                current_item_transition_ms: 0,
                current_playlist_name: "default",
            },
        ];
        const fetchState = vi.fn().mockImplementation(async () =>
            queue.shift() ?? queue[queue.length - 1],
        );
        const container = document.createElement("div");
        const stage = () => container.querySelector(".live-preview-stage");

        const handle = mount(container, fetchState);
        await handle.refresh();
        // A is on stage; no fade-in on the very first slide.
        expect(container.querySelectorAll(".live-preview-media").length).toBe(1);
        expect(
            stage().classList.contains("live-preview-stage--transitioning"),
        ).toBe(false);

        // Transition from A → B — should cross-fade (both elements present).
        await handle.refresh();
        const mediaDuring = container.querySelectorAll(".live-preview-media");
        expect(mediaDuring.length).toBe(2);
        expect(
            stage().classList.contains("live-preview-stage--transitioning"),
        ).toBe(true);
        // Outgoing element has the leaving class; new one has entering.
        expect(mediaDuring[0].classList.contains("live-preview-media--leaving")).toBe(
            true,
        );
        expect(mediaDuring[1].classList.contains("live-preview-media--entering")).toBe(
            true,
        );
    });

    it("uses an instant cut when the outgoing transition is 'cut'", async () => {
        const queue = [
            {
                is_running: true,
                current_item_id: "a",
                current_item_type: "image",
                current_item_pipeline: null,
                current_item_transition: "cut",
                current_item_transition_ms: 0,
                current_playlist_name: "default",
            },
            {
                is_running: true,
                current_item_id: "a",
                current_item_type: "image",
                current_item_pipeline: null,
                current_item_transition: "cut",
                current_item_transition_ms: 0,
                current_playlist_name: "default",
            },
            {
                is_running: true,
                current_item_id: "b",
                current_item_type: "image",
                current_item_pipeline: null,
                current_item_transition: "cut",
                current_item_transition_ms: 0,
                current_playlist_name: "default",
            },
        ];
        const fetchState = vi.fn().mockImplementation(async () =>
            queue.shift() ?? queue[queue.length - 1],
        );
        const container = document.createElement("div");
        const handle = mount(container, fetchState);
        await handle.refresh();
        await handle.refresh();
        expect(
            container.querySelectorAll(".live-preview-media").length,
        ).toBe(1);
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
