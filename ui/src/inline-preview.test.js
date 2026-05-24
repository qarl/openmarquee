// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// Mock drawCanvas BEFORE importing inline-preview so the regression
// tests can verify wire-shape → state-shape conversion. The non-mock
// tests don't read this; vi.fn() is a no-op for them.
vi.mock("./rasterize.js", async (importOriginal) => {
    const actual = await importOriginal();
    return { ...actual, drawCanvas: vi.fn() };
});
import { drawCanvas } from "./rasterize.js";
import {
    ANIMATED_TRANSITIONS,
    formatSec,
    mountInlinePreview,
    pickSkin,
} from "./inline-preview.js";

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

describe("ANIMATED_TRANSITIONS", () => {
    // Parity-audit #2 fix (2026-05-14): "cut" is one of the 16
    // transition kinds the device handles. Including it in the
    // animated set keeps the preview's progress-window math symmetric
    // with the device. Pin the membership so a future "trim the
    // animated set" refactor can't silently drop it again.
    it("includes 'cut' (parity-audit #2)", () => {
        expect(ANIMATED_TRANSITIONS.has("cut")).toBe(true);
    });

    it("contains all 16 transition kinds the device supports", () => {
        // Source of truth: renderer/src/hdmi_logic.rs:fs_for_transition_kind
        // + backend/openmarquee/playback.py transition dispatch. Both
        // ship 16 kinds total.
        const expected = [
            "cut", "fade", "wipe", "slide", "iris", "scroll", "flip",
            "marquee", "dissolve", "pixelate", "halftone", "scanline",
            "glitch", "push", "blinds", "shutter",
        ];
        expect(ANIMATED_TRANSITIONS.size).toBe(16);
        for (const k of expected) {
            expect(ANIMATED_TRANSITIONS.has(k)).toBe(true);
        }
    });
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

    it("text-slide WITH motion routes through drawCanvas (not cached PNG)", async () => {
        // Bug 1b (qarl 2026-05-02): motion in the playlist's inline-
        // preview during playback. A text_slide whose layers include
        // motion != static must NOT freeze on the cached PNG —
        // drawSlot sends it through drawTextSlideAnimated which now
        // (post-2026-05-13 consolidation) calls drawCanvas directly.
        //
        // Distinguishing signal: drawCanvas (mocked at top of file)
        // gets called for animated text slides; the static drawImage
        // path uses ctx.drawImage against the cached PNG and never
        // invokes drawCanvas.
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
            x: 0, y: 0, top: 0, left: 0, right: 416, bottom: 234,
            width: 416, height: 234, toJSON: () => ({}),
        });
        drawCanvas.mockClear();
        const container = document.createElement("div");
        document.body.appendChild(container);
        try {
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
                                background_color: "#000",
                                text_layers: [
                                    {
                                        text: "GO",
                                        motion: "ticker",
                                        motion_intensity: 50,
                                        motion_phase: 0,
                                        text_color: "#FFF",
                                        box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 },
                                    },
                                ],
                            },
                        },
                    ],
                }),
            });
            await tick();
            await tick();
            expect(drawCanvas).toHaveBeenCalled();
        } finally {
            container.remove();
        }
    });

    it("animated text-slide with bg pattern routes pattern through drawCanvas (UNCAGE regression)", async () => {
        // qarl-direct 2026-05-13: before this fix, drawTextSlideAnimated
        // hand-rolled bg fill that only handled background_color +
        // background_image_slide_id, silently dropping
        // background_pattern. UNCAGE's amber→scarlet gradient was
        // invisible in the playlist preview. After the fix,
        // drawTextSlideAnimated routes through drawCanvas via
        // stateFromItem so pattern bg is paint-time honored. This
        // test pins the wire-shape → state-shape contract: when the
        // playlist contains an animated slide with background_pattern,
        // drawCanvas is called with bgSource=pattern + the pattern
        // payload intact.
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
            x: 0, y: 0, top: 0, left: 0, right: 416, bottom: 234,
            width: 416, height: 234, toJSON: () => ({}),
        });
        drawCanvas.mockClear();
        const container = document.createElement("div");
        document.body.appendChild(container);
        try {
            mountInlinePreview(container, {
                width: 1920,
                height: 1080,
                outputMode: "hdmi",
                fetchPlaylist: async () => ({
                    items: [
                        {
                            item_id: "uncage",
                            transition: "cut",
                            transition_ms: 0,
                            content: {
                                id: "uncage",
                                type: "text_slide",
                                duration_ms: 1700,
                                background_color: "#050608",
                                background_pattern: {
                                    pattern: "gradient",
                                    color_a: "#FFB43C",
                                    color_b: "#5E1A1A",
                                    density: 0.0,
                                },
                                text_layers: [
                                    {
                                        text: "UNCAGE\nYOUR SIGN!!",
                                        motion: "shake",
                                        motion_intensity: 70,
                                        motion_phase: 0,
                                        text_color: "#FFF1B0",
                                        font_family: "Alfa Slab One",
                                        font_size_pct: 33,
                                        box: { x: 0.05, y: 0.15, w: 0.9, h: 0.65 },
                                    },
                                ],
                            },
                        },
                    ],
                }),
            });
            await tick();
            await tick();
            // First renderOnce fires synchronously in refresh() →
            // drawSlot → drawTextSlideAnimated → drawCanvas.
            expect(drawCanvas).toHaveBeenCalled();
            const [, state, opts] = drawCanvas.mock.calls[0];
            expect(state.bgSource).toBe("pattern");
            expect(state.bgPattern).toMatchObject({
                pattern: "gradient",
                color_a: "#FFB43C",
                color_b: "#5E1A1A",
                density: 0.0,
            });
            expect(state.layers).toHaveLength(1);
            expect(state.layers[0].text).toBe("UNCAGE\nYOUR SIGN!!");
            // elapsed_s is position-within-slot, 0 at slot entry.
            expect(opts).toMatchObject({ elapsed_s: expect.any(Number) });
        } finally {
            container.remove();
        }
    });

    it("animated text-slide with solid bg passes bgSource=color through drawCanvas", async () => {
        // Companion to the pattern regression: confirm the
        // wire-shape → state-shape mapping handles the non-pattern
        // case identically. Same code path; different input.
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
            x: 0, y: 0, top: 0, left: 0, right: 416, bottom: 234,
            width: 416, height: 234, toJSON: () => ({}),
        });
        drawCanvas.mockClear();
        const container = document.createElement("div");
        document.body.appendChild(container);
        try {
            mountInlinePreview(container, {
                width: 1920,
                height: 1080,
                outputMode: "hdmi",
                fetchPlaylist: async () => ({
                    items: [
                        {
                            item_id: "solid",
                            transition: "cut",
                            transition_ms: 0,
                            content: {
                                id: "solid",
                                type: "text_slide",
                                duration_ms: 1700,
                                background_color: "#112233",
                                text_layers: [
                                    {
                                        text: "PLAIN",
                                        motion: "pulse",
                                        motion_intensity: 50,
                                        motion_phase: 0,
                                        text_color: "#FFF",
                                        box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 },
                                    },
                                ],
                            },
                        },
                    ],
                }),
            });
            await tick();
            await tick();
            expect(drawCanvas).toHaveBeenCalled();
            const [, state] = drawCanvas.mock.calls[0];
            expect(state.bgSource).toBe("color");
            expect(state.backgroundColor).toBe("#112233");
            expect(state.bgPattern).toBeNull();
            expect(state.bgImage).toBeNull();
        } finally {
            container.remove();
        }
    });

    it("animated text-slide elapsed_s advances with playback position", async () => {
        // Lock the timing contract: elapsed_s passed to drawCanvas is
        // (position - slot.startSec). A scrub to t=0.8s on a single-
        // slot playlist should produce drawCanvas calls with
        // elapsed_s≈0.8. Pins the motion-phase clock semantics — the
        // same elapsed_s the editor preview consumes.
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
            x: 0, y: 0, top: 0, left: 0, right: 416, bottom: 234,
            width: 416, height: 234, toJSON: () => ({}),
        });
        drawCanvas.mockClear();
        const container = document.createElement("div");
        document.body.appendChild(container);
        try {
            mountInlinePreview(container, {
                width: 1920,
                height: 1080,
                outputMode: "hdmi",
                fetchPlaylist: async () => ({
                    items: [
                        {
                            item_id: "uncage",
                            transition: "cut",
                            transition_ms: 0,
                            content: {
                                id: "uncage",
                                type: "text_slide",
                                duration_ms: 1700,
                                background_color: "#000",
                                text_layers: [
                                    {
                                        text: "X",
                                        motion: "shake",
                                        motion_intensity: 70,
                                        motion_phase: 0,
                                        text_color: "#FFF",
                                        box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 },
                                    },
                                ],
                            },
                        },
                    ],
                }),
            });
            await tick();
            await tick();
            // Scrub to t=0.8s.
            const slider = container.querySelector(".inline-preview-scrub");
            slider.value = "0.8";
            slider.dispatchEvent(new Event("input"));
            await tick();
            // Last drawCanvas call reflects the new position.
            const lastCall = drawCanvas.mock.calls[drawCanvas.mock.calls.length - 1];
            expect(lastCall[2].elapsed_s).toBeCloseTo(0.8, 1);
        } finally {
            container.remove();
        }
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


