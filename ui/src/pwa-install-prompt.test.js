// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    DISMISS_TTL_MS,
    isAlreadyInstalled,
    isIosSafari,
    markDismissed,
    mountPwaInstallPrompt,
    wasRecentlyDismissed,
} from "./pwa-install-prompt.js";

function makeStorage() {
    const map = new Map();
    return {
        getItem: (k) => (map.has(k) ? map.get(k) : null),
        setItem: (k, v) => map.set(k, String(v)),
        removeItem: (k) => map.delete(k),
        _map: map,
    };
}

function fakeWin({
    userAgent = "Mozilla/5.0",
    platform = "Win32",
    maxTouchPoints = 0,
    standalone = undefined,
    matchMediaMatches = false,
} = {}) {
    const listeners = new Map();
    const win = {
        navigator: { userAgent, platform, maxTouchPoints, standalone },
        matchMedia: () => ({ matches: matchMediaMatches }),
        document: window.document,
        addEventListener: (evt, cb) => {
            if (!listeners.has(evt)) listeners.set(evt, new Set());
            listeners.get(evt).add(cb);
        },
        removeEventListener: (evt, cb) => {
            const set = listeners.get(evt);
            if (set) set.delete(cb);
        },
        _fire: (evt, payload) => {
            const set = listeners.get(evt);
            if (!set) return;
            for (const cb of set) cb(payload);
        },
    };
    return win;
}

describe("isAlreadyInstalled", () => {
    it("returns true when display-mode: standalone matches", () => {
        const win = fakeWin({ matchMediaMatches: true });
        expect(isAlreadyInstalled(win)).toBe(true);
    });
    it("returns true when navigator.standalone === true (iOS legacy)", () => {
        const win = fakeWin({ standalone: true });
        expect(isAlreadyInstalled(win)).toBe(true);
    });
    it("returns false by default", () => {
        expect(isAlreadyInstalled(fakeWin())).toBe(false);
    });
    it("tolerates matchMedia throwing (older browsers)", () => {
        const win = fakeWin();
        win.matchMedia = () => {
            throw new Error("unknown query");
        };
        expect(isAlreadyInstalled(win)).toBe(false);
    });
});

describe("wasRecentlyDismissed", () => {
    it("returns false when no key set", () => {
        expect(wasRecentlyDismissed(makeStorage())).toBe(false);
    });
    it("returns true when dismissed just now", () => {
        const s = makeStorage();
        markDismissed(s, 1000);
        expect(wasRecentlyDismissed(s, 1000)).toBe(true);
    });
    it("returns false past the TTL boundary", () => {
        const s = makeStorage();
        markDismissed(s, 0);
        expect(wasRecentlyDismissed(s, DISMISS_TTL_MS + 1)).toBe(false);
    });
    it("returns false on unparseable value", () => {
        const s = makeStorage();
        s.setItem("om.pwa-install-dismissed-at", "not-a-number");
        expect(wasRecentlyDismissed(s)).toBe(false);
    });
    it("swallows getItem errors", () => {
        const bad = {
            getItem: () => {
                throw new Error("private mode");
            },
            setItem: () => {},
        };
        expect(wasRecentlyDismissed(bad)).toBe(false);
    });
});

describe("isIosSafari", () => {
    const IOS_SAFARI_UA =
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1";
    it("detects iPhone Safari", () => {
        const win = fakeWin({ userAgent: IOS_SAFARI_UA, platform: "iPhone" });
        expect(isIosSafari(win)).toBe(true);
    });
    it("detects iPadOS 13+ (MacIntel + touch) Safari", () => {
        const ua =
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15";
        const win = fakeWin({ userAgent: ua, platform: "MacIntel", maxTouchPoints: 5 });
        expect(isIosSafari(win)).toBe(true);
    });
    it("returns false for desktop Mac Safari (no touch)", () => {
        const ua =
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15";
        const win = fakeWin({ userAgent: ua, platform: "MacIntel", maxTouchPoints: 0 });
        expect(isIosSafari(win)).toBe(false);
    });
    it("returns false for Chrome on iOS (no PWA install path)", () => {
        const ua =
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/115.0.0.0 Mobile/15E148 Safari/604.1";
        const win = fakeWin({ userAgent: ua, platform: "iPhone" });
        expect(isIosSafari(win)).toBe(false);
    });
    it("returns false for Android Chrome", () => {
        const ua =
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Mobile Safari/537.36";
        const win = fakeWin({ userAgent: ua, platform: "Linux armv8l" });
        expect(isIosSafari(win)).toBe(false);
    });
});

