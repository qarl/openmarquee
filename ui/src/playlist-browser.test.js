// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
    mountPlaylistBrowser,
    nextPlaylistName,
} from "./playlist-browser.js";

const DEFAULT_PLAYLIST_ID = "00000000-0000-4000-8000-000000000001";
const LUNCH_ID = "00000000-0000-4000-8000-000000000010";
const EVENING_ID = "00000000-0000-4000-8000-000000000011";

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
        expect(
            nextPlaylistName(["default", "Playlist 1", "Playlist 2"]),
        ).toBe("playlist-3");
    });
});

describe("mountPlaylistBrowser", () => {
    // v4 collection: list of {id, name, items}.
    const COLLECTION = {
        playlists: [
            { id: DEFAULT_PLAYLIST_ID, name: "default", items: [{ item_id: "a" }] },
            { id: LUNCH_ID, name: "lunch", items: [{ item_id: "a" }, { item_id: "b" }] },
            { id: EVENING_ID, name: "evening", items: [] },
        ],
    };

    it("puts 'default' first (by id), then alphabetical by name, with item counts", async () => {
        const container = document.createElement("div");
        mountPlaylistBrowser(container, {
            fetchPlaylists: async () => COLLECTION,
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        const tiles = container.querySelectorAll(
            ".playlist-browser-tile[data-id]",
        );
        expect(Array.from(tiles).map((t) => t.dataset.id)).toEqual([
            DEFAULT_PLAYLIST_ID,
            EVENING_ID,
            LUNCH_ID,
        ]);
        const defaultTile = container.querySelector(
            `[data-id="${DEFAULT_PLAYLIST_ID}"]`,
        );
        expect(defaultTile.textContent).toMatch(/1 slide/);
        const lunchTile = container.querySelector(`[data-id="${LUNCH_ID}"]`);
        expect(lunchTile.textContent).toMatch(/2 slides/);
        // Display name is shown.
        expect(lunchTile.textContent).toMatch(/lunch/);
    });

    it("uses firstItem.updated_at as the thumb cachebust query (qarl ask 1 followup)", async () => {
        // Same cachebust contract as slide-browser/playlist-track —
        // when the rerender side-effect bumps a slide's envelope
        // updated_at, the playlist-browser tile thumb URL must include
        // the new stamp so the browser HTTP cache invalidates. Was
        // pinned to created_at only (737216b missed this surface);
        // QA caught it 2026-04-30 after live-firing rotation flips.
        const ITEM = {
            id: "first",
            name: "First slide",
            type: "text_slide",
            created_at: "2026-04-21T10:00:00Z",
            updated_at: "2026-04-30T13:59:23Z",
        };
        const container = document.createElement("div");
        mountPlaylistBrowser(container, {
            fetchPlaylists: async () => ({
                playlists: [
                    { id: DEFAULT_PLAYLIST_ID, name: "default", items: [{ item_id: "first" }] },
                ],
            }),
            fetchItems: async () => [ITEM],
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        const img = container.querySelector(".playlist-browser-tile-thumb");
        expect(img.getAttribute("src")).toContain(
            encodeURIComponent("2026-04-30T13:59:23Z"),
        );
    });

    it("fires onSelect with the playlist id on tile click", async () => {
        const onSelect = vi.fn();
        const container = document.createElement("div");
        mountPlaylistBrowser(container, {
            fetchPlaylists: async () => COLLECTION,
            onSelect,
            onCreate: vi.fn(),
        });
        await tick();
        container
            .querySelector(`[data-id="${LUNCH_ID}"] button.playlist-browser-tile-action`)
            .click();
        expect(onSelect).toHaveBeenCalledWith(LUNCH_ID);
    });

    it("fires onDelete with id + display name on × click", async () => {
        const onDelete = vi.fn();
        const container = document.createElement("div");
        mountPlaylistBrowser(container, {
            fetchPlaylists: async () => COLLECTION,
            onSelect: vi.fn(),
            onCreate: vi.fn(),
            onDelete,
        });
        await tick();
        container
            .querySelector(`[data-id="${LUNCH_ID}"] .playlist-browser-tile-delete`)
            .click();
        expect(onDelete).toHaveBeenCalledWith(LUNCH_ID, "lunch");
    });

    it("highlight() marks exactly one tile by id", async () => {
        const container = document.createElement("div");
        const handle = mountPlaylistBrowser(container, {
            fetchPlaylists: async () => COLLECTION,
            onSelect: vi.fn(),
            onCreate: vi.fn(),
        });
        await tick();
        handle.highlight(LUNCH_ID);
        const selected = container.querySelectorAll(
            ".playlist-browser-tile--selected",
        );
        expect(selected).toHaveLength(1);
        expect(selected[0].dataset.id).toBe(LUNCH_ID);
    });
});
