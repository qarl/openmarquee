// Layer factory + auto-naming helpers. Extracted from editor.js
// (Batch 5.1). Public surface: defaultLayer, nextLayerName,
// makeAutoNamedLayer.
//
// A fresh layer carries a transparent box, center alignment, and
// the first font family in the bundle. Operators rarely accept the
// defaults verbatim, but they read sanely in preview before any
// edits land.

import { FONT_FAMILIES } from "./font-picker.js";
import { pickFontSizePct } from "./rasterize.js";

/**
 * Build a blank TextLayer with editor-state field names. Caller
 * usually overrides `.name` via `makeAutoNamedLayer` so the slide
 * has a meaningful label before any edits.
 */
export function defaultLayer() {
    return {
        text: "",
        name: "",
        textColor: "#FFFFFF",
        fontFamily: FONT_FAMILIES[0].value,
        fontSizePct: pickFontSizePct(),
        autoMode: null,
        autoFormat: null,
        textAlign: "center",
        anchor: "center",
        motion: "static",
        motionIntensity: 50,
        motionPhase: 0,
        motionSpeed: 1.0,
        blend: "normal",
        // r51: text effects (defaults off — operator opts in via the
        // editor "Text Effects" panel section). Editor-state field
        // names; loadForEdit + performSave bridge to wire snake_case
        // (outline / drop_shadow).
        outline: false,
        dropShadow: false,
        opacity: 1.0,
        visible: true,
        box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 },
    };
}

/**
 * Smallest unused "Layer N" given the slide's current layer names.
 * Custom-named layers ("Headline", "Hours") don't reserve a slot —
 * deleting "Layer 2" and adding fills back as "Layer 2". Mirrors
 * slide-browser's nextAutoName for the slide-title field. qarl
 * 2026-05-01.
 */
export function nextLayerName(layers) {
    const pattern = /^Layer (\d+)$/;
    const used = new Set();
    for (const layer of layers || []) {
        const m = (layer?.name || "").match(pattern);
        if (m) used.add(Number(m[1]));
    }
    let n = 1;
    while (used.has(n)) n += 1;
    return `Layer ${n}`;
}

/**
 * defaultLayer() pre-populated with the next-unused "Layer N" name
 * — used at editor mount, resetToBlank, and the +New affordance so
 * a fresh layer always has a real label.
 */
export function makeAutoNamedLayer(existingLayers) {
    const layer = defaultLayer();
    layer.name = nextLayerName(existingLayers);
    return layer;
}
