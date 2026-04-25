// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
    mountPlaylistBrowser,
    nextPlaylistName,
} from "./playlist-browser.js";

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

afterEach(() => {
    vi.restoreAllMocks();
});

describe("nextPlaylistName", () => {
    it("returns 'playlist-1' when none exist (or only default)", () => {
        expect(nextPlaylistName([])).toBe("playlist-1");
        expect(nextPlaylistName(["default"])).toBe("playlist-1");
    });
    it("fills gaps in the playlist-N series", () => {
        expect(
            nextPlaylistName(["default", "playlist-1", "playlist-3"]),
        ).toBe("playlist-2");
    });
    it("jumps past the max when no gaps", () => {
        expect(
            nextPlaylistName(["playlist-1", "playlist-2", "playlist-3"]),
        ).toBe("playlist-4");
    });
    it("treats legacy 'Playlist N' names (caps + space) as part of the series", () => {
        // Pre-fix devices have playlists named "Playlist 1" etc. — the
        // numbering should continue from those so a new playlist on the
        // same device gets a non-colliding number.
        expect(
            nextPlaylistName(["default", "Playlist 1", "Playlist 2"]),
        ).toBe("playlist-3");
    });
});

describe("mountPlaylistBrowser", () => {
    const COLLECTION = {
        playlists: {
            default: { items: [{ item_id: "a" }] },
            lunch: { items: [{ item_id: "a" }, { item_id: "b" }] },
            evening: { items: [] },
        },
    };

    it("puts 'default' first, then alphabetical, with item counts", async () => {
        const container = document.createElement("div");
        mountPlaylistBrowser(container, {
            fetchPlaylists: async () => COLLECTION,
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        const tiles = container.querySelectorAll(
            ".playlist-browser-tile[data-name]",
        );
        expect(Array.from(tiles).map((t) => t.dataset.name)).toEqual([
            "default",
            "evening",
            "lunch",
        ]);
        const defaultTile = container.querySelector(
            '[data-name="default"]',
        );
        expect(defaultTile.textContent).toMatch(/1 slide/);
        const lunchTile = container.querySelector('[data-name="lunch"]');
        expect(lunchTile.textContent).toMatch(/2 slides/);
    });

    it("fires onSelect with the playlist name on tile click", async () => {
        const onSelect = vi.fn();
        const container = document.createElement("div");
        mountPlaylistBrowser(container, {
            fetchPlaylists: async () => COLLECTION,
            onSelect,
            onCreate: vi.fn(),
        });
        await tick();
        container
            .querySelector('[data-name="lunch"] button')
            .click();
        expect(onSelect).toHaveBeenCalledWith("lunch");
    });

    // The inline +New tile was removed when the playlist page-head gained
    // "+ New playlist"; onCreate stays in the public API for the page-head
    // button to invoke.

    it("highlight() marks exactly one tile", async () => {
        const container = document.createElement("div");
        const handle = mountPlaylistBrowser(container, {
            fetchPlaylists: async () => COLLECTION,
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        handle.highlight("lunch");
        const selected = container.querySelectorAll(
            ".playlist-browser-tile--selected",
        );
        expect(selected).toHaveLength(1);
        expect(selected[0].dataset.name).toBe("lunch");
    });
});
