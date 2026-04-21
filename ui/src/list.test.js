// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountList } from "./list.js";

afterEach(() => {
    vi.restoreAllMocks();
});

function waitForTick() {
    // Let one microtask flush: fetchItems resolves, list re-renders.
    return new Promise((r) => setTimeout(r, 0));
}

describe("mountList", () => {
    it("renders one <li> per item with name and action buttons", async () => {
        const container = document.createElement("div");
        const items = [
            { id: "a", name: "Open", text: "OPEN" },
            { id: "b", name: "Closed", text: "CLOSED" },
        ];
        mountList(container, {
            fetchItems: async () => items,
            onPlay: vi.fn(),
            onDelete: vi.fn(),
        });
        await waitForTick();

        const lis = container.querySelectorAll(".slide");
        expect(lis).toHaveLength(2);
        expect(lis[0].querySelector(".slide-name").textContent).toBe("Open");
        expect(lis[1].querySelector(".slide-name").textContent).toBe("Closed");
        expect(lis[0].querySelectorAll("button")).toHaveLength(2); // Play + Delete
    });

    it("shows the empty-state message when there are no items", async () => {
        const container = document.createElement("div");
        mountList(container, {
            fetchItems: async () => [],
            onPlay: vi.fn(),
            onDelete: vi.fn(),
        });
        await waitForTick();

        const status = container.querySelector(".list-status").textContent;
        expect(status).toContain("No slides yet");
    });

    it("shows an error message when fetchItems rejects", async () => {
        const container = document.createElement("div");
        mountList(container, {
            fetchItems: async () => {
                throw new Error("boom");
            },
            onPlay: vi.fn(),
            onDelete: vi.fn(),
        });
        await waitForTick();

        const status = container.querySelector(".list-status").textContent;
        expect(status).toContain("boom");
    });

    it("invokes onPlay(id) when Play is clicked", async () => {
        const container = document.createElement("div");
        const onPlay = vi.fn().mockResolvedValue(undefined);
        mountList(container, {
            fetchItems: async () => [{ id: "abc", name: "x", text: "x" }],
            onPlay,
            onDelete: vi.fn(),
        });
        await waitForTick();

        const playBtn = container.querySelector(".slide button");
        playBtn.click();
        await waitForTick();

        expect(onPlay).toHaveBeenCalledWith("abc");
    });

    it("invokes onDelete(id) and refreshes the list when Delete is clicked", async () => {
        const container = document.createElement("div");
        const items = [
            { id: "abc", name: "x", text: "x" },
            { id: "def", name: "y", text: "y" },
        ];
        const onDelete = vi.fn(async (id) => {
            const idx = items.findIndex((item) => item.id === id);
            items.splice(idx, 1);
        });
        mountList(container, {
            fetchItems: async () => [...items],
            onPlay: vi.fn(),
            onDelete,
        });
        await waitForTick();

        // Click Delete on first item.
        const deleteBtn = container.querySelectorAll(".slide")[0].querySelectorAll("button")[1];
        deleteBtn.click();
        await waitForTick();
        await waitForTick(); // one for onDelete, one for the post-refresh

        expect(onDelete).toHaveBeenCalledWith("abc");
        // After refresh, only the second item remains.
        const remaining = container.querySelectorAll(".slide");
        expect(remaining).toHaveLength(1);
        expect(remaining[0].dataset.id).toBe("def");
    });

    it("returns a refresh function the caller can trigger", async () => {
        const container = document.createElement("div");
        const fetchItems = vi.fn().mockResolvedValue([]);
        const { refresh } = mountList(container, {
            fetchItems,
            onPlay: vi.fn(),
            onDelete: vi.fn(),
        });
        await waitForTick();
        expect(fetchItems).toHaveBeenCalledTimes(1);

        await refresh();
        expect(fetchItems).toHaveBeenCalledTimes(2);
    });
});
