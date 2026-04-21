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

    it("renders a checkbox per content item, pre-checked for items already in the playlist", async () => {
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
        const checkboxes = card.querySelectorAll("input[type='checkbox']");
        expect(checkboxes).toHaveLength(3);
        const checkedIds = Array.from(checkboxes)
            .filter((cb) => cb.checked)
            .map((cb) => cb.value);
        expect(checkedIds).toEqual(["aaa", "ccc"]);
    });

    it("Save sends the currently-checked ids via onSave(name, ids)", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => ({
                playlists: {
                    default: { item_ids: [] },
                    lunch: { item_ids: [] },
                },
            }),
            onSave,
            onDelete: vi.fn(),
        });
        await tick();

        const card = container.querySelector('.playlist-card[data-name="lunch"]');
        // Check the first and third items.
        const checkboxes = card.querySelectorAll("input[type='checkbox']");
        checkboxes[0].checked = true;
        checkboxes[2].checked = true;
        card.querySelector(".playlist-save").click();
        await tick();

        expect(onSave).toHaveBeenCalledWith("lunch", ["aaa", "ccc"]);
    });

    it("Delete calls onDelete(name) after confirm and refreshes the list", async () => {
        const container = document.createElement("div");
        const onDelete = vi.fn().mockResolvedValue(undefined);
        const playlists = {
            default: { item_ids: [] },
            lunch: { item_ids: [] },
        };
        let callCount = 0;
        mountPlaylistsManager(container, {
            fetchItems: async () => ITEMS,
            fetchPlaylists: async () => {
                callCount++;
                // After delete, the second call sees lunch removed.
                if (callCount >= 2) return { playlists: { default: { item_ids: [] } } };
                return { playlists };
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
                    '<script>alert(1)</script>': { item_ids: [] },
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
});
