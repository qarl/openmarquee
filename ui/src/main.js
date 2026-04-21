// OpenMarquee web UI — entry point.
//
// Wires the text-slide editor into #app. Panel dimensions are hardcoded
// to match the SYSTEM_SPEC defaults (128x96) for now; when we read device
// config from the backend (Phase 3+ polish), swap in the real values.

import { saveTextSlide } from "./api.js";
import { mountEditor } from "./editor.js";

const PANEL_WIDTH = 128;
const PANEL_HEIGHT = 96;

function boot() {
    const root = document.getElementById("app");
    mountEditor(root, {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        onSave: saveTextSlide,
    });
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}
