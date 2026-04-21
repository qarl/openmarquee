// OpenMarquee web UI — entry point.
//
// Two modules live side by side in #app: the text-slide editor on top,
// the saved-slides list below. The editor pings the list to refresh after
// a successful save so the new slide shows up without a page reload.
//
// Panel dimensions are hardcoded to SYSTEM_SPEC defaults (128×96) for now;
// reading device config from the backend is a Phase 3 polish task.

import {
    deleteContent,
    getPlaybackState,
    getSchedule,
    listContent,
    playContent,
    saveImage,
    saveSchedule,
    saveTextSlide,
    setPlaylistOrder,
    startPlayback,
    stopPlayback,
} from "./api.js";
import { mountEditor } from "./editor.js";
import { mountImageUploader } from "./image-upload.js";
import { mountList } from "./list.js";
import { mountPlaybackControls } from "./playback.js";
import { mountSchedule } from "./schedule.js";

const PANEL_WIDTH = 128;
const PANEL_HEIGHT = 96;

function boot() {
    const root = document.getElementById("app");
    root.innerHTML = `
        <div class="editor-slot"></div>
        <div class="image-upload-slot"></div>
        <div class="playback-slot"></div>
        <div class="list-slot"></div>
        <div class="schedule-slot"></div>
    `;

    const list = mountList(root.querySelector(".list-slot"), {
        fetchItems: listContent,
        onPlay: playContent,
        onDelete: deleteContent,
        onReorder: setPlaylistOrder,
    });

    mountPlaybackControls(root.querySelector(".playback-slot"), {
        fetchState: getPlaybackState,
        onStart: startPlayback,
        onStop: stopPlayback,
    });

    const onSaveWithRefresh = (saveFn) => async (payload) => {
        const saved = await saveFn(payload);
        await list.refresh();
        return saved;
    };

    mountEditor(root.querySelector(".editor-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        onSave: onSaveWithRefresh(saveTextSlide),
    });

    mountImageUploader(root.querySelector(".image-upload-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        onSave: onSaveWithRefresh(saveImage),
    });

    mountSchedule(root.querySelector(".schedule-slot"), {
        fetchSchedule: getSchedule,
        onSave: saveSchedule,
    });
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}
