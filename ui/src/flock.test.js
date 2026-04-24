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
    function mount(container, { peers = [], settings = { sign_name: "SignAAA", flock_sync_enabled: true }, ...over } = {}) {
        mountFlock(container, {
            fetchFlock: async () => ({ peers }),
            fetchSettings: async () => settings,
            onAdd: vi.fn(),
            onUpdate: vi.fn(),
            onUpdateSelfSync: vi.fn(),
            onDelete: vi.fn(),
            ...over,
        });
    }

    it("renders self tile first, then one tile per peer + a + New tile", async () => {
        const container = document.createElement("div");
        mount(container, {
            peers: [PEER(), PEER({ id: "22", address: "b.ts.net" })],
        });
        await tick();
        const tiles = container.querySelectorAll(".flock-tile");
        // self + 2 peers + new
        expect(tiles).toHaveLength(4);
        expect(tiles[0].classList.contains("flock-tile-self")).toBe(true);
        expect(tiles[tiles.length - 1].classList.contains("flock-tile-new")).toBe(true);
    });

    it("self tile shows the sign_name from settings", async () => {
        const container = document.createElement("div");
        mount(container, { settings: { sign_name: "SignE6B" } });
        await tick();
        expect(
            container.querySelector(".flock-tile-self .flock-tile-name")
                .textContent,
        ).toBe("SignE6B");
    });

    it("shows an empty-state hint when no peer devices exist", async () => {
        const container = document.createElement("div");
        mount(container, { peers: [] });
        await tick();
        expect(container.querySelector(".flock-status").textContent).toMatch(
            /No peer devices yet/i,
        );
    });

    it("opens the modal on + New and submits via onAdd", async () => {
        const container = document.createElement("div");
        const onAdd = vi.fn(async () => PEER());
        mount(container, { peers: [], onAdd });
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
            throw new Error("already in flock");
        });
        mount(container, { peers: [], onAdd });
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
        mount(container, { peers: [PEER()], onUpdate });
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
        mount(container, { peers: [PEER()], onDelete });
        await tick();
        container.querySelector(".flock-tile-delete").click();
        await tick();
        expect(onDelete).toHaveBeenCalledWith(
            "11111111-1111-1111-1111-111111111111",
        );
    });

    it("sync tile gets a synced state class", async () => {
        const container = document.createElement("div");
        mount(container, { peers: [PEER({ sync: true })] });
        await tick();
        const peerTile = container.querySelector(
            ".flock-tile:not(.flock-tile-new):not(.flock-tile-self)",
        );
        expect(peerTile.classList.contains("flock-tile-synced")).toBe(true);
    });

    it("Go there link drops into the peer's Flock panel", async () => {
        const container = document.createElement("div");
        mount(container, { peers: [PEER({ address: "127.0.0.1:9887" })] });
        await tick();
        const link = container.querySelector(".flock-tile-open");
        expect(link.getAttribute("href")).toBe("http://127.0.0.1:9887/#/flock");
        expect(link.getAttribute("target")).toBe("_blank");
        expect(link.textContent.trim()).toMatch(/Go there/);
    });

    it("self tile reflects flock_sync_enabled and toggling calls onUpdateSelfSync", async () => {
        const container = document.createElement("div");
        const onUpdateSelfSync = vi.fn(async () => {});
        mount(container, {
            settings: { sign_name: "SignAAA", flock_sync_enabled: false },
            onUpdateSelfSync,
        });
        await tick();
        const selfTile = container.querySelector(".flock-tile-self");
        // Disabled state: no "synced" class, checkbox unchecked.
        expect(selfTile.classList.contains("flock-tile-synced")).toBe(false);
        const checkbox = selfTile.querySelector(".flock-tile-self-sync-input");
        expect(checkbox.checked).toBe(false);
        checkbox.checked = true;
        checkbox.dispatchEvent(new Event("change", { bubbles: true }));
        await tick();
        expect(onUpdateSelfSync).toHaveBeenCalledWith(true);
    });

    it("self tile is marked synced when flock_sync_enabled is true", async () => {
        const container = document.createElement("div");
        mount(container, {
            settings: { sign_name: "SignAAA", flock_sync_enabled: true },
        });
        await tick();
        expect(
            container
                .querySelector(".flock-tile-self")
                .classList.contains("flock-tile-synced"),
        ).toBe(true);
    });

    it("peer tile thumbnail fetches the peer's current-thumbnail endpoint", async () => {
        // Refresh switched from <img src=...> to fetch+blob so the demo's
        // mock-backend can intercept the request — the assertion now
        // tracks the fetch URL rather than the final blob: src on the img.
        const seen = [];
        const fetchSpy = vi.fn(async (url) => {
            seen.push(String(url));
            return new Response(new Blob([new Uint8Array(0)], { type: "image/png" }));
        });
        vi.stubGlobal("fetch", fetchSpy);
        try {
            const container = document.createElement("div");
            mount(container, { peers: [PEER({ address: "127.0.0.1:9887" })] });
            await tick();
            await tick();
            expect(
                seen.some((u) =>
                    /^http:\/\/127\.0\.0\.1:9887\/api\/playback\/current-thumbnail\?t=\d+/.test(u),
                ),
            ).toBe(true);
        } finally {
            vi.unstubAllGlobals();
        }
    });

    it("self tile thumbnail fetches same-origin (no http://host prefix)", async () => {
        const seen = [];
        const fetchSpy = vi.fn(async (url) => {
            seen.push(String(url));
            return new Response(new Blob([new Uint8Array(0)], { type: "image/png" }));
        });
        vi.stubGlobal("fetch", fetchSpy);
        try {
            const container = document.createElement("div");
            mount(container);
            await tick();
            await tick();
            // Same-origin path doesn't get a `http://host` prefix —
            // matches whatever the mock backend / real device serves.
            expect(
                seen.some((u) => /^\/api\/playback\/current-thumbnail\?t=\d+/.test(u)),
            ).toBe(true);
        } finally {
            vi.unstubAllGlobals();
        }
    });
});
