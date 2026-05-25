// @vitest-environment jsdom
//
// Tests for font-picker.js. Two groups:
//   1. cssFontFamily table-test (pure function, exercised in node-
//      compatible code, but the file's jsdom env is required for
//      the second group below).
//   2. setupFontPicker tile-click flow — pins the dual-event-dispatch
//      contract (the load-bearing fix for a previous live-preview
//      regression has no other guardrail today).
import { afterEach, describe, expect, it } from "vitest";
import { cssFontFamily, FONT_FAMILIES, setupFontPicker } from "./font-picker.js";

describe("cssFontFamily", () => {
    it("returns generics unquoted with Noto Color Emoji suffix", () => {
        // The unquoted-vs-quoted contract is load-bearing for
        // ctx.font (canvas silently drops an unquoted multi-word
        // family). Lock the two halves separately so a future
        // refactor that quotes ALL families (or none) surfaces here.
        expect(cssFontFamily("sans-serif")).toBe('sans-serif, "Noto Color Emoji"');
        expect(cssFontFamily("serif")).toBe('serif, "Noto Color Emoji"');
        expect(cssFontFamily("monospace")).toBe('monospace, "Noto Color Emoji"');
        expect(cssFontFamily("cursive")).toBe('cursive, "Noto Color Emoji"');
        expect(cssFontFamily("fantasy")).toBe('fantasy, "Noto Color Emoji"');
        expect(cssFontFamily("system-ui")).toBe('system-ui, "Noto Color Emoji"');
    });

    it("returns named families quoted with Noto Color Emoji suffix", () => {
        expect(cssFontFamily("Inter")).toBe('"Inter", "Noto Color Emoji"');
        expect(cssFontFamily("Bebas Neue")).toBe('"Bebas Neue", "Noto Color Emoji"');
        expect(cssFontFamily("Roboto Slab")).toBe('"Roboto Slab", "Noto Color Emoji"');
        expect(cssFontFamily("Playfair Display")).toBe(
            '"Playfair Display", "Noto Color Emoji"',
        );
    });
});

// Helper: build the minimal layerEl shape setupFontPicker expects.
// Mirrors the production HTML wrapper around a text layer card.
function makeLayerEl() {
    const layerEl = document.createElement("div");
    layerEl.innerHTML = `
        <select class="field-font-family">
            <option value="sans-serif">Sans-serif</option>
            <option value="Inter">Inter</option>
            <option value="Bebas Neue">Bebas Neue</option>
        </select>
        <button class="font-picker-trigger" aria-expanded="false">
            <span class="font-picker-trigger-label">Sans-serif</span>
        </button>
        <div class="font-picker-popover" hidden></div>
    `;
    document.body.appendChild(layerEl);
    return layerEl;
}

describe("setupFontPicker — tile click contract", () => {
    afterEach(() => { document.body.innerHTML = ""; });

    it("populates one tile per font family across all sections", () => {
        const layerEl = makeLayerEl();
        setupFontPicker(layerEl);
        const tiles = layerEl.querySelectorAll(".font-picker-popover .font-picker-tile");
        expect(tiles.length).toBe(FONT_FAMILIES.length);
    });

    it("dispatches BOTH input AND change events on tile click", () => {
        // Load-bearing regression-lock. The dual dispatch fixes a
        // prior live-preview staleness bug (see font-picker.js:141-
        // 146). A future "clean up redundant event" refactor would
        // silently regress without this test.
        const layerEl = makeLayerEl();
        setupFontPicker(layerEl);
        const selectEl = layerEl.querySelector(".field-font-family");
        const eventLog = [];
        selectEl.addEventListener("input", (e) => { eventLog.push(["input", e.target.value]); });
        selectEl.addEventListener("change", (e) => { eventLog.push(["change", e.target.value]); });

        const interTile = layerEl.querySelector(
            '.font-picker-popover .font-picker-tile[data-value="Inter"]',
        );
        interTile.click();

        expect(selectEl.value).toBe("Inter");
        expect(eventLog).toEqual([
            ["input", "Inter"],
            ["change", "Inter"],
        ]);
    });

    it("marks the clicked tile selected + aria-selected='true' and clears the prior", () => {
        const layerEl = makeLayerEl();
        setupFontPicker(layerEl);
        const popover = layerEl.querySelector(".font-picker-popover");

        // Initial: sans-serif (selectEl default) is selected.
        const initial = popover.querySelector(
            '.font-picker-tile[data-value="sans-serif"]',
        );
        expect(initial.classList.contains("selected")).toBe(true);
        expect(initial.getAttribute("aria-selected")).toBe("true");

        // Click Bebas Neue.
        const bebas = popover.querySelector(
            '.font-picker-tile[data-value="Bebas Neue"]',
        );
        bebas.click();

        // Prior cleared.
        expect(initial.classList.contains("selected")).toBe(false);
        expect(initial.getAttribute("aria-selected")).toBe("false");
        // New marked.
        expect(bebas.classList.contains("selected")).toBe(true);
        expect(bebas.getAttribute("aria-selected")).toBe("true");
    });

    it("uses a section-head <div> with textContent (no innerHTML interpolation)", () => {
        // Pin Improvement 1: the section-head must be a real element
        // built via createElement + textContent, not via innerHTML
        // string interpolation. A regression to the template-literal
        // shape would re-introduce the unsafe pattern.
        const layerEl = makeLayerEl();
        setupFontPicker(layerEl);
        const popover = layerEl.querySelector(".font-picker-popover");
        const heads = popover.querySelectorAll(".font-picker-section-head");
        // 5 categories (System, Block, Serif, Mono, Script).
        expect(heads.length).toBe(5);
        for (const h of heads) {
            expect(h.tagName).toBe("DIV");
            expect(h.textContent.length).toBeGreaterThan(0);
        }
    });
});

