// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountSlideBrowser, nextAutoName } from "./slide-browser.js";

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

afterEach(() => {
    vi.restoreAllMocks();
});

describe("nextAutoName", () => {
    it("returns '... 1' when no matching names exist", () => {
        expect(nextAutoName([], "Text Slide")).toBe("Text Slide 1");
    });

    it("returns the smallest gap to recycle deleted numbers", () => {
        const items = [
            { name: "Text Slide 1" },
            { name: "Text Slide 3" },
            { name: "Other slide" },
        ];
        expect(nextAutoName(items, "Text Slide")).toBe("Text Slide 2");
    });

    it("jumps past the max when no gaps exist", () => {
        const items = [
            { name: "Image Slide 1" },
            { name: "Image Slide 2" },
            { name: "Image Slide 3" },
        ];
        expect(nextAutoName(items, "Image Slide")).toBe("Image Slide 4");
    });

    it("ignores names that don't match the prefix pattern", () => {
        const items = [
            { name: "Video Slide 2" },
            { name: "Custom Name" },
            { name: "Video Slide Foo" },
            { name: "Video Slide 10" },
        ];
        expect(nextAutoName(items, "Video Slide")).toBe("Video Slide 1");
    });
});

describe("mountSlideBrowser", () => {
    const ITEMS = [
        { id: "a", name: "Alpha", type: "text_slide", created_at: "2026-04-21T10:00:00Z" },
        { id: "b", name: "Beta", type: "image", created_at: "2026-04-21T11:00:00Z" },
        { id: "c", name: "Charlie", type: "text_slide", created_at: "2026-04-21T12:00:00Z" },
    ];

    it("renders a '+ New' tile plus one tile per matching item", async () => {
        const container = document.createElement("div");
        mountSlideBrowser(container, {
            type: "text_slide",
            fetchItems: async () => ITEMS,
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        const tiles = container.querySelectorAll(".slide-browser-tile");
        // 2 text_slide items — the inline +New tile is gone (the slides
        // shell page-head now owns the create affordance).
        expect(tiles).toHaveLength(2);
        // Chronological order: oldest first (Alpha 10:00 before Charlie 12:00),
        // so new slides land at the end (B19, 2026-05-05).
        expect(tiles[0].dataset.id).toBe("a");
        expect(tiles[1].dataset.id).toBe("c");
    });

    it("filters out items that don't match the requested type", async () => {
        const container = document.createElement("div");
        mountSlideBrowser(container, {
            type: "image",
            fetchItems: async () => ITEMS,
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        // Only Beta is an image.
        const dataTiles = Array.from(
            container.querySelectorAll(".slide-browser-tile[data-id]"),
        );
        expect(dataTiles.map((t) => t.dataset.id)).toEqual(["b"]);
    });

    it("fires onSelect with the item object on tile click", async () => {
        const onSelect = vi.fn();
        const container = document.createElement("div");
        mountSlideBrowser(container, {
            type: "text_slide",
            fetchItems: async () => ITEMS,
            onSelect,
            onCreate: vi.fn(),
        });
        await tick();
        container.querySelector('.slide-browser-tile[data-id="a"] button').click();
        expect(onSelect).toHaveBeenCalledTimes(1);
        expect(onSelect.mock.calls[0][0].id).toBe("a");
    });

    // The inline +New tile was removed when the slides shell page-head
    // gained "+ New text/image/video"; onCreate stays on the public API
    // for back-compat (the shell calls editor/uploader.reset directly).

    it("highlight() marks exactly one tile as selected", async () => {
        const container = document.createElement("div");
        const handle = mountSlideBrowser(container, {
            type: "text_slide",
            fetchItems: async () => ITEMS,
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        handle.highlight("c");
        const selected = container.querySelectorAll(
            ".slide-browser-tile--selected",
        );
        expect(selected).toHaveLength(1);
        expect(selected[0].dataset.id).toBe("c");
        handle.highlight(null);
        expect(
            container.querySelectorAll(".slide-browser-tile--selected"),
        ).toHaveLength(0);
    });

    it("uses the slide's updated_at as the asset cachebust query param (qarl ask 1 followup)", async () => {
        // Backend re-renders text slides on a settings dim flip and bumps
        // each slide's envelope updated_at. The tile thumb URL must
        // include that stamp so the browser HTTP cache invalidates and
        // refetches new bytes — without it, refreshVersion=0 on the
        // freshly-remounted browser collides with the pre-flip cache key
        // and the operator sees stale PNGs.
        const items = [
            {
                id: "tx",
                name: "Time",
                type: "text_slide",
                created_at: "2026-04-30T01:00:00Z",
                updated_at: "2026-04-30T05:30:00Z",
            },
        ];
        const container = document.createElement("div");
        mountSlideBrowser(container, {
            type: "text_slide",
            fetchItems: async () => items,
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        const img = container.querySelector(".slide-browser-tile-thumb");
        expect(img.getAttribute("src")).toContain(
            encodeURIComponent("2026-04-30T05:30:00Z"),
        );
    });

    it("falls back to created_at when updated_at is missing", async () => {
        const items = [
            {
                id: "tx",
                name: "Time",
                type: "text_slide",
                created_at: "2026-04-21T10:00:00Z",
            },
        ];
        const container = document.createElement("div");
        mountSlideBrowser(container, {
            type: "text_slide",
            fetchItems: async () => items,
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        const img = container.querySelector(".slide-browser-tile-thumb");
        expect(img.getAttribute("src")).toContain(
            encodeURIComponent("2026-04-21T10:00:00Z"),
        );
    });

    it("renders gracefully when fetchItems throws", async () => {
        const container = document.createElement("div");
        mountSlideBrowser(container, {
            type: "text_slide",
            fetchItems: async () => {
                throw new Error("boom");
            },
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        // No tiles render (no items, and the +New tile was removed).
        expect(
            container.querySelectorAll(".slide-browser-tile"),
        ).toHaveLength(0);
    });
});
