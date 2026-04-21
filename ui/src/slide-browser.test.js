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
        // 1 new-tile + 2 text_slide items = 3 total.
        expect(tiles).toHaveLength(3);
        expect(tiles[0].classList.contains("slide-browser-tile--new")).toBe(true);
        // Most-recent first (Charlie 12:00 before Alpha 10:00).
        expect(tiles[1].dataset.id).toBe("c");
        expect(tiles[2].dataset.id).toBe("a");
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

    it("fires onCreate on '+ New' click", async () => {
        const onCreate = vi.fn();
        const container = document.createElement("div");
        mountSlideBrowser(container, {
            type: "text_slide",
            fetchItems: async () => ITEMS,
            onSelect: vi.fn(),
            onCreate,
        });
        await tick();
        container.querySelector(".slide-browser-tile--new button").click();
        expect(onCreate).toHaveBeenCalledTimes(1);
    });

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
        // Only the '+ New' tile remains.
        expect(
            container.querySelectorAll(".slide-browser-tile"),
        ).toHaveLength(1);
    });
});
