// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mountPlaylistsManager } from "./playlists.js";

beforeEach(() => {
    // jsdom's window.confirm is undefined; mock it to always accept.
    vi.stubGlobal("confirm", vi.fn(() => true));
});

afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

const ITEMS = [
    { id: "aaa", name: "Open", text: "OPEN" },
    { id: "bbb", name: "Closed", text: "CLOSED" },
    { id: "ccc", name: "Lunch Special", text: "" },
];

describe("mountPlaylistsManager", () => {
    it("excludes the default playlist from the manager", async () => {
        const container = document.createElement("div");
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: ["aaa"] },
                    lunch: { item_ids: ["ccc"] },
                },
            }),
            onSave: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();

        const cards = container.querySelectorAll(".playlist-card");
        expect(cards).toHaveLength(1);
        expect(cards[0].dataset.name).toBe("lunch");
    });

    it("shows an empty-state hint when no named playlists exist", async () => {
        const container = document.createElement("div");
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({ playlists: { default: { item_ids: [] } } }),
            onSave: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();
        expect(container.querySelector(".playlists-status").textContent).toMatch(
            /No named playlists yet/,
        );
    });

    it("renders each member as a draggable <li> with a × remove button", async () => {
        const container = document.createElement("div");
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: [] },
                    lunch: { item_ids: ["aaa", "ccc"] },
                },
            }),
            onSave: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();

        const card = container.querySelector('.playlist-card[data-name="lunch"]');
        const items = card.querySelectorAll(".playlist-item");
        expect(items).toHaveLength(2);
        expect(items[0].dataset.id).toBe("aaa");
        expect(items[1].dataset.id).toBe("ccc");
        expect(items[0].querySelector(".playlist-item-remove")).not.toBeNull();
    });

    it("Add dropdown lists only items NOT already in the playlist", async () => {
        const container = document.createElement("div");
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: [] },
                    lunch: { item_ids: ["aaa"] },
                },
            }),
            onSave: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();

        const card = container.querySelector('.playlist-card[data-name="lunch"]');
        const options = Array.from(
            card.querySelectorAll(".playlist-add-select option"),
        )
            .map((o) => o.value)
            .filter(Boolean);
        // aaa is already in; bbb and ccc should be the only candidates.
        expect(options).toEqual(["bbb", "ccc"]);
    });

    it("selecting from Add dropdown saves the playlist with the item appended", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: [] },
                    lunch: { item_ids: ["aaa"] },
                },
            }),
            onSave,
            onDelete: vi.fn(),
        });
        await tick();

        const card = container.querySelector('.playlist-card[data-name="lunch"]');
        const select = card.querySelector(".playlist-add-select");
        select.value = "ccc";
        select.dispatchEvent(new Event("change"));
        await tick();

        expect(onSave).toHaveBeenCalledWith("lunch", ["aaa", "ccc"]);
    });

    it("disables the Add dropdown when every item is already in the playlist", async () => {
        const container = document.createElement("div");
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: [] },
                    everything: { item_ids: ["aaa", "bbb", "ccc"] },
                },
            }),
            onSave: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();

        const select = container.querySelector(
            '.playlist-card[data-name="everything"] .playlist-add-select',
        );
        expect(select.disabled).toBe(true);
    });

    it("× button label names the item and the playlist for screen-readers", async () => {
        const container = document.createElement("div");
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: [] },
                    lunch: { item_ids: ["aaa"] },
                },
            }),
            onSave: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();

        const btn = container.querySelector(
            '.playlist-card[data-name="lunch"] .playlist-item[data-id="aaa"] .playlist-item-remove',
        );
        expect(btn.getAttribute("aria-label")).toBe("Remove Open from lunch");
    });

    it("clicking × on a member saves the playlist with that item removed", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: [] },
                    lunch: { item_ids: ["aaa", "bbb", "ccc"] },
                },
            }),
            onSave,
            onDelete: vi.fn(),
        });
        await tick();

        const card = container.querySelector('.playlist-card[data-name="lunch"]');
        const bbbRemove = card
            .querySelector('.playlist-item[data-id="bbb"] .playlist-item-remove');
        bbbRemove.click();
        await tick();

        expect(onSave).toHaveBeenCalledWith("lunch", ["aaa", "ccc"]);
    });

    it("renders an empty <ul> for playlists with no items (so SortableJS has a drop target)", async () => {
        const container = document.createElement("div");
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: [] },
                    weekend: { item_ids: [] },
                },
            }),
            onSave: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();

        const card = container.querySelector('.playlist-card[data-name="weekend"]');
        const ul = card.querySelector(".playlist-items");
        expect(ul).not.toBeNull();
        expect(ul.querySelectorAll(".playlist-item")).toHaveLength(0);
        // CSS :empty::before uses the attribute, so it must round-trip.
        expect(ul.getAttribute("data-empty-hint")).toMatch(/drag/i);
    });

    it("Delete calls onDelete(name) after confirm and refreshes the list", async () => {
        const container = document.createElement("div");
        const onDelete = vi.fn().mockResolvedValue(undefined);
        let callCount = 0;
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => {
                callCount++;
                if (callCount >= 2) return { playlists: { default: { item_ids: [] } } };
                return {
                    playlists: {
                        default: { item_ids: [] },
                        lunch: { item_ids: [] },
                    },
                };
            },
            onSave: vi.fn(),
            onDelete,
        });
        await tick();

        container.querySelector('.playlist-card[data-name="lunch"] .playlist-delete').click();
        await tick();
        await tick();

        expect(onDelete).toHaveBeenCalledWith("lunch");
        expect(container.querySelectorAll(".playlist-card")).toHaveLength(0);
    });

    it("Create calls onSave with an empty list and clears the input", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({ playlists: { default: { item_ids: [] } } }),
            onSave,
            onDelete: vi.fn(),
        });
        await tick();

        const input = container.querySelector(".playlists-create-name");
        input.value = "weekend";
        container.querySelector(".playlists-create").dispatchEvent(new Event("submit"));
        await tick();

        expect(onSave).toHaveBeenCalledWith("weekend", []);
        expect(input.value).toBe("");
    });

    it("Create refuses the reserved name 'default'", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn();
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({ playlists: { default: { item_ids: [] } } }),
            onSave,
            onDelete: vi.fn(),
        });
        await tick();

        const input = container.querySelector(".playlists-create-name");
        input.value = "default";
        container.querySelector(".playlists-create").dispatchEvent(new Event("submit"));
        await tick();

        expect(onSave).not.toHaveBeenCalled();
        expect(container.querySelector(".playlists-status").textContent).toMatch(
            /reserved/i,
        );
    });

    it("escapes playlist names + item names so injected markup doesn't render", async () => {
        const container = document.createElement("div");
        mountPlaylistsManager(container, {
            fetchItems: async () => [
                { id: "x", name: '<img src=x onerror="alert(1)">' },
            ],
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: [] },
                    '<script>alert(1)</script>': { item_ids: ["x"] },
                },
            }),
            onSave: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();

        // No nested scripts / imgs — payloads rendered as text.
        expect(container.querySelector(".playlist-card script")).toBeNull();
        expect(container.querySelector(".playlist-item img")).toBeNull();
    });

    it("renames a label to '(missing)' when a playlist references a deleted item id", async () => {
        const container = document.createElement("div");
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS, // no "zzz" item exists
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: [] },
                    lunch: { item_ids: ["zzz"] },
                },
            }),
            onSave: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();

        const card = container.querySelector('.playlist-card[data-name="lunch"]');
        const label = card.querySelector('.playlist-item[data-id="zzz"] .playlist-item-label');
        expect(label.textContent).toBe("(missing)");
    });
});
