// openMarquee web UI — entry point.
//
// Seven panels (slides/text, slides/image, slides/video, slides/auto,
// playlists, schedule, settings) mount into a sidebar shell. Sidebar
// nav (`nav.js`) toggles the active panel's `hidden` attribute; panels
// stay mounted so their state (scroll, in-progress edits, polling
// loops) survives navigation clicks.
//
// Panel dimensions come from /api/settings at boot so every preview
// canvas matches the device's configured display aspect ratio (128×96
// is just the SYSTEM_SPEC default; operators with a 1920×1080 HDMI
// target see 16:9 previews). Settings changes take effect on the
// next page load — re-mounting on save is a future refinement.

import {
    generateBackground,
    getPlaybackState,
    getSchedule,
    getSettings,
    listContent,
    listPlaylists,
    saveImage,
    saveSchedule,
    saveSettings,
    saveTextSlide,
    saveVideo,
    setPlaylistOrder,
    startPlayback,
    stopPlayback,
} from "./api.js";
import { mountAutoSlide } from "./auto-slide.js";
import { mountComposer } from "./composer.js";
import { mountEditor } from "./editor.js";
import { mountImageUploader } from "./image-upload.js";
import { mountNav } from "./nav.js";
import { mountPlaybackControls } from "./playback.js";
import { mountPlaylistTrack } from "./playlist-track.js";
import { mountSchedule } from "./schedule.js";
import { mountSettings } from "./settings.js";
import { mountVideoUploader } from "./video-upload.js";

// Fallback dims if /api/settings can't be reached — matches SYSTEM_SPEC
// §3.4 defaults so the editor at least renders something usable in an
// offline / broken-API scenario.
const FALLBACK_WIDTH = 128;
const FALLBACK_HEIGHT = 96;

const SECTIONS = [
    "slides/text",
    "slides/image",
    "slides/video",
    "slides/auto",
    "playlists",
    "schedule",
    "settings",
];
const DEFAULT_SECTION = "slides/text";

async function resolvePanelDims() {
    try {
        const settings = await getSettings();
        const w = Number(settings.display_width);
        const h = Number(settings.display_height);
        const rotation = Number(settings.display_rotation || 0);
        if (Number.isFinite(w) && w > 0 && Number.isFinite(h) && h > 0) {
            // 90° / 270° rotate the preview into portrait — swap dims so
            // the editor's aspect ratio matches what the installed sign
            // actually shows.
            if (rotation === 90 || rotation === 270) {
                return { width: h, height: w };
            }
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
        <section data-section="slides/text" class="panel">
            <div class="editor-slot"></div>
            <div class="composer-slot"></div>
        </section>
        <section data-section="slides/image" class="panel">
            <div class="image-upload-slot"></div>
        </section>
        <section data-section="slides/video" class="panel">
            <div class="video-upload-slot"></div>
        </section>
        <section data-section="slides/auto" class="panel">
            <div class="auto-slide-slot"></div>
        </section>
        <section data-section="playlists" class="panel">
            <div class="playlist-track-slot"></div>
        </section>
        <section data-section="schedule" class="panel">
            <div class="schedule-slot"></div>
        </section>
        <section data-section="settings" class="panel">
            <div class="settings-slot"></div>
        </section>
    `;

    // Playlist track handles its own refresh — any save returns and the
    // on-save callbacks below ping its refresh() so newly-created slides
    // appear in the pallet.
    const playlistTrack = mountPlaylistTrack(
        root.querySelector(".playlist-track-slot"),
        {
            fetchItems: listContent,
            fetchPlaylists: listPlaylists,
            onReorder: setPlaylistOrder,
            playback: {
                fetchState: getPlaybackState,
                onStart: startPlayback,
                onStop: stopPlayback,
            },
            mountPlaybackControls,
        },
    );

    const onSaveWithRefresh = (saveFn) => async (payload) => {
        const saved = await saveFn(payload);
        await playlistTrack.refresh();
        return saved;
    };

    mountEditor(root.querySelector(".editor-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        onSave: onSaveWithRefresh(saveTextSlide),
    });

    mountComposer(root.querySelector(".composer-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        fetchItems: listContent,
        // Composite slides ride the ImageSlide API — a flat PNG is all the
        // server needs. The layer structure lives only in the browser tab.
        onSave: onSaveWithRefresh(saveImage),
        // Server-side generator — returns an ImageSlide and auto-appends to
        // the default playlist.
        onGenerateBackground: onSaveWithRefresh(generateBackground),
    });

    mountImageUploader(root.querySelector(".image-upload-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        onSave: onSaveWithRefresh(saveImage),
    });

    mountVideoUploader(root.querySelector(".video-upload-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        onSave: onSaveWithRefresh(saveVideo),
    });

    mountAutoSlide(root.querySelector(".auto-slide-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
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
        defaultSection: DEFAULT_SECTION,
    });
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}
