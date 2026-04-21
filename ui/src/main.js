// OpenMarquee web UI — entry point.
//
// Two modules live side by side in #app: the text-slide editor on top,
// the saved-slides list below. The editor pings the list to refresh after
// a successful save so the new slide shows up without a page reload.
//
// Panel dimensions are hardcoded to SYSTEM_SPEC defaults (128×96) for now;
// reading device config from the backend is a Phase 3 polish task.

import { deleteContent, listContent, playContent, saveTextSlide } from "./api.js";
import { mountEditor } from "./editor.js";
import { mountList } from "./list.js";

const PANEL_WIDTH = 128;
const PANEL_HEIGHT = 96;

function boot() {
    const root = document.getElementById("app");
    root.innerHTML = `
        <div class="editor-slot"></div>
        <div class="list-slot"></div>
    `;

    const list = mountList(root.querySelector(".list-slot"), {
        fetchItems: listContent,
        onPlay: playContent,
        onDelete: deleteContent,
    });

    mountEditor(root.querySelector(".editor-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        onSave: async (payload) => {
            const saved = await saveTextSlide(payload);
            await list.refresh();
            return saved;
        },
    });
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}
