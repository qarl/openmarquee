// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    _resetOverlayRegistryForTests,
    _tickOverlaysForTests,
    attachAutoTextOverlay,
} from "./auto-text-overlay.js";

beforeEach(() => {
    _resetOverlayRegistryForTests();
    document.body.innerHTML = "";
    vi.useFakeTimers();
});

afterEach(() => {
    vi.useRealTimers();
    _resetOverlayRegistryForTests();
});

describe("attachAutoTextOverlay", () => {
    it("returns null and adds nothing when item has no auto_mode", () => {
        const parent = document.createElement("div");
        document.body.appendChild(parent);
        const overlay = attachAutoTextOverlay(parent, {
            id: "x",
            type: "text_slide",
        });
        expect(overlay).toBeNull();
        expect(parent.querySelector(".om-auto-text-overlay")).toBeNull();
    });

    it("attaches an overlay div with the formatted token when auto_mode is set", () => {
        const parent = document.createElement("div");
        document.body.appendChild(parent);
        const overlay = attachAutoTextOverlay(parent, {
            id: "x",
            type: "text_slide",
            auto_mode: "time",
            auto_format: "time_hm",
        });
        expect(overlay).not.toBeNull();
        expect(overlay.classList.contains("om-auto-text-overlay")).toBe(true);
        expect(overlay.dataset.autoMode).toBe("time");
        expect(overlay.dataset.autoFormat).toBe("time_hm");
        expect(overlay.textContent).toMatch(/^\d\d:\d\d$/);
    });

    it("ticker updates every second when at least one overlay is mounted", () => {
        const parent = document.createElement("div");
        document.body.appendChild(parent);
        const overlay = attachAutoTextOverlay(parent, {
            id: "x",
            type: "text_slide",
            auto_mode: "time",
            auto_format: "time_hms",
        });
        const initial = overlay.textContent;
        // Advance the clock by a second AFTER the overlay was mounted —
        // need to manually invoke the tick because vitest's fake timers
        // and our setInterval don't advance the system clock.
        vi.setSystemTime(new Date("2026-04-30T12:34:56Z"));
        _tickOverlaysForTests();
        expect(overlay.textContent).toMatch(/^\d\d:\d\d:\d\d$/);
        expect(overlay.textContent).not.toBe(initial);
    });

    it("prunes overlays whose elements have left the DOM", () => {
        const parent = document.createElement("div");
        document.body.appendChild(parent);
        const overlay = attachAutoTextOverlay(parent, {
            id: "x",
            type: "text_slide",
            auto_mode: "day",
        });
        // Tile re-render: parent's innerHTML wipes; overlay element
        // becomes orphaned (no longer connected to the document).
        parent.innerHTML = "";
        expect(overlay.isConnected).toBe(false);
        // Tick after the orphaning — the registry should drain.
        _tickOverlaysForTests();
        // Adding a fresh overlay should restart the ticker (covered by
        // ensureTicker on each attach call).
        const fresh = attachAutoTextOverlay(parent, {
            id: "x",
            type: "text_slide",
            auto_mode: "day",
        });
        expect(fresh).not.toBeNull();
        expect(fresh.textContent.length).toBeGreaterThan(0);
    });
});
