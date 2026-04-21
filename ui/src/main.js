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
    fetchContentItem,
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
    updateTextSlide,
} from "./api.js";
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
        </section>
        <section data-section="slides/image" class="panel">
            <div class="image-upload-slot"></div>
        </section>
        <section data-section="slides/video" class="panel">
            <div class="video-upload-slot"></div>
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

    const editor = mountEditor(root.querySelector(".editor-slot"), {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        fetchItems: listContent,
        onSave: onSaveWithRefresh(saveTextSlide),
        onSaveExisting: onSaveWithRefresh(updateTextSlide),
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

    const nav = mountNav({
        main: root,
        sidebar: document.querySelector(".sidebar"),
        sections: SECTIONS,
        defaultSection: DEFAULT_SECTION,
    });

    // Click-to-edit wiring: playlist-track.js dispatches this event when
    // an operator clicks the ✎ affordance on a pallet tile. We navigate
    // to the Text subpage + hydrate the editor with the slide's data.
    document.addEventListener("openmarquee:edit-slide", async (event) => {
        const { id, type } = event.detail || {};
        if (type !== "text_slide") return;
        try {
            const slide = await fetchContentItem(id);
            window.location.hash = "#/slides/text";
            await editor.loadForEdit(slide);
        } catch (err) {
            // Editor surfaces its own status; console makes the failure
            // visible during development.
            console.error("[openmarquee] failed to open slide for edit:", err);
        }
    });
    // Silence unused-var; `nav` is the mount's return value in case a
    // caller later wants to trigger navigation programmatically.
    void nav;
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}