// State-machine + race tests for the playback transport. The existing
// suite covers the render path heavily (drawCanvas dispatch under
// different slide shapes) but the playback state machine (play / pause
// / scrub / refresh × position interactions) was almost entirely
// untested. These 8 tests close the gap surfaced by the 2026-05-24
// test-coverage audit.

describe("mountInlinePreview — playback state machine", () => {
    // Deterministic raf control. The transport tests assert "raf was
    // cancelled" / "another tick did NOT fire" — bare real-time raf
    // would flake under load. Each test that needs timing control
    // installs this harness in its body so non-raf tests stay simple.
    function installRafMock() {
        const rafCallbacks = [];
        const rafSpy = vi
            .spyOn(window, "requestAnimationFrame")
            .mockImplementation((cb) => {
                const id = rafCallbacks.length;
                rafCallbacks.push({ id, cb, cancelled: false, fired: false });
                return id;
            });
        const cancelSpy = vi
            .spyOn(window, "cancelAnimationFrame")
            .mockImplementation((id) => {
                if (rafCallbacks[id]) rafCallbacks[id].cancelled = true;
            });
        function tickOnce(timestamp) {
            // Fire the next callback that hasn't been cancelled and
            // hasn't already fired. Returns false when there's nothing
            // left to fire (the loop is genuinely stopped).
            const next = rafCallbacks.find((c) => !c.cancelled && !c.fired);
            if (!next) return false;
            next.fired = true;
            next.cb(timestamp);
            return true;
        }
        // Per QA reinforcement: belt-and-suspenders cleanup. restoreAllMocks
        // (in the file-level afterEach) restores the spies; we also drop
        // the rafCallbacks array reference so it can't outlive the test
        // closure if some leftover scheduled work resolved late.
        function reset() {
            rafSpy.mockRestore();
            cancelSpy.mockRestore();
            rafCallbacks.length = 0;
        }
        return { rafCallbacks, tickOnce, reset };
    }

    // Stage layout: jsdom returns 0×0 for getBoundingClientRect by
    // default so sizeCanvasToStage bails early. Stub a non-zero rect
    // so renderOnce paths actually fire (the tick assertions need the
    // render side-effects to be reachable).
    function stubStageRect() {
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
            x: 0, y: 0, top: 0, left: 0, right: 416, bottom: 234,
            width: 416, height: 234, toJSON: () => ({}),
        });
    }

    function makePlaylist(items) {
        return {
            items: items.map((item, i) => ({
                item_id: item.id ?? `i${i}`,
                transition: item.transition || "cut",
                transition_ms: item.transition_ms || 0,
                content: {
                    id: item.id ?? `i${i}`,
                    type: item.type || "text_slide",
                    duration_ms: item.duration_ms || 5000,
                    text_layers: item.text_layers,
                    background_color: item.background_color || "#000",
                },
            })),
        };
    }

    it("pause cancels the raf loop (no further ticks fire)", async () => {
        stubStageRect();
        const raf = installRafMock();
        try {
            drawCanvas.mockClear();
            const container = document.createElement("div");
            document.body.appendChild(container);
            try {
                mountInlinePreview(container, {
                    width: 128, height: 96, outputMode: "hdmi",
                    fetchPlaylist: async () => makePlaylist([
                        {
                            id: "a", duration_ms: 5000,
                            text_layers: [{ text: "X", motion: "shake", motion_intensity: 70, motion_phase: 0, box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }],
                        },
                    ]),
                });
                await tick();
                await tick();

                const playBtn = container.querySelector(".inline-preview-play");
                playBtn.click(); // start playing → first raf scheduled
                raf.tickOnce(1000); // primes lastTick = 1000
                raf.tickOnce(2000); // advances position by ~1s
                const drawCallsAtPause = drawCanvas.mock.calls.length;
                playBtn.click(); // pause → cancelAnimationFrame fired

                // Try to fire any pending raf — should return false
                // because the last-scheduled raf was cancelled.
                const fired = raf.tickOnce(3000);
                expect(fired).toBe(false);
                // And no extra drawCanvas calls landed.
                expect(drawCanvas.mock.calls.length).toBe(drawCallsAtPause);
            } finally {
                container.remove();
            }
        } finally {
            raf.reset();
        }
    });

    it("play → pause → play preserves position (does not reset to 0)", async () => {
        stubStageRect();
        const raf = installRafMock();
        try {
            const container = document.createElement("div");
            document.body.appendChild(container);
            try {
                mountInlinePreview(container, {
                    width: 128, height: 96, outputMode: "hdmi",
                    fetchPlaylist: async () => makePlaylist([
                        { id: "a", duration_ms: 10000,
                          text_layers: [{ text: "X", motion: "shake", motion_intensity: 70, motion_phase: 0, box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }] },
                    ]),
                });
                await tick();
                await tick();

                const playBtn = container.querySelector(".inline-preview-play");
                const slider = container.querySelector(".inline-preview-scrub");
                playBtn.click(); // play
                raf.tickOnce(1000); // primes lastTick
                raf.tickOnce(3000); // dt=2.0, position advances ~2.0
                const pausedAt = Number(slider.value);
                expect(pausedAt).toBeCloseTo(2.0, 1);
                playBtn.click(); // pause
                // Slider position must persist (it's the operator's
                // last-known location).
                expect(Number(slider.value)).toBeCloseTo(pausedAt, 2);
                // Resume — first tick after pause re-primes lastTick;
                // no time-travel forward.
                playBtn.click(); // play
                raf.tickOnce(4000); // re-prime: position unchanged
                expect(Number(slider.value)).toBeCloseTo(pausedAt, 2);
                // Next tick advances from where pause left off, NOT from 0.
                raf.tickOnce(5000); // dt=1.0
                expect(Number(slider.value)).toBeCloseTo(pausedAt + 1.0, 1);
            } finally {
                container.remove();
            }
        } finally {
            raf.reset();
        }
    });

    it("stop() also pauses playback (aria flips + raf cancelled)", async () => {
        stubStageRect();
        const raf = installRafMock();
        try {
            const container = document.createElement("div");
            document.body.appendChild(container);
            try {
                const handle = mountInlinePreview(container, {
                    width: 128, height: 96, outputMode: "hdmi",
                    fetchPlaylist: async () => makePlaylist([
                        { id: "a", duration_ms: 5000,
                          text_layers: [{ text: "X", motion: "shake", motion_intensity: 70, motion_phase: 0, box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }] },
                    ]),
                });
                await tick();
                await tick();
                const playBtn = container.querySelector(".inline-preview-play");
                playBtn.click(); // play
                expect(playBtn.getAttribute("aria-label")).toBe("pause");
                handle.stop();
                // setPlaying(false) inside stop() flips aria + cancels raf.
                expect(playBtn.getAttribute("aria-label")).toBe("play");
                // Any pending raf is cancelled — no further tick runs.
                const fired = raf.tickOnce(1000);
                expect(fired).toBe(false);
            } finally {
                container.remove();
            }
        } finally {
            raf.reset();
        }
    });

    it("play / pause / play button sequence schedules exactly one raf per play (no double-schedule on toggle)", async () => {
        // Belt-and-suspenders against a regression where the play
        // button's click handler might call setPlaying twice or where
        // pause leaves a dangling raf that the next play wouldn't
        // supersede -- both would manifest as +2 raf entries per
        // play-click and a leaked tick loop.
        stubStageRect();
        const raf = installRafMock();
        try {
            const container = document.createElement("div");
            document.body.appendChild(container);
            try {
                mountInlinePreview(container, {
                    width: 128, height: 96, outputMode: "hdmi",
                    fetchPlaylist: async () => makePlaylist([
                        { id: "a", duration_ms: 5000,
                          text_layers: [{ text: "X", motion: "shake", motion_intensity: 70, motion_phase: 0, box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }] },
                    ]),
                });
                await tick();
                await tick();
                const playBtn = container.querySelector(".inline-preview-play");
                const before = raf.rafCallbacks.length;
                playBtn.click(); // play
                const afterPlay = raf.rafCallbacks.length;
                expect(afterPlay - before).toBe(1);
                playBtn.click(); // pause: no new raf
                expect(raf.rafCallbacks.length - afterPlay).toBe(0);
                playBtn.click(); // play again
                expect(raf.rafCallbacks.length - afterPlay).toBe(1);
            } finally {
                container.remove();
            }
        } finally {
            raf.reset();
        }
    });

    it("slider input updates position even during playback (slider wins)", async () => {
        stubStageRect();
        const raf = installRafMock();
        try {
            const container = document.createElement("div");
            document.body.appendChild(container);
            try {
                mountInlinePreview(container, {
                    width: 128, height: 96, outputMode: "hdmi",
                    fetchPlaylist: async () => makePlaylist([
                        { id: "a", duration_ms: 10000,
                          text_layers: [{ text: "X", motion: "shake", motion_intensity: 70, motion_phase: 0, box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }] },
                    ]),
                });
                await tick();
                await tick();
                const playBtn = container.querySelector(".inline-preview-play");
                const slider = container.querySelector(".inline-preview-scrub");
                playBtn.click();
                raf.tickOnce(1000); // prime
                raf.tickOnce(2000); // position ~1.0
                expect(Number(slider.value)).toBeCloseTo(1.0, 1);
                // Operator drags scrubber while playback was running.
                slider.value = "5.5";
                slider.dispatchEvent(new Event("input"));
                // Slider's input handler MUST take precedence over
                // playback's accumulated position.
                expect(Number(slider.value)).toBeCloseTo(5.5, 2);
                // And drawCanvas's elapsed_s reflects the new position
                // (slot starts at 0; slot's elapsed = position - startSec).
                const lastCall = drawCanvas.mock.calls[drawCanvas.mock.calls.length - 1];
                expect(lastCall[2].elapsed_s).toBeCloseTo(5.5, 1);
            } finally {
                container.remove();
            }
        } finally {
            raf.reset();
        }
    });

    it("scrubbing across a slide boundary switches the active slot", async () => {
        stubStageRect();
        try {
            drawCanvas.mockClear();
            const container = document.createElement("div");
            document.body.appendChild(container);
            try {
                mountInlinePreview(container, {
                    width: 128, height: 96, outputMode: "hdmi",
                    fetchPlaylist: async () => makePlaylist([
                        { id: "alpha", duration_ms: 5000,
                          text_layers: [{ text: "ALPHA", motion: "shake", motion_intensity: 70, motion_phase: 0, box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }] },
                        { id: "beta", duration_ms: 5000,
                          text_layers: [{ text: "BETA", motion: "shake", motion_intensity: 70, motion_phase: 0, box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }] },
                    ]),
                });
                await tick();
                await tick();
                const slider = container.querySelector(".inline-preview-scrub");

                // Scrub to t=1.0 — slot 0 (alpha) is still active.
                // drawCanvas signature: (sampler, state, opts).
                // stateFromItem renames text_layers → layers
                // (state-from-item.js:30), so we read state.layers[].text.
                slider.value = "1.0";
                slider.dispatchEvent(new Event("input"));
                let lastCall = drawCanvas.mock.calls[drawCanvas.mock.calls.length - 1];
                expect(lastCall[1].layers[0].text).toBe("ALPHA");

                // Scrub to t=7.0 — crosses into slot 1 (beta).
                slider.value = "7.0";
                slider.dispatchEvent(new Event("input"));
                lastCall = drawCanvas.mock.calls[drawCanvas.mock.calls.length - 1];
                expect(lastCall[1].layers[0].text).toBe("BETA");
                // And elapsed_s for beta starts from 0 (position 7 - startSec 5 = 2).
                expect(lastCall[2].elapsed_s).toBeCloseTo(2.0, 1);
            } finally {
                container.remove();
            }
        } finally {
            // No raf mock to clean — scrub tests don't install it.
        }
    });

    it("slider input with empty timeline is a no-op (no throw)", async () => {
        const container = document.createElement("div");
        mountInlinePreview(container, {
            width: 128, height: 96, outputMode: "hdmi",
            fetchPlaylist: async () => ({ items: [] }),
        });
        await tick();
        const slider = container.querySelector(".inline-preview-scrub");
        slider.value = "5";
        expect(() => slider.dispatchEvent(new Event("input"))).not.toThrow();
        // Idle banner stays visible — scrub on an empty playlist
        // doesn't accidentally reveal it.
        expect(container.querySelector(".inline-preview-idle").hidden).toBe(false);
    });

    it("refresh with a shorter playlist resets position when it overflows the new total", async () => {
        stubStageRect();
        let useShort = false;
        const container = document.createElement("div");
        document.body.appendChild(container);
        try {
            const handle = mountInlinePreview(container, {
                width: 128, height: 96, outputMode: "hdmi",
                fetchPlaylist: async () =>
                    useShort
                        ? makePlaylist([{ id: "a", duration_ms: 3000,
                            text_layers: [{ text: "A", motion: "shake", motion_intensity: 70, motion_phase: 0, box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }] }])
                        : makePlaylist([
                            { id: "a", duration_ms: 5000,
                              text_layers: [{ text: "A", motion: "shake", motion_intensity: 70, motion_phase: 0, box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }] },
                            { id: "b", duration_ms: 3000,
                              text_layers: [{ text: "B", motion: "shake", motion_intensity: 70, motion_phase: 0, box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }] },
                          ]),
            });
            await tick();
            await tick();
            const slider = container.querySelector(".inline-preview-scrub");
            // Initial total: 5+3=8s. Scrub to 6.0 — past the new short
            // total (3s). After refresh, the internal position must
            // reset to 0 so the playhead is somewhere coherent (else
            // findActiveIndex would land at the LAST slot for any
            // over-range position, showing the wrong slide).
            slider.value = "6.0";
            slider.dispatchEvent(new Event("input"));
            expect(Number(slider.value)).toBeCloseTo(6.0, 1);

            // Verify the reset via drawCanvas's elapsed_s argument.
            // refresh() ends with renderOnce(); renderOnce passes
            // elapsed_s = (position - slot.startSec) to drawCanvas.
            // - If position was correctly reset to 0: elapsed_s ≈ 0.
            // - If position was NOT reset (regression): renderOnce
            //   would call drawSlot(timeline[last]) with elapsed_s ≈ 6.
            // slider.value itself is NOT auto-updated by refresh (the
            // tick loop is what writes it back during playback), so the
            // drawCanvas argument is the cleanest external observable.
            drawCanvas.mockClear();
            useShort = true;
            await handle.refresh();
            expect(Number(slider.max)).toBeCloseTo(3.0, 1);
            const lastCall = drawCanvas.mock.calls[drawCanvas.mock.calls.length - 1];
            // After refresh, position=0, only one slot (startSec=0).
            // elapsed_s ≈ 0 confirms the reset fired.
            expect(lastCall[2].elapsed_s).toBeCloseTo(0, 1);
        } finally {
            container.remove();
        }
    });
});
