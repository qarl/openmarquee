// openMarquee web UI — entry point.
//
// Six panels (slides/text, slides/image, slides/video, playlists,
// schedule, settings) mount into a sidebar shell. Sidebar nav
// (`nav.js`) toggles the active panel's `hidden` attribute; panels
// stay mounted so their state (scroll, in-progress edits, polling
// loops) survives navigation clicks.
//
// Panel dimensions come from /api/settings at boot AND whenever the
// Settings page emits an `openmarquee:settings-updated` event — the
// editor + uploader + playlist-track panels get re-mounted at the new
// dims so the canvas always matches the configured display. Existing
// stored slides keep their old-dim PNGs until re-saved (the playback
// loop NEAREST-upscales at runtime); that's expected, not a bug.
//
// Playback model: the backend playback loop is autonomous ("hardware
// always running"). The UI's inline preview on the Playlists panel is
// a parallel client-side simulator the operator scrubs — no UI
// affordance starts / stops the backend loop.

import {
    deleteContent,
    deletePlaylistByName,
    fetchContentItem,
    generateBackground,
    getSchedule,
    getSettings,
    listContent,
    listPlaylists,
    patchSlideDuration,
    saveImage,
    savePlaylistByName,
    saveSchedule,
    saveSettings,
    saveTextSlide,
    saveVideo,
    updateImage,
    updateTextSlide,
    updateVideo,
} from "./api.js";
import { mountEditor } from "./editor.js";
import { mountImageUploader } from "./image-upload.js";
import { mountInlinePreview } from "./inline-preview.js";
import { mountNav } from "./nav.js";
import {
    mountPlaylistBrowser,
    nextPlaylistName,
} from "./playlist-browser.js";
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

/**
 * Fetch the NAMED playlist with each item's ContentItem inlined — the
 * inline preview needs full item metadata (duration, type, auto_mode,
 * pipeline) to drive its client-side playback engine.
 */
// Unsaved draft of the currently-edited playlist. Set by the
// playlist-track's onDraftChange callback on every drag / transition
// change; cleared on Save. When present and matching `name`,
// fetchResolvedPlaylist uses it instead of hitting the API so the
// inline preview reflects the operator's in-progress state.
let playlistDraft = null;

