// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountOnboarding } from "./onboarding.js";

afterEach(() => {
    vi.restoreAllMocks();
});

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

// Minimal fixture matching onboarding.html's data-* contract.
function makeRoot() {
    const root = document.createElement("section");
    root.setAttribute("data-onboarding-root", "");
    root.innerHTML = `
        <form data-form="onboarding" novalidate>
            <input name="ssid" type="text">
            <input name="password" type="password">
            <button type="button" data-reveal-for="password">Show</button>
            <button type="submit" disabled>Connect</button>
            <p data-status></p>
            <p data-form-error></p>
        </form>
        <div data-done hidden><a href="/">Continue</a></div>
    `;
    return root;
}

// Deterministic mount: instant wait (no real timers), fetch injected,
// navigate stubbed so success paths don't try to jsdom-navigate.
function mount(root, fetchImpl, extra = {}) {
    mountOnboarding(root, {
        fetch: fetchImpl,
        wait: () => Promise.resolve(),
        pollIntervalMs: 0,
        redirectDelayMs: 0,
        navigate: () => {},
        ...extra,
    });
}

const $ = (root, sel) => root.querySelector(sel);

describe("mountOnboarding", () => {
    it("keeps Connect disabled until ssid + 8-char password", () => {
        const root = makeRoot();
        mount(root, vi.fn());
        const submit = $(root, 'button[type="submit"]');
        const ssid = $(root, 'input[name="ssid"]');
        const pw = $(root, 'input[name="password"]');
        expect(submit.disabled).toBe(true);

        ssid.value = "HomeNet";
        ssid.dispatchEvent(new Event("input"));
        expect(submit.disabled).toBe(true); // no password yet

        pw.value = "short";
        pw.dispatchEvent(new Event("input"));
        expect(submit.disabled).toBe(true); // < 8 chars

        pw.value = "goodpassword";
        pw.dispatchEvent(new Event("input"));
        expect(submit.disabled).toBe(false);
    });

    it("reveal button toggles the password field type", () => {
        const root = makeRoot();
        mount(root, vi.fn());
        const pw = $(root, 'input[name="password"]');
        const reveal = $(root, "[data-reveal-for]");
        expect(pw.type).toBe("password");
        reveal.click();
        expect(pw.type).toBe("text");
        reveal.click();
        expect(pw.type).toBe("password");
    });

    it("POSTs the creds then polls to success (ONLINE) and shows the continue link", async () => {
        const root = makeRoot();
        const calls = [];
        const fetchImpl = vi.fn(async (url, init) => {
            calls.push({ url, init });
            if (url.includes("submit-credentials")) {
                return { ok: true, status: 202, json: async () => ({ status: "submitted" }) };
            }
            // status poll: CONNECTING, then ONLINE.
            const n = calls.filter((c) => c.url.includes("/status")).length;
            const state = n < 2 ? "CONNECTING" : "ONLINE";
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    state,
                    wifi_station_state: state,
                    wifi_station_ssid: "HomeNet",
                }),
            };
        });
        const navigate = vi.fn();
        mount(root, fetchImpl, { navigate });

        $(root, 'input[name="ssid"]').value = "HomeNet";
        $(root, 'input[name="ssid"]').dispatchEvent(new Event("input"));
        $(root, 'input[name="password"]').value = "goodpassword";
        $(root, 'input[name="password"]').dispatchEvent(new Event("input"));
        $(root, "form").dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));

        // Let the submit + poll loop drain (instant wait).
        for (let i = 0; i < 10; i++) await tick();

        const post = calls.find((c) => c.url.includes("submit-credentials"));
        expect(post.init.method).toBe("POST");
        expect(JSON.parse(post.init.body)).toEqual({
            ssid: "HomeNet",
            password: "goodpassword",
        });
        expect($(root, "[data-status]").textContent).toMatch(/connected to homenet/i);
        expect($(root, "[data-done]").hidden).toBe(false);
        // Hard auto-redirect to the dashboard fired.
        expect(navigate).toHaveBeenCalledTimes(1);
    });

    it("treats LINGER as connected (the default-regime success state)", async () => {
        const root = makeRoot();
        const fetchImpl = vi.fn(async (url) => {
            if (url.includes("submit-credentials")) {
                return { ok: true, status: 200, json: async () => ({}) };
            }
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    state: "LINGER",
                    wifi_station_state: "LINGER",
                    wifi_station_ssid: "HomeNet",
                }),
            };
        });
        const navigate = vi.fn();
        mount(root, fetchImpl, { navigate });
        $(root, 'input[name="ssid"]').value = "HomeNet";
        $(root, 'input[name="ssid"]').dispatchEvent(new Event("input"));
        $(root, 'input[name="password"]').value = "goodpassword";
        $(root, 'input[name="password"]').dispatchEvent(new Event("input"));
        $(root, "form").dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
        for (let i = 0; i < 6; i++) await tick();
        expect($(root, "[data-status]").textContent).toMatch(/connected/i);
        expect($(root, "[data-done]").hidden).toBe(false);
        // LINGER is a connected state → auto-redirect fires (before the
        // AP tears down on the LINGER→ONLINE transition).
        expect(navigate).toHaveBeenCalledTimes(1);
    });

    it("times out after maxPolls of CONNECTING and re-enables the form", async () => {
        const root = makeRoot();
        const fetchImpl = vi.fn(async (url) => {
            if (url.includes("submit-credentials")) {
                return { ok: true, status: 200, json: async () => ({}) };
            }
            // Never settles — always CONNECTING.
            return {
                ok: true,
                status: 200,
                json: async () => ({ state: "CONNECTING", wifi_station_state: "CONNECTING" }),
            };
        });
        const navigate = vi.fn();
        mount(root, fetchImpl, { maxPolls: 3, navigate });
        $(root, 'input[name="ssid"]').value = "HomeNet";
        $(root, 'input[name="ssid"]').dispatchEvent(new Event("input"));
        $(root, 'input[name="password"]').value = "goodpassword";
        $(root, 'input[name="password"]').dispatchEvent(new Event("input"));
        $(root, "form").dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
        for (let i = 0; i < 12; i++) await tick();
        // Bounded: exactly maxPolls status fetches, then gives up.
        const statusCalls = fetchImpl.mock.calls.filter(([u]) => u.includes("/status"));
        expect(statusCalls.length).toBe(3);
        expect($(root, "[data-status]").textContent).toMatch(/still trying/i);
        expect($(root, 'button[type="submit"]').disabled).toBe(false);
        // Never redirect on a non-connected outcome.
        expect(navigate).not.toHaveBeenCalled();
    });

    it("shows an error when the sign drops back to SETUP (bad password)", async () => {
        const root = makeRoot();
        const fetchImpl = vi.fn(async (url) => {
            if (url.includes("submit-credentials")) {
                return { ok: true, status: 202, json: async () => ({}) };
            }
            // First poll CONNECTING, then back to SETUP = rejected.
            fetchImpl._n = (fetchImpl._n || 0) + 1;
            const state = fetchImpl._n < 2 ? "CONNECTING" : "SETUP";
            return {
                ok: true,
                status: 200,
                json: async () => ({ state, wifi_station_state: state }),
            };
        });
        const navigate = vi.fn();
        mount(root, fetchImpl, { navigate });

        $(root, 'input[name="ssid"]').value = "HomeNet";
        $(root, 'input[name="ssid"]').dispatchEvent(new Event("input"));
        $(root, 'input[name="password"]').value = "wrongpassword";
        $(root, 'input[name="password"]').dispatchEvent(new Event("input"));
        $(root, "form").dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
        for (let i = 0; i < 10; i++) await tick();

        expect($(root, "[data-form-error]").textContent).toMatch(/couldn't connect/i);
        expect($(root, 'button[type="submit"]').disabled).toBe(false);
        expect($(root, "[data-done]").hidden).toBe(true);
        // A rejected password must NOT redirect.
        expect(navigate).not.toHaveBeenCalled();
    });

    it("surfaces a submit-credentials failure without polling", async () => {
        const root = makeRoot();
        const fetchImpl = vi.fn(async (url) => {
            if (url.includes("submit-credentials")) {
                return { ok: false, status: 422, json: async () => ({ detail: "bad ssid" }) };
            }
            throw new Error("should not poll after submit failure");
        });
        mount(root, fetchImpl);

        $(root, 'input[name="ssid"]').value = "HomeNet";
        $(root, 'input[name="ssid"]').dispatchEvent(new Event("input"));
        $(root, 'input[name="password"]').value = "goodpassword";
        $(root, 'input[name="password"]').dispatchEvent(new Event("input"));
        $(root, "form").dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
        for (let i = 0; i < 5; i++) await tick();

        expect($(root, "[data-form-error]").textContent).toMatch(/bad ssid/i);
        // Only the submit-credentials call happened, no status polls.
        expect(fetchImpl.mock.calls.every(([u]) => !u.includes("/status"))).toBe(true);
    });
});
