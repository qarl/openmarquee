import { afterEach, beforeEach, vi } from "vitest";

// Batch 20.4: Node 22+ ships an experimental built-in `localStorage`
// that shadows the jsdom one on globalThis (and apparently on
// window.localStorage too, in this vitest version). Its methods throw
// without --localstorage-file. The Batch 20.3 apiFetch reads
// `localStorage` on every call; without a usable store the tests
// can't stub tokens.
//
// Replace BOTH globalThis.localStorage AND window.localStorage with a
// per-test in-memory Map on every beforeEach. This is the simplest
// working shape -- jsdom's Storage API is what we want and a plain
// Map-backed shim is good enough for the apiFetch read/write paths
// (only setItem / getItem / removeItem are exercised).
beforeEach(() => {
    const store = new Map();
    const fake = {
        setItem: (k, v) => { store.set(String(k), String(v)); },
        getItem: (k) => (store.has(String(k)) ? store.get(String(k)) : null),
        removeItem: (k) => { store.delete(String(k)); },
        clear: () => { store.clear(); },
        key: (i) => Array.from(store.keys())[i] ?? null,
        get length() { return store.size; },
    };
    globalThis.localStorage = fake;
    if (typeof window !== "undefined") {
        try {
            Object.defineProperty(window, "localStorage", {
                configurable: true,
                writable: true,
                value: fake,
            });
        } catch {
            // Older jsdom defines localStorage as a getter that can't
            // be overwritten; fall back to globalThis only.
        }
    }
});

// Default fetch stub for every test file. mountSettings kicks off
// populateWifiScan / display-dims fetches during refresh(); under jsdom
// the relative URL parse throws and dumps a multi-line stack into stderr
// (QA 2026-04-26 #06, then #10 when the same noise re-surfaced via
// schedule.test.js mounting Settings transitively). Tests that need a
// real fetch mock still call vi.stubGlobal("fetch", ...) and override.
beforeEach(() => {
    vi.stubGlobal(
        "fetch",
        vi.fn(async (url) => {
            const path = String(url || "");
            if (path.endsWith("/api/system/wifi-scan")) {
                return new Response(JSON.stringify({ networks: [] }), {
                    status: 200,
                    headers: { "Content-Type": "application/json" },
                });
            }
            if (path.endsWith("/api/system/display-dims")) {
                return new Response(JSON.stringify({}), {
                    status: 200,
                    headers: { "Content-Type": "application/json" },
                });
            }
            return new Response("", { status: 404 });
        }),
    );
});

afterEach(() => {
    vi.unstubAllGlobals();
});
