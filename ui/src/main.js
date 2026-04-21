// OpenMarquee web UI — entry point.
//
// Four panels (slides, playlists, schedule, settings) mount into a sidebar
// shell. Sidebar nav (`nav.js`) toggles the active panel's `hidden`
// attribute; panels themselves stay mounted so their state survives
// navigation.
//
// Panel dimensions are hardcoded to SYSTEM_SPEC defaults (128×96) for now;
// a follow-up reads them from /api/settings once the editor can react to
// changes on the fly.

import {
    deletePlaylistByName,
    deleteContent,
    getPlaybackState,
    getSchedule,
    getSettings,
    listContent,
    listPlaylists,
    playContent,
    savePlaylistByName,
    saveImage,
    saveSchedule,
    saveTextSlide,
    saveVideo,
    setPlaylistOrder,
    startPlayback,
    stopPlayback,
} from "./api.js";
import { mountComposer } from "./composer.js";
import { mountEditor } from "./editor.js";
import { mountImageUploader } from "./image-upload.js";
import { mountList } from "./list.js";
import { mountNav } from "./nav.js";
import { mountPlaybackControls } from "./playback.js";
import { mountPlaylistsManager } from "./playlists.js";
import { mountSchedule } from "./schedule.js";
import { mountSettingsView } from "./settings-view.js";
import { mountVideoUploader } from "./video-upload.js";

const PANEL_WIDTH = 128;
const PANEL_HEIGHT = 96;

const SECTIONS = ["slides", "playlists", "schedule", "settings"];

function boot() {
    const root = document.getElementById("app");
    root.innerHTML = `
        <section data-section="slides" class="panel">
            <div class="editor-slot"></div>
            <div class="composer-slot"></div>
            <div class="image-upload-slot"></div>
            <div class="video-upload-slot"></div>
            <div class="playback-slot"></div>
            <div class="list-slot"></div>
        </section>
        <section data-section="playlists" class="panel">
            <div class="playlists-slot"></div>
        </section>
        <section data-section="schedule" class="panel">
            <div class="schedule-slot"></div>
        </section>
        <section data-section="settings" class="panel">
            <div class="settings-slot"></div>
        </section>
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

    mountComposer(root.querySelector(".composer-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        fetchItems: listContent,
        // Composite slides ride the ImageSlide API — a flat PNG is all the
        // server needs. The layer structure lives only in the browser tab.
        onSave: onSaveWithRefresh(saveImage),
    });

    mountVideoUploader(root.querySelector(".video-upload-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        onSave: onSaveWithRefresh(saveVideo),
    });

    mountPlaylistsManager(root.querySelector(".playlists-slot"), {
        fetchItems: listContent,
        fetchPlaylists: listPlaylists,
        onSave: savePlaylistByName,
        onDelete: deletePlaylistByName,
    });

    mountSchedule(root.querySelector(".schedule-slot"), {
        fetchSchedule: getSchedule,
        onSave: saveSchedule,
        fetchPlaylistNames: async () => {
            const collection = await listPlaylists();
            return Object.keys(collection.playlists || {}).sort();
        },
    });

    mountSettingsView(root.querySelector(".settings-slot"), {
        fetchSettings: getSettings,
    });

    mountNav({
        main: root,
        sidebar: document.querySelector(".sidebar"),
        sections: SECTIONS,
    });
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}
