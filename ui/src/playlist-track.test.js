// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountPlaylistTrack } from "./playlist-track.js";

afterEach(() => {
    vi.restoreAllMocks();
});

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

const ITEMS = [
    { id: "a", name: "Welcome", type: "text_slide", duration_ms: 5000 },
    { id: "b", name: "Logo", type: "image", duration_ms: 3000 },
    { id: "c", name: "Promo", type: "video", duration_ms: 10000 },
];

function fetchPlaylistsWith(defaultIds) {
    return async () => ({
        schema_version: 2,
        playlists: { default: { item_ids: defaultIds } },
    });
}

describe("mountPlaylistTrack", () => {
    it("renders track blocks for the default playlist in order", async () => {
        const container = document.createElement("div");
        mountPlaylistTrack(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: fetchPlaylistsWith(["b", "a"]),
            onReorder: vi.fn(),
        });
        await tick();

        const blocks = container.querySelectorAll(".track-block");
        expect(blocks).toHaveLength(2);
        expect(blocks[0].dataset.id).toBe("b");
        expect(blocks[1].dataset.id).toBe("a");
    });

    it("renders the pallet with every content item", async () => {
        const container = document.createElement("div");
        mountPlaylistTrack(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: fetchPlaylistsWith([]),
            onReorder: vi.fn(),
        });
        await tick();

        const tiles = container.querySelectorAll(".pallet-tile");
        expect(tiles).toHaveLength(3);
        const ids = Array.from(tiles).map((t) => t.dataset.id);
        expect(ids).toEqual(["a", "b", "c"]);
    });

    it("displays each track block's duration in seconds", async () => {
        const container = document.createElement("div");
        mountPlaylistTrack(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: fetchPlaylistsWith(["a", "b", "c"]),
            onReorder: vi.fn(),
        });
        await tick();

        const durations = Array.from(
            container.querySelectorAll(".track-block-duration"),
        ).map((el) => el.textContent);
        expect(durations).toEqual(["5s", "3s", "10s"]);
    });

    it("clicking × removes the block and PUTs the new order", async () => {
        const container = document.createElement("div");
        const onReorder = vi.fn().mockResolvedValue(undefined);
        mountPlaylistTrack(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: fetchPlaylistsWith(["a", "b", "c"]),
            onReorder,
        });
        await tick();

        // Remove the middle one.
        const middleRemove = container.querySelector(
            '.track-block[data-id="b"] .track-remove',
        );
        middleRemove.click();
        await tick();

        expect(onReorder).toHaveBeenCalledWith(["a", "c"]);
    });

    it("empty-state hint is surfaced on an empty playlist via data-empty-hint", async () => {
        const container = document.createElement("div");
        mountPlaylistTrack(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: fetchPlaylistsWith([]),
            onReorder: vi.fn(),
        });
        await tick();

        const ul = container.querySelector(".playlist-track-list");
        expect(ul.children.length).toBe(0);
        expect(ul.getAttribute("data-empty-hint")).toMatch(/pallet/i);
    });

    it("mounts the injected playback controls when playback hooks are passed", async () => {
        const container = document.createElement("div");
        const mountPlaybackControls = vi.fn();
        mountPlaylistTrack(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: fetchPlaylistsWith([]),
            onReorder: vi.fn(),
            playback: {
                fetchState: vi.fn(),
                onStart: vi.fn(),
                onStop: vi.fn(),
            },
            mountPlaybackControls,
        });
        await tick();

        expect(mountPlaybackControls).toHaveBeenCalledTimes(1);
        const slot = mountPlaybackControls.mock.calls[0][0];
        expect(slot.classList.contains("playlist-track-playback")).toBe(true);
    });

    it("skips stale ids (playlist references an item no longer in storage)", async () => {
        const container = document.createElement("div");
        mountPlaylistTrack(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: fetchPlaylistsWith(["a", "DELETED", "b"]),
            onReorder: vi.fn(),
        });
        await tick();

        const blocks = container.querySelectorAll(".track-block");
        expect(Array.from(blocks).map((b) => b.dataset.id)).toEqual(["a", "b"]);
    });
});
