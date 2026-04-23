// @vitest-environment jsdom
// Regression for bug (#4): populateBgSlideOptions used to list every
// content item in the picker. Text-on-text or text-on-video are bad
// options (unreadable / bandwidth-contention at playback), so the
// picker should only surface image slides.

import { afterEach, describe, expect, it, vi } from "vitest";

import { populateBgSlideOptions } from "./editor.js";

function makeSelect() {
    // JSDOM's document is already globally available in the vitest
    // jsdom environment; just use it.
    return document.createElement("select");
}

function optionLabels(sel) {
    return Array.from(sel.querySelectorAll("option")).map((o) => o.textContent);
}

afterEach(() => {
    vi.restoreAllMocks();
});

describe("populateBgSlideOptions — type filter", () => {
    it("lists only image slides (drops text + video + anything else)", async () => {
        const sel = makeSelect();
        const statusEl = document.createElement("p");
        const fetchItems = vi.fn(async () => [
            { id: "t1", type: "text_slide", name: "TxtA" },
            { id: "i1", type: "image", name: "ImgA" },
            { id: "v1", type: "video", name: "VidA" },
            { id: "i2", type: "image", name: "ImgB" },
            // Defensive: a hypothetical future type should also be excluded.
            { id: "x1", type: "widget", name: "Widget" },
        ]);

        await populateBgSlideOptions(sel, fetchItems, statusEl);

        expect(optionLabels(sel)).toEqual(["(pick a slide)", "ImgA", "ImgB"]);
    });

    it("preserves the empty-placeholder first option when no images exist", async () => {
        const sel = makeSelect();
        const statusEl = document.createElement("p");
        const fetchItems = vi.fn(async () => [
            { id: "t1", type: "text_slide", name: "TxtOnly" },
        ]);

        await populateBgSlideOptions(sel, fetchItems, statusEl);

        expect(optionLabels(sel)).toEqual(["(pick a slide)"]);
    });

    it("uses item.name for the label, falling back to item.text then Untitled", async () => {
        const sel = makeSelect();
        const statusEl = document.createElement("p");
        const fetchItems = vi.fn(async () => [
            { id: "i1", type: "image", name: "Named" },
            { id: "i2", type: "image", text: "FromText" }, // no name
            { id: "i3", type: "image" }, // no name, no text
        ]);

        await populateBgSlideOptions(sel, fetchItems, statusEl);

        expect(optionLabels(sel)).toEqual([
            "(pick a slide)",
            "Named",
            "FromText",
            "Untitled",
        ]);
    });
});
