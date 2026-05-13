import { describe, expect, it } from "vitest";
import { stateFromItem } from "./state-from-item.js";

describe("stateFromItem", () => {
    it("defaults bgSource to color with backgroundColor fallback", () => {
        const state = stateFromItem({ background_color: "#abcdef" });
        expect(state.bgSource).toBe("color");
        expect(state.backgroundColor).toBe("#abcdef");
        expect(state.bgPattern).toBeNull();
        expect(state.bgImage).toBeNull();
    });

    it("defaults backgroundColor to black when background_color absent", () => {
        const state = stateFromItem({});
        expect(state.backgroundColor).toBe("#000000");
        expect(state.bgSource).toBe("color");
    });

    it("routes background_pattern → bgSource=pattern + bgPattern shape", () => {
        // qarl 2026-05-13 UNCAGE regression: this is the lookup the
        // playlist inline-preview was previously skipping.
        const state = stateFromItem({
            background_color: "#050608",
            background_pattern: {
                pattern: "gradient",
                color_a: "#FFB43C",
                color_b: "#5E1A1A",
                density: 0.0,
            },
        });
        expect(state.bgSource).toBe("pattern");
        expect(state.bgPattern).toEqual({
            pattern: "gradient",
            color_a: "#FFB43C",
            color_b: "#5E1A1A",
            density: 0.0,
        });
        // backgroundColor stays populated so drawCanvas can fall back
        // if a pattern paint somehow no-ops.
        expect(state.backgroundColor).toBe("#050608");
    });

    it("routes background_image_slide_id + resolved image → bgSource=slide", () => {
        const fakeImg = { width: 1920, height: 1080 };
        const state = stateFromItem(
            {
                background_image_slide_id: "bg-uuid",
                background_color: "#000",
            },
            fakeImg,
        );
        expect(state.bgSource).toBe("slide");
        expect(state.bgImage).toBe(fakeImg);
    });

    it("falls back to color when bg slide referenced but image unresolved", () => {
        // First-frame race: bg image not yet loaded. drawCanvas needs
        // to paint *something* — the solid color is the legible fallback
        // until getCachedImage's load handler triggers a renderOnce.
        const state = stateFromItem(
            { background_image_slide_id: "bg-uuid", background_color: "#222" },
            null,
        );
        expect(state.bgSource).toBe("color");
        expect(state.backgroundColor).toBe("#222");
    });

    it("bg slide preference wins over background_pattern when both present", () => {
        // Defensive: malformed item with both fields. The wire schema
        // shouldn't produce this, but the helper's branch order pins
        // the resolution so a future schema change doesn't silently
        // flip which bg wins.
        const fakeImg = { width: 100, height: 100 };
        const state = stateFromItem(
            {
                background_image_slide_id: "bg-uuid",
                background_pattern: { pattern: "gradient" },
            },
            fakeImg,
        );
        expect(state.bgSource).toBe("slide");
        expect(state.bgPattern).toBeNull();
    });

    it("passes text_layers through verbatim", () => {
        const layers = [
            { text: "HELLO", motion: "shake" },
            { text: "world", motion: "static" },
        ];
        const state = stateFromItem({ text_layers: layers });
        expect(state.layers).toBe(layers);
    });

    it("empty/missing text_layers yields empty layers array", () => {
        expect(stateFromItem({}).layers).toEqual([]);
        expect(stateFromItem({ text_layers: null }).layers).toEqual([]);
    });
});
