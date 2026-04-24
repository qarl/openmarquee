// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mountFlock } from "./flock.js";

beforeEach(() => {
    // jsdom lacks HTMLDialogElement.showModal() in older versions.
    if (!HTMLDialogElement.prototype.showModal) {
        HTMLDialogElement.prototype.showModal = function () {
            this.setAttribute("open", "");
        };
        HTMLDialogElement.prototype.close = function () {
            this.removeAttribute("open");
        };
    }
    vi.stubGlobal("confirm", vi.fn(() => true));
});

afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

const PEER = (over = {}) => ({
    id: "11111111-1111-1111-1111-111111111111",
    address: "lobby.ts.net",
    name: null,
    sync: false,
    added_at: "2026-04-24T12:00:00+00:00",
    last_seen_at: null,
    ...over,
});

describe("mountFlock", () => {
    it("renders one tile per peer + a + New tile", async () => {
        const container = document.createElement("div");
        mountFlock(container, {
            fetchFlock: async () => ({
                peers: [PEER(), PEER({ id: "22", address: "b.ts.net" })],
            }),
            onAdd: vi.fn(),
            onUpdate: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();
        const peerTiles = container.querySelectorAll(
            ".flock-tile:not(.flock-tile-new)",
        );
        expect(peerTiles).toHaveLength(2);
        expect(container.querySelector(".flock-tile-new")).toBeTruthy();
    });

    it("shows an empty-state hint when no peers exist", async () => {
        const container = document.createElement("div");
        mountFlock(container, {
            fetchFlock: async () => ({ peers: [] }),
            onAdd: vi.fn(),
            onUpdate: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();
        expect(container.querySelector(".flock-status").textContent).toMatch(
            /No peers yet/i,
        );
    });

    it("opens the modal on + New and submits via onAdd", async () => {
        const container = document.createElement("div");
        const onAdd = vi.fn(async () => PEER());
        mountFlock(container, {
            fetchFlock: async () => ({ peers: [] }),
            onAdd,
            onUpdate: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();

        container.querySelector(".flock-tile-new").click();
        const modal = container.querySelector(".flock-modal");
        expect(modal.hasAttribute("open")).toBe(true);

        modal.querySelector(".flock-address").value = "new.ts.net";
        modal
            .querySelector(".flock-modal-form")
            .dispatchEvent(new Event("submit", { cancelable: true }));
        await tick();
        expect(onAdd).toHaveBeenCalledWith("new.ts.net");
    });

    it("surfaces add errors inline instead of closing the modal", async () => {
        const container = document.createElement("div");
        const onAdd = vi.fn(async () => {
            throw new Error("Add peer failed (409): already in flock");
        });
        mountFlock(container, {
            fetchFlock: async () => ({ peers: [] }),
            onAdd,
            onUpdate: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();
        container.querySelector(".flock-tile-new").click();
        const modal = container.querySelector(".flock-modal");
        modal.querySelector(".flock-address").value = "lobby.ts.net";
        modal
            .querySelector(".flock-modal-form")
            .dispatchEvent(new Event("submit", { cancelable: true }));
        await tick();
        expect(modal.hasAttribute("open")).toBe(true);
        expect(modal.querySelector(".flock-modal-error").textContent).toMatch(
            /already in flock/i,
        );
    });

    it("toggling the sync checkbox calls onUpdate", async () => {
        const container = document.createElement("div");
        const onUpdate = vi.fn(async () => PEER({ sync: true }));
        mountFlock(container, {
            fetchFlock: async () => ({ peers: [PEER()] }),
            onAdd: vi.fn(),
            onUpdate,
            onDelete: vi.fn(),
        });
        await tick();
        const checkbox = container.querySelector(".flock-tile-sync-input");
        checkbox.checked = true;
        checkbox.dispatchEvent(new Event("change", { bubbles: true }));
        await tick();
        expect(onUpdate).toHaveBeenCalledWith(
            "11111111-1111-1111-1111-111111111111",
            { sync: true },
        );
    });

    it("clicking × calls onDelete after confirm", async () => {
        const container = document.createElement("div");
        const onDelete = vi.fn(async () => {});
        mountFlock(container, {
            fetchFlock: async () => ({ peers: [PEER()] }),
            onAdd: vi.fn(),
            onUpdate: vi.fn(),
            onDelete,
        });
        await tick();
        container.querySelector(".flock-tile-delete").click();
        await tick();
        expect(onDelete).toHaveBeenCalledWith(
            "11111111-1111-1111-1111-111111111111",
        );
    });

    it("sync tile gets a synced state class", async () => {
        const container = document.createElement("div");
        mountFlock(container, {
            fetchFlock: async () => ({ peers: [PEER({ sync: true })] }),
            onAdd: vi.fn(),
            onUpdate: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();
        expect(
            container
                .querySelector(".flock-tile:not(.flock-tile-new)")
                .classList.contains("flock-tile-synced"),
        ).toBe(true);
    });

    it("Open there link points at http://address/", async () => {
        const container = document.createElement("div");
        mountFlock(container, {
            fetchFlock: async () => ({ peers: [PEER({ address: "127.0.0.1:9887" })] }),
            onAdd: vi.fn(),
            onUpdate: vi.fn(),
            onDelete: vi.fn(),
        });
        await tick();
        const link = container.querySelector(".flock-tile-open");
        expect(link.getAttribute("href")).toBe("http://127.0.0.1:9887/");
        expect(link.getAttribute("target")).toBe("_blank");
    });
});