describe("mountPwaInstallPrompt", () => {
    let container;
    beforeEach(() => {
        container = document.createElement("div");
        document.body.appendChild(container);
    });
    afterEach(() => {
        container.remove();
        container = null;
    });

    it("no-ops when already installed", () => {
        const win = fakeWin({ matchMediaMatches: true });
        mountPwaInstallPrompt(container, { win, storage: makeStorage() });
        expect(container.querySelector("[data-om-pwa-install]")).toBeNull();
    });

    it("no-ops when recently dismissed", () => {
        const s = makeStorage();
        markDismissed(s, Date.now());
        const win = fakeWin();
        mountPwaInstallPrompt(container, { win, storage: s });
        expect(container.querySelector("[data-om-pwa-install]")).toBeNull();
    });

    it("shows the iOS instructional banner on iOS Safari", () => {
        const win = fakeWin({
            userAgent:
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
            platform: "iPhone",
        });
        mountPwaInstallPrompt(container, { win, storage: makeStorage() });
        const banner = container.querySelector("[data-om-pwa-install]");
        expect(banner).not.toBeNull();
        // iOS copy mentions the Share icon + "Add to Home Screen".
        expect(banner.textContent).toMatch(/Share icon/i);
        expect(banner.textContent).toMatch(/Add to Home Screen/i);
        // NO Install button on iOS — only the manual instructions +
        // Not now.
        expect(banner.querySelector("[data-om-pwa-install-btn]")).toBeNull();
        expect(banner.querySelector("[data-om-pwa-install-dismiss]")).not.toBeNull();
    });

    it("renders on beforeinstallprompt and fires prompt() on Install click", async () => {
        const win = fakeWin();
        mountPwaInstallPrompt(container, { win, storage: makeStorage() });
        expect(container.querySelector("[data-om-pwa-install]")).toBeNull();

        // Simulate the browser firing beforeinstallprompt.
        const prompt = vi.fn();
        const userChoice = Promise.resolve({ outcome: "accepted" });
        const evt = {
            preventDefault: vi.fn(),
            prompt,
            userChoice,
        };
        win._fire("beforeinstallprompt", evt);
        expect(evt.preventDefault).toHaveBeenCalled();
        const banner = container.querySelector("[data-om-pwa-install]");
        expect(banner).not.toBeNull();
        const installBtn = banner.querySelector("[data-om-pwa-install-btn]");
        expect(installBtn).not.toBeNull();
        installBtn.click();
        expect(prompt).toHaveBeenCalled();
        // Await the userChoice resolution + let the async-cleanup
        // microtask land before asserting the tear-down.
        await userChoice;
        await Promise.resolve();
        expect(container.querySelector("[data-om-pwa-install]")).toBeNull();
    });

    it("dismisses on Not now and persists the TTL flag", () => {
        const win = fakeWin({
            userAgent:
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
            platform: "iPhone",
        });
        const s = makeStorage();
        mountPwaInstallPrompt(container, { win, storage: s });
        const dismiss = container.querySelector("[data-om-pwa-install-dismiss]");
        expect(dismiss).not.toBeNull();
        dismiss.click();
        expect(container.querySelector("[data-om-pwa-install]")).toBeNull();
        expect(s._map.has("om.pwa-install-dismissed-at")).toBe(true);
    });

    it("marks dismissed when the user rejects the native prompt", async () => {
        const win = fakeWin();
        const s = makeStorage();
        mountPwaInstallPrompt(container, { win, storage: s });
        const userChoice = Promise.resolve({ outcome: "dismissed" });
        win._fire("beforeinstallprompt", {
            preventDefault: () => {},
            prompt: () => {},
            userChoice,
        });
        container.querySelector("[data-om-pwa-install-btn]").click();
        await userChoice;
        await Promise.resolve();
        expect(s._map.has("om.pwa-install-dismissed-at")).toBe(true);
    });

    it("tears down banner on appinstalled", () => {
        const win = fakeWin();
        mountPwaInstallPrompt(container, { win, storage: makeStorage() });
        win._fire("beforeinstallprompt", {
            preventDefault: () => {},
            prompt: () => {},
            userChoice: Promise.resolve({ outcome: "accepted" }),
        });
        expect(container.querySelector("[data-om-pwa-install]")).not.toBeNull();
        win._fire("appinstalled", {});
        expect(container.querySelector("[data-om-pwa-install]")).toBeNull();
    });

    it("destroy() removes listeners + banner", () => {
        const win = fakeWin();
        const ctrl = mountPwaInstallPrompt(container, { win, storage: makeStorage() });
        win._fire("beforeinstallprompt", {
            preventDefault: () => {},
            prompt: () => {},
            userChoice: Promise.resolve({ outcome: "accepted" }),
        });
        ctrl.destroy();
        expect(container.querySelector("[data-om-pwa-install]")).toBeNull();
        // After destroy, a fresh beforeinstallprompt should NOT
        // re-render.
        win._fire("beforeinstallprompt", {
            preventDefault: () => {},
            prompt: () => {},
            userChoice: Promise.resolve({ outcome: "accepted" }),
        });
        expect(container.querySelector("[data-om-pwa-install]")).toBeNull();
    });
});
