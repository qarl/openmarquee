// Focused tests for extractDetailMessage's two operator-UX
// improvements:
//   1. Suppress `(internal_error)` sentinel from slice-3 error shapes
//      (the Bundle-C-item-6 whitelist's "I won't tell you" fallback;
//      not a real exception class, no diagnostic value).
//   2. Include Pydantic `loc` (field path) in 422 messages so multi-
//      field forms get "name: string too long" instead of bare msg.
//
// This file deliberately runs in the default node environment
// (the vitest jsdom directive is intentionally absent) so it stays
// runnable while jsdom is virtiofs-wedged on this dev machine.
// api.js's top-level code is import-safe in node: `localStorage`
// + `window` are only touched inside function bodies, gated by
// `typeof ... !== "undefined"`. extractDetailMessage itself
// touches no DOM.
//
// IMPORTANT: do NOT mention the literal env-directive token in any
// comment in this file -- vitest 1.6's parser picks it up out of
// comments AND backtick-quoted sections AND triggers jsdom load,
// which is exactly what this file is structured to avoid.
//
// The "one-line" assertion update for the legacy 422 test lives in
// api.test.js (jsdom-env, can't run locally); CI is the gate for
// that single mechanical literal swap.
import { describe, expect, it } from "vitest";
import { extractDetailMessage } from "./api.js";

function makeResponse(body, { status = 400, statusText = "Bad Request" } = {}) {
    return {
        status,
        statusText,
        json: async () => body,
    };
}

describe("extractDetailMessage — internal_error sentinel suppression (Bundle C item 6)", () => {
    it("suppresses '(internal_error)' suffix and returns bare error code", async () => {
        // Backend whitelist canonical source: backend/openmarquee/
        // api_live.py:50-66. A non-whitelisted exception collapses
        // error_class to "internal_error"; surfacing it in toasts
        // wastes pill width and looks like a real Python class to
        // operators who don't know the whitelist exists.
        const r = makeResponse({
            detail: { error: "live_negotiation_failed", error_class: "internal_error" },
        });
        expect(await extractDetailMessage(r)).toBe("live_negotiation_failed");
    });

    it("STILL appends real whitelisted error_class names", async () => {
        // Inverse lock: the suppression must not over-broaden. The
        // 4 real whitelisted classes (OSError, ValueError,
        // TimeoutError, ConnectionError) still get the parenthetical.
        for (const cls of ["OSError", "ValueError", "TimeoutError", "ConnectionError"]) {
            const r = makeResponse({
                detail: { error: "live_negotiation_failed", error_class: cls },
            });
            expect(await extractDetailMessage(r)).toBe(`live_negotiation_failed (${cls})`);
        }
    });

    it("falls back to bare error when error_class is absent (legacy path)", async () => {
        // Pre-Bundle-C-item-6 shape: error_class field missing
        // entirely. Behavior unchanged from before this commit.
        const r = makeResponse({ detail: { error: "live_negotiation_failed" } });
        expect(await extractDetailMessage(r)).toBe("live_negotiation_failed");
    });
});

describe("extractDetailMessage — Pydantic loc field path (422)", () => {
    it("includes loc field name as prefix: 'name: msg'", async () => {
        const r = makeResponse(
            { detail: [{ msg: "string too long", loc: ["body", "name"] }] },
            { status: 422, statusText: "Unprocessable Entity" },
        );
        expect(await extractDetailMessage(r)).toBe("name: string too long");
    });

    it("dot-joins nested loc paths and filters the 'body' sentinel", async () => {
        // Nested case: settings.wifi_ssid validation failure. The
        // outer "body" segment is FastAPI's request-body wrapper
        // and adds no operator value; it gets filtered.
        const r = makeResponse(
            { detail: [{ msg: "invalid SSID", loc: ["body", "settings", "wifi_ssid"] }] },
            { status: 422, statusText: "Unprocessable Entity" },
        );
        expect(await extractDetailMessage(r)).toBe("settings.wifi_ssid: invalid SSID");
    });

    it("falls back to bare msg when loc is missing (older / non-Pydantic 422)", async () => {
        // Older endpoints or hand-rolled 422 envelopes may omit loc.
        // Don't lose the message just because the field path is
        // absent.
        const r = makeResponse(
            { detail: [{ msg: "raw validation failure" }] },
            { status: 422, statusText: "Unprocessable Entity" },
        );
        expect(await extractDetailMessage(r)).toBe("raw validation failure");
    });

    it("falls back to bare msg when loc is present but contains only 'body'", async () => {
        // Defensive: if the whole loc array filters down to empty
        // (only "body" present), the prefix would be empty too --
        // omit it rather than emit a leading ": ".
        const r = makeResponse(
            { detail: [{ msg: "whole body invalid", loc: ["body"] }] },
            { status: 422, statusText: "Unprocessable Entity" },
        );
        expect(await extractDetailMessage(r)).toBe("whole body invalid");
    });
});
