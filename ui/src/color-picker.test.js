// @vitest-environment jsdom
// Tests for the curated-palette color picker widget.

import { describe, it, expect, beforeEach } from "vitest";
import {
    INLINE_SWATCHES,
    ALL_SWATCHES,
    mountColorPicker,
} from "./color-picker.js";

describe("color-picker palette constants", () => {
    it("has exactly 8 inline swatches", () => {
        expect(INLINE_SWATCHES).toHaveLength(8);
    });

    it("has exactly 17 total swatches in the complex picker", () => {
        expect(ALL_SWATCHES).toHaveLength(17);
    });

    it("each ALL_SWATCHES entry is in one of three layout rows", () => {
        const rows = new Set(ALL_SWATCHES.map((s) => s.row));
        expect(rows).toEqual(new Set(["darks", "lights", "wildcard"]));
    });

    it("8 darks + 8 lights + 1 wildcard, per the designer spec", () => {
        const counts = ALL_SWATCHES.reduce((acc, s) => {
            acc[s.row] = (acc[s.row] || 0) + 1;
            return acc;
        }, {});
        expect(counts).toEqual({ darks: 8, lights: 8, wildcard: 1 });
    });

    it("every swatch has a hex in valid #RRGGBB form", () => {
        for (const sw of ALL_SWATCHES) {
            expect(sw.hex).toMatch(/^#[0-9A-Fa-f]{6}$/);
        }
    });

    it("every inline swatch is also in the full ALL_SWATCHES list", () => {
        const allHexes = new Set(ALL_SWATCHES.map((s) => s.hex.toLowerCase()));
        for (const sw of INLINE_SWATCHES) {
            expect(allHexes).toContain(sw.hex.toLowerCase());
        }
    });
});

describe("mountColorPicker", () => {
    let input;
    let host;

    beforeEach(() => {
        host = document.createElement("div");
        document.body.appendChild(host);
        input = document.createElement("input");
        input.type = "color";
        input.value = "#ffffff";
        host.appendChild(input);
    });

    it("inserts a wrapper containing 8 inline swatches + a More button", () => {
        mountColorPicker(input);
        const wrap = host.querySelector(".om-color-picker");
        expect(wrap).not.toBeNull();
        const swatches = wrap.querySelectorAll(".om-color-picker-swatch");
        expect(swatches).toHaveLength(8);
        expect(wrap.querySelector(".om-color-picker-more")).not.toBeNull();
    });

    it("hides the original native input but keeps it in the form tree", () => {
        mountColorPicker(input);
        // Input still in DOM
        expect(host.contains(input)).toBe(true);
        // But hidden via class (CSS does the visual work)
        expect(input.classList.contains("om-color-picker-native")).toBe(true);
        // And out of keyboard focus order
        expect(input.tabIndex).toBe(-1);
    });

    it("marks the inline swatch matching the current value as selected", () => {
        // White (#FFFFFF) is in INLINE_SWATCHES, so the white swatch
        // should be selected on mount.
        mountColorPicker(input);
        const wrap = host.querySelector(".om-color-picker");
        const selected = wrap.querySelectorAll(".om-color-picker-swatch.is-selected");
        expect(selected).toHaveLength(1);
        expect(selected[0].dataset.hex.toLowerCase()).toBe("#ffffff");
    });

    it("clicking an inline swatch sets the input value + fires input event", () => {
        let lastEventValue = null;
        input.addEventListener("input", (e) => {
            lastEventValue = e.target.value;
        });
        mountColorPicker(input);
        const amberBtn = host.querySelector(
            '.om-color-picker-swatch[data-hex="#F4B755"]',
        );
        amberBtn.click();
        expect(input.value).toBe("#f4b755");
        expect(lastEventValue).toBe("#f4b755");
    });

    it("clicking a swatch updates the selected indicator", () => {
        mountColorPicker(input);
        const wrap = host.querySelector(".om-color-picker");
        const coralBtn = wrap.querySelector(
            '.om-color-picker-swatch[data-hex="#EC666F"]',
        );
        coralBtn.click();
        const selected = wrap.querySelectorAll(".om-color-picker-swatch.is-selected");
        expect(selected).toHaveLength(1);
        expect(selected[0].dataset.hex.toLowerCase()).toBe("#ec666f");
    });

    it("when current value isn't in inline 8, More button paints it", () => {
        // Set to a value that's NOT one of the inline 8 (Cream, in
        // the lights row but not inline).
        input.value = "#f3ecda";
        mountColorPicker(input);
        const wrap = host.querySelector(".om-color-picker");
        const moreBtn = wrap.querySelector(".om-color-picker-more");
        expect(moreBtn.classList.contains("has-custom-preview")).toBe(true);
        // No inline swatch should be marked selected.
        expect(wrap.querySelectorAll(".om-color-picker-swatch.is-selected"))
            .toHaveLength(0);
    });

    it("when current value is custom (off-palette), More button paints it", () => {
        input.value = "#123456";
        mountColorPicker(input);
        const moreBtn = host.querySelector(".om-color-picker-more");
        expect(moreBtn.classList.contains("has-custom-preview")).toBe(true);
        // Style ends up on the element so CSS can read it.
        expect(moreBtn.style.getPropertyValue("--om-swatch")).toBe("#123456");
    });

    it("external value changes refresh the selected indicator", () => {
        mountColorPicker(input);
        const wrap = host.querySelector(".om-color-picker");
        // Initial: white selected.
        expect(
            wrap.querySelector('.om-color-picker-swatch[data-hex="#FFFFFF"]')
                .classList.contains("is-selected"),
        ).toBe(true);
        // Simulate an external set (e.g., loadForEdit hydrating the
        // input from a saved slide).
        input.value = "#86ed9d"; // Mint
        input.dispatchEvent(new Event("input", { bubbles: true }));
        expect(
            wrap.querySelector('.om-color-picker-swatch[data-hex="#FFFFFF"]')
                .classList.contains("is-selected"),
        ).toBe(false);
        expect(
            wrap.querySelector('.om-color-picker-swatch[data-hex="#86ED9D"]')
                .classList.contains("is-selected"),
        ).toBe(true);
    });

    it("clicking More opens a popover containing all 17 swatches", () => {
        mountColorPicker(input);
        const moreBtn = host.querySelector(".om-color-picker-more");
        moreBtn.click();
        const popover = document.querySelector(".om-color-picker-popover");
        expect(popover).not.toBeNull();
        expect(
            popover.querySelectorAll(".om-color-picker-swatch"),
        ).toHaveLength(17);
        // Cleanup so it doesn't leak between tests.
        popover.remove();
    });

    it("popover has 3 rows matching the designer layout", () => {
        mountColorPicker(input);
        host.querySelector(".om-color-picker-more").click();
        const popover = document.querySelector(".om-color-picker-popover");
        const darkRow = popover.querySelector(".om-color-picker-popover-darks");
        const lightRow = popover.querySelector(".om-color-picker-popover-lights");
        const wild = popover.querySelector(".om-color-picker-popover-wildcard");
        expect(darkRow.querySelectorAll(".om-color-picker-swatch")).toHaveLength(8);
        expect(lightRow.querySelectorAll(".om-color-picker-swatch")).toHaveLength(8);
        expect(wild.querySelectorAll(".om-color-picker-swatch")).toHaveLength(1);
        popover.remove();
    });

    it("popover has a custom hex input + native OS picker button", () => {
        mountColorPicker(input);
        host.querySelector(".om-color-picker-more").click();
        const popover = document.querySelector(".om-color-picker-popover");
        expect(popover.querySelector(".om-color-picker-popover-hex input"))
            .not.toBeNull();
        expect(popover.querySelector(".om-color-picker-popover-native"))
            .not.toBeNull();
        popover.remove();
    });

    it("clicking a popover swatch sets value + closes popover", () => {
        mountColorPicker(input);
        host.querySelector(".om-color-picker-more").click();
        const popover = document.querySelector(".om-color-picker-popover");
        const navySwatch = popover.querySelector(
            '.om-color-picker-swatch[data-hex="#0C1226"]',
        );
        navySwatch.click();
        // Popover should auto-close on swatch pick.
        expect(document.querySelector(".om-color-picker-popover")).toBeNull();
        expect(input.value).toBe("#0c1226");
    });

    it("submitting custom hex from popover sets the value", () => {
        mountColorPicker(input);
        host.querySelector(".om-color-picker-more").click();
        const popover = document.querySelector(".om-color-picker-popover");
        const hexInput = popover.querySelector(".om-color-picker-popover-hex input");
        hexInput.value = "#ABCDEF";
        // Press Enter to commit.
        hexInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
        expect(input.value).toBe("#abcdef");
        // Popover closed.
        expect(document.querySelector(".om-color-picker-popover")).toBeNull();
    });

    it("invalid custom hex doesn't commit", () => {
        mountColorPicker(input);
        const initial = input.value;
        host.querySelector(".om-color-picker-more").click();
        const popover = document.querySelector(".om-color-picker-popover");
        const hexInput = popover.querySelector(".om-color-picker-popover-hex input");
        hexInput.value = "not a hex";
        hexInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
        expect(input.value).toBe(initial);
        // Popover stays open since nothing was committed.
        expect(document.querySelector(".om-color-picker-popover")).not.toBeNull();
        document.querySelector(".om-color-picker-popover")?.remove();
    });

    it("calls optional onChange callback when a swatch is clicked", () => {
        let captured = null;
        mountColorPicker(input, {
            onChange: (hex) => { captured = hex; },
        });
        host.querySelector(
            '.om-color-picker-swatch[data-hex="#7FD2FB"]',
        ).click();
        expect(captured).toBe("#7fd2fb");
    });
});
