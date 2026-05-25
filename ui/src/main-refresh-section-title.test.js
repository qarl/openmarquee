// @vitest-environment jsdom
//
// Tests for refreshSectionTitle + SECTION_TITLES (main.js).
//
// First piece of the target #7 boot() decomposition (per 680cd55
// survey). Confirms the module-scope-extracted helper preserves
// the pre-decomp behavior verbatim.
//
// jsdom is required for window.location.hash + document.querySelector
// + textContent. The directive at file top satisfies vitest 1.6 (per
// the file-top-only rule from
// feedback_vitest_directive_pickup_from_comments -- the env-directive
// token must not appear in any body comment).
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
    refreshSectionTitle,
    SECTION_TITLES,
} from "./main.js";

let slot;

beforeEach(() => {
    slot = document.createElement("span");
    slot.setAttribute("data-section-title", "");
    document.body.appendChild(slot);
});

afterEach(() => {
    document.body.innerHTML = "";
    // Reset hash so cross-test state doesn't leak (jsdom keeps the
    // last assignment between tests in the same file).
    window.location.hash = "";
});

describe("SECTION_TITLES", () => {
    it("contains the 11 documented section keys", () => {
        // Pin the contract. Adding a new section title is a real
        // change; this test surfaces it so the author re-reads the
        // SECTIONS routing table in main.js to confirm alignment.
        expect(Object.keys(SECTION_TITLES).sort()).toEqual([
            "flock",
            "live",
            "playlists",
            "schedule",
            "settings",
            "slides",
            "slides/image",
            "slides/stream",
            "slides/text",
            "slides/video",
            "slides/web",
        ]);
    });
});

describe("refreshSectionTitle", () => {
    it("paints the matching title for each known hash", () => {
        // Iterate SECTION_TITLES so the test stays in sync if the
        // table grows -- one less drift surface.
        for (const [hash, expectedTitle] of Object.entries(SECTION_TITLES)) {
            window.location.hash = `#/${hash}`;
            refreshSectionTitle();
            expect(slot.textContent).toBe(expectedTitle);
        }
    });

    it("falls back to 'Slides' for unknown hashes", () => {
        window.location.hash = "#/some-future-section-not-in-the-table";
        refreshSectionTitle();
        expect(slot.textContent).toBe("Slides");
    });

    it("falls back to 'Slides' for an empty hash", () => {
        window.location.hash = "";
        refreshSectionTitle();
        expect(slot.textContent).toBe("Slides");
    });

    it("strips the leading '#/' prefix when matching", () => {
        // Belt-and-suspenders for the hash-prefix regex
        // `(window.location.hash || "").replace(/^#\/?/, "")`: confirm
        // both "#/settings" and "#settings" (no slash) work.
        window.location.hash = "#settings";
        refreshSectionTitle();
        expect(slot.textContent).toBe("Settings");
    });

    it("no-ops when the [data-section-title] slot isn't in the DOM", () => {
        document.body.innerHTML = "";  // drop the slot the beforeEach added
        // Should NOT throw.
        expect(() => refreshSectionTitle()).not.toThrow();
    });
});
