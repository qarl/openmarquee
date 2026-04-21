// openMarquee web UI — entry point.
//
// Four panels (slides, playlists, schedule, settings) mount into a sidebar
// shell. Sidebar nav (`nav.js`) toggles the active panel's `hidden`
// attribute; panels themselves stay mounted so their state survives
// navigation.
//
// Panel dimensions come from /api/settings at boot so every preview canvas
// matches the device's configured display aspect ratio (128×96 is just
// the SYSTEM_SPEC default; operators with a 1920×1080 HDMI target see a
// 16:9 preview). Changes made in the Settings form take effect on the
// next page load — re-mounting the whole app on settings save is a
// future refinement.

import {
    deletePlaylistByName,
    deleteContent,
    generateBackground,
    getPlaybackState,
    getSchedule,
    getSettings,
    listContent,
    listPlaylists,
    playContent,
    savePlaylistByName,
    saveImage,
    saveSchedule,
    saveSettings,
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
import { mountSettings } from "./settings.js";
import { mountVideoUploader } from "./video-upload.js";

// Fallback dims if /api/settings can't be reached — matches SYSTEM_SPEC
// §3.4 defaults so the editor at least renders something usable in an
// offline / broken-API scenario.
const FALLBACK_WIDTH = 128;
const FALLBACK_HEIGHT = 96;

const SECTIONS = ["slides", "playlists", "schedule", "settings"];

async function resolvePanelDims() {
    try {
        const settings = await getSettings();
        const w = Number(settings.display_width);
        const h = Number(settings.display_height);
        if (Number.isFinite(w) && w > 0 && Number.isFinite(h) && h > 0) {
            return { width: w, height: h };
        }
    } catch {
        // Fall through to fallback — editor still mounts even if the
        // settings endpoint is briefly unavailable on boot.
    }
    return { width: FALLBACK_WIDTH, height: FALLBACK_HEIGHT };
}

async function boot() {
    const { width: PANEL_WIDTH, height: PANEL_HEIGHT } = await resolvePanelDims();
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
        // Server-side generator — returns an ImageSlide and auto-appends to
        // the default playlist. Wrap with onSaveWithRefresh so the
        // generated slide appears in the Saved slides list immediately.
        onGenerateBackground: onSaveWithRefresh(generateBackground),
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

    mountSettings(root.querySelector(".settings-slot"), {
        fetchSettings: getSettings,
        onSave: saveSettings,
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
