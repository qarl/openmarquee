import { afterEach, describe, expect, it, vi } from "vitest";
import { listTimezones } from "./iana-timezones.js";

afterEach(() => {
    vi.restoreAllMocks();
});

describe("listTimezones", () => {
    it("returns the browser's IANA zones when Intl.supportedValuesOf is available", () => {
        const zones = listTimezones();
        expect(Array.isArray(zones)).toBe(true);
        expect(zones.length).toBeGreaterThan(100);
        // Sanity: a handful of well-known zones appear.
        expect(zones).toContain("UTC");
        expect(zones).toContain("America/Los_Angeles");
        expect(zones).toContain("Europe/London");
    });

    it("falls back to a small bundled list when Intl.supportedValuesOf is missing", () => {
        // Simulate an ancient browser that predates supportedValuesOf.
        vi.spyOn(Intl, "supportedValuesOf").mockImplementation(() => {
            throw new Error("not supported");
        });
        const zones = listTimezones();
        expect(zones).toContain("UTC");
        expect(zones.length).toBeLessThan(50);
    });

    it("falls back when supportedValuesOf returns an empty array", () => {
        vi.spyOn(Intl, "supportedValuesOf").mockReturnValue([]);
        const zones = listTimezones();
        expect(zones.length).toBeGreaterThan(0);
    });
});
