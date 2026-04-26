import { afterEach, beforeEach, vi } from "vitest";

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
