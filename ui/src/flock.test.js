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
    added_at: "2026-01-01T00:00:00+00:00",
    last_seen_at: null,
    model: null,
    mode: null,
    signal: null,
    uptime: null,
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

    it("renders the self card first, then one card per peer + a + New tile", async () => {
        const container = document.createElement("div");
        mount(container, {
            peers: [PEER(), PEER({ id: "22", address: "b.ts.net" })],
        });
        await tick();
        const cards = container.querySelectorAll(".om-flock-grid > .om-peer-card, .om-flock-grid > .flock-new-device");
        // self + 2 peers + new
        expect(cards).toHaveLength(4);
        expect(cards[0].classList.contains("this")).toBe(true);
        expect(cards[cards.length - 1].classList.contains("flock-new-device")).toBe(true);
    });

    it("self card shows the sign_name from settings", async () => {
        const container = document.createElement("div");
        mount(container, { settings: { sign_name: "SignE6B" } });
        await tick();
        expect(
            container.querySelector(".om-peer-card.this .om-peer-name").textContent,
        ).toBe("SignE6B");
    });

    it("self card carries a 'this device' pulse pill", async () => {
        const container = document.createElement("div");
        mount(container);
        await tick();
        const pill = container.querySelector(".om-peer-card.this .om-pill.live");
        expect(pill).toBeTruthy();
        expect(pill.textContent).toMatch(/this device/);
        expect(pill.querySelector(".om-pulse")).toBeTruthy();
    });

    it("eyebrow reports online count and total signs", async () => {
        const container = document.createElement("div");
        const recent = new Date(Date.now() - 5_000).toISOString();
        mount(container, {
            peers: [PEER({ last_seen_at: recent }), PEER({ id: "off", address: "x.ts.net" })],
        });
        await tick();
        // Self counts as online by virtue of serving the panel; one of
        // two peers is fresh, the other has no last_seen → offline.
        expect(container.querySelector(".flock-eyebrow").textContent).toMatch(
            /2 of 3 signs online/,
        );
    });

    it("shows an empty-state hint when no peer devices exist", async () => {
        const container = document.createElement("div");
        mount(container, { peers: [] });
        await tick();
        expect(container.querySelector(".flock-status").textContent).toMatch(
            /No peer devices yet/i,
        );
    });

    it("opens the modal on + New device and submits via onAdd", async () => {
        const container = document.createElement("div");
        const onAdd = vi.fn(async () => PEER());
        mount(container, { peers: [], onAdd });
        await tick();

        container.querySelector(".flock-new-device").click();
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
        container.querySelector(".flock-new-device").click();
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

    it("toggling the per-peer sync checkbox calls onUpdate", async () => {
        const container = document.createElement("div");
        const onUpdate = vi.fn(async () => PEER({ sync: true }));
        mount(container, { peers: [PEER()], onUpdate });
        await tick();
        const checkbox = container.querySelector(".flock-peer-sync-input");
        checkbox.checked = true;
        checkbox.dispatchEvent(new Event("change", { bubbles: true }));
        await tick();
        expect(onUpdate).toHaveBeenCalledWith(
            "11111111-1111-1111-1111-111111111111",
            { sync: true },
        );
    });

    it("Deflock button confirms then calls onDelete", async () => {
        const container = document.createElement("div");
        const onDelete = vi.fn(async () => {});
        mount(container, { peers: [PEER()], onDelete });
        await tick();
        container.querySelector(".flock-peer-deflock").click();
        await tick();
        expect(onDelete).toHaveBeenCalledWith(
            "11111111-1111-1111-1111-111111111111",
        );
    });

    it("Deflock confirm prompt names the peer ('Remove X from your flock?')", async () => {
        const container = document.createElement("div");
        const onDelete = vi.fn(async () => {});
        const confirmSpy = vi.fn(() => false);
        vi.stubGlobal("confirm", confirmSpy);
        mount(container, {
            peers: [PEER({ name: "SignA7F" })],
            onDelete,
        });
        await tick();
        container.querySelector(".flock-peer-deflock").click();
        await tick();
        expect(confirmSpy).toHaveBeenCalledTimes(1);
        expect(confirmSpy).toHaveBeenCalledWith(
            "Remove SignA7F from your flock?",
        );
        // Cancelled — onDelete NOT called.
        expect(onDelete).not.toHaveBeenCalled();
    });

    it("Edit button navigates the browser to the peer's tailnet URL", async () => {
        // Per qarl's locked decision: no in-app context swap. Edit is a
        // browser-native location.href navigate to the peer's UI.
        const container = document.createElement("div");
        // Spy via property descriptor — assigning location.href in
        // jsdom otherwise just rewrites the property. The peer is
        // online (recent last_seen) so the Edit button isn't disabled.
        const recent = new Date(Date.now() - 5_000).toISOString();
        mount(container, {
            peers: [PEER({ address: "127.0.0.1:9887", last_seen_at: recent })],
        });
        await tick();
        let navigatedTo = null;
        const orig = window.location;
        Object.defineProperty(window, "location", {
            configurable: true,
            value: {
                ...orig,
                set href(v) { navigatedTo = v; },
                get href() { return orig.href; },
            },
        });
        try {
            container.querySelector(".flock-peer-edit").click();
            await tick();
            expect(navigatedTo).toBe("http://127.0.0.1:9887/");
        } finally {
            Object.defineProperty(window, "location", {
                configurable: true,
                value: orig,
            });
        }
    });

    it("offline peer's Edit button is disabled and the card carries an offline pill", async () => {
        const container = document.createElement("div");
        // last_seen_at unset → offline.
        mount(container, { peers: [PEER()] });
        await tick();
        const card = container.querySelector(".om-peer-card[data-peer-id]");
        expect(card.classList.contains("offline")).toBe(true);
        expect(card.querySelector(".om-pill.bad")).toBeTruthy();
        expect(card.querySelector(".flock-peer-edit").disabled).toBe(true);
    });

    it("self card sync toggle reflects flock_sync_enabled and calls onUpdateSelfSync on flip", async () => {
        const container = document.createElement("div");
        const onUpdateSelfSync = vi.fn(async () => {});
        mount(container, {
            settings: { sign_name: "SignAAA", flock_sync_enabled: false },
            onUpdateSelfSync,
        });
        await tick();
        const selfCard = container.querySelector(".om-peer-card.this");
        const checkbox = selfCard.querySelector(".flock-self-sync-input");
        expect(checkbox.checked).toBe(false);
        // Pill mirrors intent — "standalone" while disabled.
        expect(selfCard.querySelector(".om-pill")).toBeTruthy();
        checkbox.checked = true;
        checkbox.dispatchEvent(new Event("change", { bubbles: true }));
        await tick();
        expect(onUpdateSelfSync).toHaveBeenCalledWith(true);
    });

    it("sync-status pill labels: peer.sync && online → syncing", async () => {
        const container = document.createElement("div");
        const recent = new Date(Date.now() - 5_000).toISOString();
        mount(container, {
            peers: [PEER({ sync: true, last_seen_at: recent })],
        });
        await tick();
        const card = container.querySelector(".om-peer-card[data-peer-id]");
        const pill = card.querySelector(".om-peer-actions .om-pill");
        expect(pill.textContent).toMatch(/^syncing$/);
    });

    it("sync-status pill labels: !peer.sync && online → standalone", async () => {
        const container = document.createElement("div");
        const recent = new Date(Date.now() - 5_000).toISOString();
        mount(container, {
            peers: [PEER({ sync: false, last_seen_at: recent })],
        });
        await tick();
        const card = container.querySelector(".om-peer-card[data-peer-id]");
        const pill = card.querySelector(".om-peer-actions .om-pill");
        expect(pill.textContent).toMatch(/^standalone$/);
    });

    it("sync-status pill labels: peer.sync && offline → sync paused (intent-vs-reality mismatch)", async () => {
        const container = document.createElement("div");
        mount(container, {
            peers: [PEER({ sync: true, last_seen_at: null })],
        });
        await tick();
        const card = container.querySelector(".om-peer-card[data-peer-id]");
        // Two pills on this card: header "offline" + actions "sync paused".
        const actionPill = card.querySelector(".om-peer-actions .om-pill");
        expect(actionPill.textContent).toMatch(/sync paused/);
    });

    it("peer card thumbnail fetches the peer's current-thumbnail endpoint cross-origin", async () => {
        const seen = [];
        const fetchSpy = vi.fn(async (url) => {
            seen.push(String(url));
            return new Response(new Blob([new Uint8Array(0)], { type: "image/png" }));
        });
        vi.stubGlobal("fetch", fetchSpy);
        try {
            const container = document.createElement("div");
            const recent = new Date(Date.now() - 5_000).toISOString();
            mount(container, { peers: [PEER({ address: "127.0.0.1:9887", last_seen_at: recent })] });
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

    it("self card thumbnail fetches same-origin (no http://host prefix)", async () => {
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
            expect(
                seen.some((u) => /^\/api\/playback\/current-thumbnail\?t=\d+/.test(u)),
            ).toBe(true);
        } finally {
            vi.unstubAllGlobals();
        }
    });
});
