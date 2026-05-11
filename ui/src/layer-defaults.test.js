// Tests for nextLayerName / makeAutoNamedLayer / defaultLayer
// (sweep #3 #3 -- ui/src/layer-defaults.js had zero unit tests).

import { describe, expect, it } from "vitest";
import {
    defaultLayer,
    makeAutoNamedLayer,
    nextLayerName,
} from "./layer-defaults.js";

describe("nextLayerName", () => {
    it("returns 'Layer 1' for an empty list", () => {
        expect(nextLayerName([])).toBe("Layer 1");
    });

    it("gap-fills the smallest unused slot", () => {
        const layers = [{ name: "Layer 1" }, { name: "Layer 3" }];
        // "Layer 2" is unused; gap-fill picks that.
        expect(nextLayerName(layers)).toBe("Layer 2");
    });

    it("counts up past N when no gap exists", () => {
        const layers = [
            { name: "Layer 1" },
            { name: "Layer 2" },
            { name: "Layer 3" },
        ];
        expect(nextLayerName(layers)).toBe("Layer 4");
    });

    it("custom-named layers don't reserve a slot", () => {
        // "Headline" doesn't match /^Layer (\d+)$/, so it doesn't
        // count toward the used slots. The result is "Layer 1".
        const layers = [{ name: "Headline" }, { name: "Hours" }];
        expect(nextLayerName(layers)).toBe("Layer 1");
    });

    it("handles null/undefined entries in the layers list", () => {
        // A defensive callsite might pass a list with a null
        // placeholder; the helper shouldn't crash.
        const layers = [{ name: "Layer 1" }, null, undefined];
        expect(nextLayerName(layers)).toBe("Layer 2");
    });

    it("handles undefined input by treating it as empty", () => {
        expect(nextLayerName(undefined)).toBe("Layer 1");
        expect(nextLayerName(null)).toBe("Layer 1");
    });
});

describe("makeAutoNamedLayer", () => {
    it("builds a defaultLayer with auto-named .name", () => {
        const layer = makeAutoNamedLayer([]);
        expect(layer.name).toBe("Layer 1");
        // Carries the defaults from defaultLayer().
        expect(layer.textColor).toBe("#FFFFFF");
        expect(layer.box).toEqual({ x: 0.1, y: 0.1, w: 0.8, h: 0.8 });
    });

    it("picks the next-unused N given the existing layers", () => {
        const layer = makeAutoNamedLayer([
            { name: "Layer 1" }, { name: "Layer 2" },
        ]);
        expect(layer.name).toBe("Layer 3");
    });
});

describe("defaultLayer", () => {
    it("returns a sane blank layer shape", () => {
        const layer = defaultLayer();
        expect(layer.text).toBe("");
        expect(layer.name).toBe("");
        expect(layer.motion).toBe("static");
        expect(layer.blend).toBe("normal");
        expect(layer.opacity).toBe(1.0);
        expect(layer.visible).toBe(true);
    });
});