async function fetchResolvedPlaylist(name) {
    const items = await listContent();
    const byId = new Map(items.map((it) => [String(it.id), it]));
    let raw;
    if (playlistDraft && playlistDraft.name === name) {
        raw = playlistDraft.entries;
    } else {
        const collection = await listPlaylists();
        raw = collection.playlists?.[name]?.items || [];
    }
    const resolved = raw
        .map((entry) => ({
            item_id: String(entry.item_id),
            transition: entry.transition || "cut",
            transition_ms: Number(entry.transition_ms) || 0,
            content: byId.get(String(entry.item_id)) || null,
        }))
        .filter((entry) => entry.content !== null);
    return { items: resolved };
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
    // Inline preview runs a requestAnimationFrame loop + caches
    // <img>/<video> elements; stop() before dropping its DOM.
    let inlinePreviewHandle = null;
    // Multi-playlist active-name state. Browser-select + new-create
    // update this; playlist-track reads it on each refresh via a
    // closure callable, and reorder / inline-preview fetches target
    // whatever name this holds at call time.
    let currentPlaylistName = "default";
    let playlistBrowserHandle = null;

    // Forward *all* args so both onSave(payload) and the two-arg
    // onSaveExisting(id, payload) work through the same wrapper. The
    // older single-arg signature dropped `payload` on edit calls, which
    // surfaced as a 422 ("body required") on every PUT.
    const onSaveWithRefresh = (saveFn) => async (...args) => {
        const saved = await saveFn(...args);
        if (playlistTrack) await playlistTrack.refresh();
        // Each slide subpage carries a horizontal browser at the top;
        // refresh all three so a just-saved slide shows up regardless
        // of where the save happened.
        await editor?.refreshBrowser?.();
        await imageUploader?.refreshBrowser?.();
        await videoUploader?.refreshBrowser?.();
        // The inline preview also caches the playlist; refresh it so
        // a newly-added slide shows up mid-session.
        await inlinePreviewHandle?.refresh?.();
        return saved;
    };

    /**
     * Mount (or re-mount) every panel that depends on display dims.
     * Called once at boot + again whenever Settings emits a change.
     */
    function mountDimensionedPanels({ width, height, outputMode }) {
        // CSS variable picked up by every tile thumbnail + preview
        // wrapper (.pallet-tile-thumb, .track-block-thumb-wrap,
        // .slide-browser-tile-thumb) so the thumbs match the device's
        // aspect ratio without each rule needing to be inline-styled.
        document.documentElement.style.setProperty(
            "--device-aspect",
            `${width} / ${height}`,
        );

        if (inlinePreviewHandle) {
            inlinePreviewHandle.stop();
            inlinePreviewHandle = null;
        }

        const trackSlot = root.querySelector(".playlist-track-slot");
        trackSlot.innerHTML = "";
        playlistTrack = mountPlaylistTrack(trackSlot, {
            fetchItems: listContent,
            fetchPlaylists: listPlaylists,
            // Explicit Save: drag/drop / transition / × / name-edit
            // mutate a draft; only the Save button triggers persistence.
            // Renames pivot via PUT-new + DELETE-old (the default
            // playlist is rename-locked at the UI level).
            onSavePlaylist: async ({ originalName, newName, entries }) => {
                const target = newName || originalName;
                await savePlaylistByName(target, entries);
                // Clear the draft — the server is now authoritative.
                playlistDraft = null;
                if (target !== originalName && originalName !== "default") {
                    try {
                        await deletePlaylistByName(originalName);
                    } catch (err) {
                        console.warn(
                            "[openmarquee] rename: old playlist delete failed (continuing):",
                            err,
                        );
                    }
                    currentPlaylistName = target;
                    await playlistBrowserHandle?.refresh();
                    playlistBrowserHandle?.highlight(target);
                }
                await inlinePreviewHandle?.refresh();
            },
            onDraftChange: async (draft) => {
                // Operator reordered / flipped a transition / renamed —
                // stash the draft so the preview pulls it instead of
                // the stale saved copy, then force a preview refresh.
                playlistDraft = draft;
                await inlinePreviewHandle?.refresh();
            },
            onUpdateDuration: async (id, ms) => {
                const result = await patchSlideDuration(id, ms);
                await inlinePreviewHandle?.refresh();
                return result;
            },
            getCurrentPlaylistName: () => currentPlaylistName,
            inlinePreview: {
                width,
                height,
                outputMode,
                mount: (slot, dims) => {
                    inlinePreviewHandle = mountInlinePreview(slot, {
                        width: dims.width,
                        height: dims.height,
                        outputMode: dims.outputMode,
                        fetchPlaylist: () =>
                            fetchResolvedPlaylist(currentPlaylistName),
                    });
                    return inlinePreviewHandle;
                },
            },
            playlistBrowser: {
                mount: (slot) => {
                    playlistBrowserHandle = mountPlaylistBrowser(slot, {
                        fetchPlaylists: listPlaylists,
                        fetchItems: listContent,
                        onSelect: async (name) => {
                            // Abandon any draft for the playlist we're
                            // switching away from — without this, the
                            // stale draft could resurface if the
                            // operator navigated back before saving.
                            playlistDraft = null;
                            currentPlaylistName = name;
                            playlistBrowserHandle.highlight(name);
                            await playlistTrack?.refresh();
                            await inlinePreviewHandle?.refresh();
                        },
                        onCreate: async () => {
                            playlistDraft = null;
                            const collection = await listPlaylists();
                            const names = Object.keys(
                                collection.playlists || {},
                            );
                            const newName = nextPlaylistName(names);
                            await savePlaylistByName(newName, []);
                            currentPlaylistName = newName;
                            await playlistBrowserHandle.refresh();
                            playlistBrowserHandle.highlight(newName);
                            await playlistTrack?.refresh();
                            await inlinePreviewHandle?.refresh();
                        },
                    });
                    playlistBrowserHandle.highlight(currentPlaylistName);
                    return playlistBrowserHandle;
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
        fetchSettings: getSettings,
    });

    mountSettings(root.querySelector(".settings-slot"), {
        fetchSettings: getSettings,
        onSave: saveSettings,
    });

    // Re-mount on settings change so the canvas always matches current
    // display config.
    document.addEventListener("openmarquee:settings-updated", async () => {
        mountDimensionedPanels(await resolvePanelDims());
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

    document.addEventListener("openmarquee:delete-slide", async (event) => {
        const { id, name } = event.detail || {};
        if (!id) return;
        const label = name || "this slide";
        if (!window.confirm(`Delete "${label}"? This can't be undone.`)) return;
        try {
            await deleteContent(id);
        } catch (err) {
            console.error("[openmarquee] delete failed:", err);
            window.alert(`Could not delete: ${err?.message || err}`);
            return;
        }
        // Refresh every surface that lists slides so the deleted tile
        // vanishes without a page reload.
        await Promise.all([
            playlistTrack?.refresh(),
            editor?.refreshBrowser?.(),
            imageUploader?.refreshBrowser?.(),
            videoUploader?.refreshBrowser?.(),
            inlinePreviewHandle?.refresh?.(),
        ]);
    });
    // Silence unused-var; `nav` is the mount's return value in case a
    // caller later wants to trigger navigation programmatically.
    void nav;
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}
