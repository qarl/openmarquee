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
    updateImage,
    updateTextSlide,
    updateVideo,
} from "./api.js";
import { mountEditor } from "./editor.js";
import { mountImageUploader } from "./image-upload.js";
import { mountLivePreview } from "./live-preview.js";
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
        const outputMode = settings.output_mode || "hdmi";
        if (Number.isFinite(w) && w > 0 && Number.isFinite(h) && h > 0) {
            // 90° / 270° rotate the preview into portrait — swap dims so
            // the editor's aspect ratio matches what the installed sign
            // actually shows.
            if (rotation === 90 || rotation === 270) {
                return { width: h, height: w, outputMode };
            }
            return { width: w, height: h, outputMode };
        }
    } catch {
        // Fall through to fallback — editor still mounts even if the
        // settings endpoint is briefly unavailable on boot.
    }
    return {
        width: FALLBACK_WIDTH,
        height: FALLBACK_HEIGHT,
        outputMode: "hdmi",
    };
}

async function boot() {
    const {
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        outputMode: OUTPUT_MODE,
    } = await resolvePanelDims();
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
            livePreview: {
                width: PANEL_WIDTH,
                height: PANEL_HEIGHT,
                mount: (slot, { width, height }) =>
                    mountLivePreview(slot, {
                        width,
                        height,
                        fetchState: getPlaybackState,
                    }),
            },
            outputMode: OUTPUT_MODE,
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
        // Free AI background generator (Pollinations.ai). onSaveWithRefresh
        // pings the track refresh so the newly-generated ImageSlide
        // appears in the pallet + the editor's bg-slide dropdown on
        // subsequent opens.
        onGenerateBackground: onSaveWithRefresh(generateBackground),
    });

    const imageUploader = mountImageUploader(
        root.querySelector(".image-upload-slot"),
        {
            width: PANEL_WIDTH,
            height: PANEL_HEIGHT,
            onSave: onSaveWithRefresh(saveImage),
            onSaveExisting: onSaveWithRefresh(updateImage),
        },
    );

    const videoUploader = mountVideoUploader(
        root.querySelector(".video-upload-slot"),
        {
            width: PANEL_WIDTH,
            height: PANEL_HEIGHT,
            outputMode: OUTPUT_MODE,
            onSave: onSaveWithRefresh(saveVideo),
            onSaveExisting: onSaveWithRefresh(updateVideo),
        },
    );

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
    // an operator clicks the ✎ affordance on a pallet tile. We route by
    // the slide's type to the right subpage + uploader / editor.
    const EDIT_ROUTES = {
        text_slide: {
            section: "slides/text",
            load: (slide) => editor.loadForEdit(slide),
        },
        image: {
            section: "slides/image",
            load: (slide) => imageUploader.loadForEdit(slide),
        },
        video: {
            section: "slides/video",
            load: (slide) => videoUploader.loadForEdit(slide),
        },
    };
    document.addEventListener("openmarquee:edit-slide", async (event) => {
        const { id, type } = event.detail || {};
        const route = EDIT_ROUTES[type];
        if (!route) return;
        try {
            const slide = await fetchContentItem(id);
            window.location.hash = `#/${route.section}`;
            await route.load(slide);
        } catch (err) {
            // Each uploader/editor surfaces its own status line; console
            // makes the failure visible during development.
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
