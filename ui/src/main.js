// openMarquee web UI — entry point.
//
// Seven panels (slides/text, slides/image, slides/video, slides/auto,
// playlists, schedule, settings) mount into a sidebar shell. Sidebar
// nav (`nav.js`) toggles the active panel's `hidden` attribute; panels
// stay mounted so their state (scroll, in-progress edits, polling
// loops) survives navigation clicks.
//
// Panel dimensions come from /api/settings at boot AND whenever the
// Settings page emits an `openmarquee:settings-updated` event — the
// editor + uploader + playlist-track panels get re-mounted at the new
// dims so the canvas always matches the configured display. Existing
// stored slides keep their old-dim PNGs until re-saved (the playback
// loop NEAREST-upscales at runtime); that's expected, not a bug.

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
import { SETTINGS_BROADCAST_CHANNEL } from "./simulator.js";
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

    // Mutable across re-mounts. Keeping them in the outer scope lets
    // onSaveWithRefresh + the edit-slide route table reach the current
    // handles even after a settings-driven re-mount tears down the old
    // ones.
    let playlistTrack = null;
    let editor = null;
    let imageUploader = null;
    let videoUploader = null;
    // Live-preview runs a setInterval; we need to stop the old one
    // before dropping its DOM so we don't leak a poll loop against a
    // detached stage element.
    let livePreviewHandle = null;

    const onSaveWithRefresh = (saveFn) => async (payload) => {
        const saved = await saveFn(payload);
        if (playlistTrack) await playlistTrack.refresh();
        // Each slide subpage carries a horizontal browser at the top;
        // refresh all three so a just-saved slide shows up regardless
        // of where the save happened.
        await editor?.refreshBrowser?.();
        await imageUploader?.refreshBrowser?.();
        await videoUploader?.refreshBrowser?.();
        return saved;
    };

    /**
     * Mount (or re-mount) every panel that depends on display dims.
     * Called once at boot + again whenever Settings emits a change.
     */
    function mountDimensionedPanels({ width, height, outputMode }) {
        if (livePreviewHandle) {
            livePreviewHandle.stop();
            livePreviewHandle = null;
        }

        const trackSlot = root.querySelector(".playlist-track-slot");
        trackSlot.innerHTML = "";
        playlistTrack = mountPlaylistTrack(trackSlot, {
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
                width,
                height,
                mount: (slot, dims) => {
                    livePreviewHandle = mountLivePreview(slot, {
                        width: dims.width,
                        height: dims.height,
                        fetchState: getPlaybackState,
                    });
                    return livePreviewHandle;
                },
            },
            outputMode,
        });

        const editorSlot = root.querySelector(".editor-slot");
        editorSlot.innerHTML = "";
        editor = mountEditor(editorSlot, {
            width,
            height,
            fetchItems: listContent,
            onSave: onSaveWithRefresh(saveTextSlide),
            onSaveExisting: onSaveWithRefresh(updateTextSlide),
            // Free AI background generator (Pollinations.ai).
            // onSaveWithRefresh pings the track refresh so the newly-
            // generated ImageSlide appears in the pallet + the
            // editor's bg-slide dropdown on subsequent opens.
            onGenerateBackground: onSaveWithRefresh(generateBackground),
        });

        const imageSlot = root.querySelector(".image-upload-slot");
        imageSlot.innerHTML = "";
        imageUploader = mountImageUploader(imageSlot, {
            width,
            height,
            fetchItems: listContent,
            onSave: onSaveWithRefresh(saveImage),
            onSaveExisting: onSaveWithRefresh(updateImage),
        });

        const videoSlot = root.querySelector(".video-upload-slot");
        videoSlot.innerHTML = "";
        videoUploader = mountVideoUploader(videoSlot, {
            width,
            height,
            outputMode,
            fetchItems: listContent,
            onSave: onSaveWithRefresh(saveVideo),
            onSaveExisting: onSaveWithRefresh(updateVideo),
        });
    }

    // Initial mount.
    mountDimensionedPanels(await resolvePanelDims());

    // Schedule + settings don't depend on dims, so they mount once.
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

    // Re-mount on settings change so the canvas always matches current
    // display config. resolvePanelDims re-reads /api/settings — the
    // event's detail would let us skip that fetch, but re-reading
    // keeps the dim-derivation logic (rotation swap, fallback) in one
    // place. Broadcasting to the simulator pop-out (if open) lets it
    // re-apply skin + window sizing in lockstep.
    const settingsBroadcast =
        typeof BroadcastChannel !== "undefined"
            ? new BroadcastChannel(SETTINGS_BROADCAST_CHANNEL)
            : null;
    document.addEventListener("openmarquee:settings-updated", async () => {
        mountDimensionedPanels(await resolvePanelDims());
        if (settingsBroadcast) {
            settingsBroadcast.postMessage({ type: "settings-updated" });
        }
    });

    const nav = mountNav({
        main: root,
        sidebar: document.querySelector(".sidebar"),
        sections: SECTIONS,
        defaultSection: DEFAULT_SECTION,
    });

    // Click-to-edit wiring: playlist-track.js dispatches this event
    // when an operator clicks the ✎ affordance on a pallet tile. We
    // route by the slide's type to the right subpage + uploader /
    // editor. The route table closes over the mutable handles above
    // so it keeps working after a re-mount.
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
