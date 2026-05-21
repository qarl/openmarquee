// openMarquee web UI — entry point.
//
// Five panels (slides, playlists, flock, schedule, settings) mount into
// a sidebar shell. Sidebar nav (`nav.js`) toggles the active panel's
// `hidden` attribute; panels stay mounted so their state (scroll,
// in-progress edits, polling loops) survives navigation clicks. The
// `slides` panel is itself a tabbed shell hosting text / image / video
// child panels — see slides.js.
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
    addFlockPeer,
    createPlaylist,
    deleteContent,
    deleteFlockPeer,
    deletePlaylistById,
    effectiveDisplayDims,
    fetchContentItem,
    generateBackground,
    getFlockDiscover,
    getSchedule,
    getSettings,
    getSystemInfo,
    listContent,
    listFlock,
    listPlaylists,
    patchSlideDuration,
    saveImage,
    savePlaylistById,
    saveSchedule,
    saveSettings,
    saveTextSlide,
    saveStream,
    saveVideo,
    updateFlockPeer,
    updateImage,
    updateStream,
    updateTextSlide,
    updateVideo,
} from "./api.js";
import { setSignTimezone } from "./auto-format.js";
import { DEFAULT_PLAYLIST_ID } from "./constants.js";
import { mountEditor } from "./editor.js";
import { mountImageUploader } from "./image-upload.js";
import { mountInlinePreview } from "./inline-preview.js";
import { mountNav } from "./nav.js";
import {
    mountPlaylistBrowser,
    nextPlaylistName,
} from "./playlist-browser.js";
import { mountPlaylistTrack } from "./playlist-track.js";
import { mountFirstRunWelcome } from "./first-run.js";
import { mountFlock } from "./flock.js";
import { Icons } from "./icons.js";
import { mountSchedule } from "./schedule.js";
import { mountSettings } from "./settings.js";
import { mountSlidesShell } from "./slides.js";
import { rerenderAllSlidesForRotation } from "./rotation-rerender.js";
import { mountStreamPanel } from "./stream-panel.js";
import { mountVideoUploader } from "./video-upload.js";
import { mountStreamUploader } from "./stream-upload.js";
import { initWasmRenderer, registerFont } from "./wasm-renderer.js";

// Fallback dims if /api/settings can't be reached — matches SYSTEM_SPEC
// §3.4 defaults so the editor at least renders something usable in an
// offline / broken-API scenario.
const FALLBACK_WIDTH = 128;
const FALLBACK_HEIGHT = 96;

const SECTIONS = [
    "slides",
    "playlists",
    "stream",
    "flock",
    "schedule",
    "settings",
];
const DEFAULT_SECTION = "slides";

async function resolvePanelDims() {
    try {
        const settings = await getSettings();
        // Parity follow-up (2026-05-20): publish the sign's
        // configured timezone app-wide so every auto_mode preview
        // (editor canvas, slide tiles, inline preview) resolves
        // its clock/date in the SAME zone the device renders in.
        // Runs before the editor mounts, so the first preview
        // frame already has the right zone.
        setSignTimezone(settings.timezone);
        const dims = effectiveDisplayDims(settings);
        const outputMode = settings.output_mode || "hdmi";
        // Schema default is 80 (settings.py:150-155). Coerce to int +
        // clamp to [0, 100] defensively so a malformed response can't
        // wash out the preview gamma pass.
        const brightnessRaw = Number(settings.brightness);
        const brightness = Number.isFinite(brightnessRaw)
            ? Math.max(0, Math.min(100, Math.round(brightnessRaw)))
            : 80;
        // FYS bug 7: rotation is carried alongside the effective dims so
        // the editor can lay out its saved asset.png in the panel's
        // installed orientation (rasterizeAtTarget swaps to portrait at
        // 90/270).
        const rotation = Number(settings.display_rotation || 0);
        if (dims !== null) {
            return {
                width: dims.width,
                height: dims.height,
                rotation,
                outputMode,
                brightness,
            };
        }
    } catch {
        // Fall through to fallback — editor still mounts even if the
        // settings endpoint is briefly unavailable on boot.
    }
    return {
        width: FALLBACK_WIDTH,
        height: FALLBACK_HEIGHT,
        rotation: 0,
        outputMode: "hdmi",
        brightness: 80,
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

async function fetchResolvedPlaylist(playlistId) {
    const items = await listContent();
    const byId = new Map(items.map((it) => [String(it.id), it]));
    let raw;
    if (playlistDraft && playlistDraft.playlistId === playlistId) {
        raw = playlistDraft.entries;
    } else {
        const collection = await listPlaylists();
        const target = (collection.playlists || []).find(
            (p) => String(p.id) === String(playlistId),
        );
        raw = target?.items || [];
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

    // 20.2 demo gap (caught by www-Jimmy on the 5e5dd97 deploy): the
    // demo bundle mounts the UI at /demo/, so a redirect to
    // "/login.html" lands on https://openmarquee.com/login.html
    // (root) which 404s. The demo never actually has a real backend
    // anyway -- its mock-backend.js stubs every /api/* response,
    // including /api/auth/* -- so the auth gate has nothing to enforce.
    // Skip the whole gate when the body carries data-demo-mode, the
    // same flag stream-panel uses for an analogous "skip the live-
    // backend round trip" carve-out.
    const isDemoMode =
        typeof document !== "undefined" &&
        document.body &&
        (document.body.hasAttribute("data-demo-mode") ||
            window.location.pathname.startsWith("/demo/"));

    // Batch 20.2 / phase A.2 — auth gate. Probe /api/auth/status to
    // figure out which boot route applies:
    //   - configured + valid token in localStorage → continue boot
    //   - configured + no token → redirect to /login.html
    //   - not configured → fall through. The first-run welcome card's
    //     onContinue handler kicks the operator to /set-password.html
    //     on first boot. If the operator already dismissed the welcome
    //     card AND their auth.json was wiped, they fall through into
    //     a broken editor; recovery is to manually visit
    //     /set-password.html. The intentionally simple semantics here
    //     keep every existing e2e spec working under
    //     OPENMARQUEE_DISABLE_AUTH=1 — auto-redirecting to set-password
    //     on every "auth not configured" load would break them all.
    // The auth probe is itself whitelisted by AuthMiddleware so it
    // works pre-set-password.
    let authStatus = null;
    if (!isDemoMode) {
        try {
            const r = await fetch("/api/auth/status");
            if (r.ok) authStatus = await r.json();
        } catch (err) {
            // /api/auth/status unreachable — degrade gracefully rather
            // than locking the operator out. The first-run + boot logic
            // below still runs against settings; the auth gate just
            // can't enforce here.
            console.warn("[boot] auth-status probe failed", err);
        }
        const hasToken =
            typeof localStorage !== "undefined" &&
            !!localStorage.getItem("openmarquee_auth_token");
        if (authStatus && authStatus.configured && !hasToken) {
            window.location.replace("/login.html");
            return;
        }
    }

    // First-run gate: a freshly-flashed device shows the welcome screen
    // until the operator dismisses it. The flag lives on
    // SystemSettings.ui_first_run_seen so it survives a power cycle.
    // Welcome is rendered INSIDE the new om-* shell (om-side + om-topbar
    // are still visible) — that's intentional, the operator immediately
    // sees the chrome they'll be using.
    try {
        const settings = await getSettings();
        if (!settings.ui_first_run_seen) {
            mountFirstRunWelcome(root, {
                signName: settings.sign_name || "this device",
                onContinue: async () => {
                    await saveSettings({
                        ...settings,
                        ui_first_run_seen: true,
                    });
                    // 20.2: route to set-password if auth isn't
                    // configured yet (first-boot path). If it IS
                    // configured by the time we get here, the
                    // auth-gate check above (which runs BEFORE this
                    // block) already kicked the operator to /login
                    // when they had no token. So a configured-status
                    // here implies the token is present too — fall
                    // through to the editor reload.
                    if (authStatus && !authStatus.configured) {
                        window.location.replace("/set-password.html");
                        return;
                    }
                    location.reload();
                },
            });
            return;
        }
    } catch (err) {
        // Settings unreachable — skip the gate so a borked /api/settings
        // doesn't lock the operator out of the editor entirely.
        console.warn("[boot] first-run check failed; skipping welcome", err);
    }

    root.innerHTML = `
        <section data-section="slides" class="panel">
            <div class="slides-shell-slot"></div>
            <div class="tab-pane" data-tab="text">
                <div class="editor-slot"></div>
            </div>
            <div class="tab-pane" data-tab="image" hidden>
                <div class="image-upload-slot"></div>
            </div>
            <div class="tab-pane" data-tab="video" hidden>
                <div class="video-upload-slot"></div>
            </div>
            <div class="tab-pane" data-tab="stream" hidden>
                <div class="stream-upload-slot"></div>
            </div>
        </section>
        <section data-section="playlists" class="panel">
            <div class="playlist-track-slot"></div>
        </section>
        <section data-section="stream" class="panel">
            <div class="stream-panel-slot"></div>
        </section>
        <section data-section="flock" class="panel">
            <div class="flock-slot"></div>
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
    let streamUploader = null;
    // Inline preview runs a requestAnimationFrame loop + caches
    // <img>/<video> elements; stop() before dropping its DOM.
    let inlinePreviewHandle = null;
    // Multi-playlist active-id state. Browser-select + new-create
    // update this; playlist-track reads it on each refresh via a
    // closure callable, and reorder / inline-preview fetches target
    // whatever id this holds at call time.
    let currentPlaylistId = DEFAULT_PLAYLIST_ID;
    let playlistBrowserHandle = null;

    // Slides shell — sub-tab switcher + +New button. Mounted once; the
    // child editor / image / video panels mount into the .tab-pane slots
    // separately (and re-mount on dim change without disturbing the shell).
    let slidesShell = null;

    // Sidebar count badges — totals next to the "Slides" and "Playlists"
    // nav entries. Decorative; failures are swallowed so a borked API
    // doesn't blank the chrome.
    async function refreshSidebarCounts() {
        try {
            const items = await listContent();
            const slot = document.querySelector("[data-slide-count]");
            if (slot) slot.textContent = String(items.length);
        } catch { /* leave last-known value */ }
        try {
            const collection = await listPlaylists();
            const count = (collection.playlists || []).length;
            const slot = document.querySelector("[data-playlist-count]");
            if (slot) slot.textContent = String(count);
        } catch { /* leave last-known value */ }
    }

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
        await streamUploader?.refreshBrowser?.();
        // Slides shell tab counts + sidebar totals.
        await slidesShell?.refreshCounts?.();
        await refreshSidebarCounts();
        // The inline preview also caches the playlist; refresh it so
        // a newly-added slide shows up mid-session.
        await inlinePreviewHandle?.refresh?.();
        return saved;
    };

    // Delete a playlist by id. Confirms first, then refreshes every
    // surface that lists playlists. If the deleted one was active, fall
    // back to the default.
    async function deletePlaylist(playlistId, displayName) {
        const label = displayName || "this playlist";
        if (!window.confirm(`Delete "${label}"? This can't be undone.`)) return;
        try {
            await deletePlaylistById(playlistId);
        } catch (err) {
            window.alert(`Could not delete: ${err?.message || err}`);
            return;
        }
        if (currentPlaylistId === playlistId) {
            currentPlaylistId = DEFAULT_PLAYLIST_ID;
            playlistDraft = null;
        }
        await playlistBrowserHandle?.refresh();
        playlistBrowserHandle?.highlight(currentPlaylistId);
        await playlistTrack?.refresh();
        await inlinePreviewHandle?.refresh();
        await refreshSidebarCounts();
    }

    // Shared playlist-creation flow — invoked by both the playlist
    // browser's create row and the playlist page-head's + New button.
    async function createNewPlaylist() {
        playlistDraft = null;
        const collection = await listPlaylists();
        const names = (collection.playlists || []).map((p) => p.name);
        const newName = nextPlaylistName(names);
        const created = await createPlaylist({ name: newName, entries: [] });
        currentPlaylistId = String(created.id);
        await playlistBrowserHandle?.refresh();
        playlistBrowserHandle?.highlight(currentPlaylistId);
        await playlistTrack?.refresh();
        await inlinePreviewHandle?.refresh();
        await refreshSidebarCounts();
    }

    /**
     * Mount (or re-mount) every panel that depends on display dims.
     * Called once at boot + again whenever Settings emits a change.
     */
    function mountDimensionedPanels({
        width,
        height,
        rotation = 0,
        outputMode,
        brightness,
    }) {
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
            // Auto-save: every drag/drop/transition/×/name-edit fires
            // through here. The id is stable across renames so a name
            // change is just a `name` field update — no PUT-new + DELETE-old
            // pivot needed (the old name-as-id era required that).
            onSavePlaylist: async ({ playlistId, name, entries }) => {
                await savePlaylistById(playlistId, { name, entries });
                playlistDraft = null;
                // Refresh the browser so the new name shows on the tile.
                await playlistBrowserHandle?.refresh();
                playlistBrowserHandle?.highlight(currentPlaylistId);
                await inlinePreviewHandle?.refresh();
                await refreshSidebarCounts();
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
            getCurrentPlaylistId: () => currentPlaylistId,
            inlinePreview: {
                width,
                height,
                outputMode,
                brightness,
                mount: (slot, dims) => {
                    inlinePreviewHandle = mountInlinePreview(slot, {
                        width: dims.width,
                        height: dims.height,
                        outputMode: dims.outputMode,
                        brightness: dims.brightness,
                        fetchPlaylist: () =>
                            fetchResolvedPlaylist(currentPlaylistId),
                    });
                    return inlinePreviewHandle;
                },
            },
            playlistBrowser: {
                mount: (slot) => {
                    playlistBrowserHandle = mountPlaylistBrowser(slot, {
                        fetchPlaylists: listPlaylists,
                        fetchItems: listContent,
                        onSelect: async (playlistId) => {
                            // Abandon any draft for the playlist we're
                            // switching away from — without this, the
                            // stale draft could resurface if the
                            // operator navigated back before saving.
                            playlistDraft = null;
                            currentPlaylistId = String(playlistId);
                            playlistBrowserHandle.highlight(currentPlaylistId);
                            await playlistTrack?.refresh();
                            await inlinePreviewHandle?.refresh();
                        },
                        onCreate: createNewPlaylist,
                        onDelete: deletePlaylist,
                    });
                    playlistBrowserHandle.highlight(currentPlaylistId);
                    return playlistBrowserHandle;
                },
            },
            // Page-head + New playlist button shares the create flow.
            onCreatePlaylist: createNewPlaylist,
            outputMode,
        });

        const editorSlot = root.querySelector(".editor-slot");
        editorSlot.innerHTML = "";
        editor = mountEditor(editorSlot, {
            width,
            height,
            rotation,
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

        // Stream slide editor — pure metadata, no canvas, so it
        // ignores width/height (mounted here alongside the others for
        // a consistent re-mount lifecycle).
        const streamSlot = root.querySelector(".stream-upload-slot");
        streamSlot.innerHTML = "";
        streamUploader = mountStreamUploader(streamSlot, {
            fetchItems: listContent,
            onSave: onSaveWithRefresh(saveStream),
            onSaveExisting: onSaveWithRefresh(updateStream),
        });
    }

    // Phase 1b parity fix (2026-05-14): init the fontdue-WASM
    // rasterizer + register every editor-selectable font BEFORE the
    // first mountDimensionedPanels call so paintLayer's wasm path is
    // ready on the first frame. Names must match the @font-face
    // family names in styles.css verbatim; paintLayer looks them up
    // via `isFontRegistered(fontFamily)`. Any font NOT in this list
    // falls back to ctx.fillText with a divergence-vs-Rust warning
    // in the parity gate.
    const WASM_FONTS = [
        ["Inter",                "/fonts/inter.ttf"],
        ["Oswald",               "/fonts/oswald.ttf"],
        ["Bebas Neue",           "/fonts/bebas-neue.ttf"],
        ["Roboto Slab",          "/fonts/roboto-slab.ttf"],
        ["Caveat Brush",         "/fonts/caveat-brush.ttf"],
        ["Permanent Marker",     "/fonts/permanent-marker.ttf"],
        ["Cinzel",               "/fonts/cinzel.ttf"],
        ["UnifrakturCook",       "/fonts/unifrakturcook.ttf"],
        ["Rye",                  "/fonts/rye.ttf"],
        ["Pacifico",             "/fonts/pacifico.ttf"],
        ["Sedgwick Ave Display", "/fonts/sedgwick-ave-display.ttf"],
        ["Bowlby One SC",        "/fonts/bowlby-one-sc.ttf"],
        ["Anton",                "/fonts/anton.ttf"],
        ["Archivo Black",        "/fonts/archivo-black.ttf"],
        ["Alfa Slab One",        "/fonts/alfa-slab-one.ttf"],
        ["Playfair Display",     "/fonts/playfair-display.ttf"],
        ["DM Serif Display",     "/fonts/dm-serif-display.ttf"],
        ["VT323",                "/fonts/vt323.ttf"],
        ["JetBrains Mono",       "/fonts/jetbrains-mono.ttf"],
        ["Space Mono",           "/fonts/space-mono.ttf"],
    ];
    try {
        await initWasmRenderer();
        await Promise.all(WASM_FONTS.map(([n, u]) => registerFont(n, u)));
    } catch (err) {
        // Don't block app boot on WASM failure — paintLayer falls
        // back to ctx.fillText when isFontRegistered returns false.
        // Log loud so a regression surfaces in dev console.
        console.error("[main] wasm-renderer init failed; falling back to fillText:", err);
    }

    // Initial mount.
    const _bootDims = await resolvePanelDims();
    mountDimensionedPanels(_bootDims);
    // FYS bug 7: track the rotation across settings-updated events so a
    // rotation change can trigger the stored-thumbnail bulk re-render.
    let lastKnownRotation = _bootDims.rotation || 0;

    // Slides shell — wraps the 3 child panels under one nav entry. Mounts
    // once: child panels live in stable .tab-pane slots that survive a
    // dim-change re-mount.
    const slidesSection = root.querySelector('[data-section="slides"]');
    slidesShell = mountSlidesShell(
        root.querySelector(".slides-shell-slot"),
        {
            fetchItems: listContent,
            tabPanesHost: slidesSection,
            // Text +New creates a server-side slide immediately so the
            // operator sees a fresh tile in the pallet right away.
            // Image / Video +New pops the file picker — empty
            // image/video slides need bytes before they exist.
            onAddNew: (tabKey) => {
                if (tabKey === "text") editor?.createNew?.();
                else if (tabKey === "image") imageUploader?.createNew?.();
                else if (tabKey === "video") videoUploader?.createNew?.();
                else if (tabKey === "stream") streamUploader?.createNew?.();
            },
        },
    );
    refreshSidebarCounts();

    // Schedule + settings don't depend on dims, so they mount once.
    mountSchedule(root.querySelector(".schedule-slot"), {
        fetchSchedule: getSchedule,
        onSave: saveSchedule,
        // Yields {id, name} pairs so the schedule UI's playlist <select>
        // shows display names but submits stable UUIDs.
        fetchPlaylistChoices: async () => {
            const collection = await listPlaylists();
            return (collection.playlists || []).map((p) => ({
                id: String(p.id),
                name: p.name,
            }));
        },
        fetchSettings: getSettings,
    });

    mountSettings(root.querySelector(".settings-slot"), {
        fetchSettings: getSettings,
        // Parity follow-up: re-publish the sign timezone after a
        // Settings save so the editor's auto_mode preview tracks a
        // timezone change without a page reload.
        onSave: async (s) => {
            const result = await saveSettings(s);
            setSignTimezone(s.timezone);
            return result;
        },
    });

    // Stream panel — phone-camera takeover (SYSTEM_SPEC §5.11). Doesn't
    // depend on display dims (the device-side renderer cover-fits the
    // remote video to whatever the active renderer's native size is),
    // so it mounts once alongside Settings + Schedule.
    //
    // The openmarquee.com/demo bundle has no real backend peer to
    // negotiate WebRTC against — `data-demo-mode` on <body> flips the
    // panel into a state-machine-only mode that still opens the local
    // camera + transitions through live, but skips the real PC creation
    // and the /api/stream/{start,stop,takeover} round trips. The
    // pathname check is belt-and-suspenders: if someone ever strips
    // data-demo-mode from the demo template, the panel still simulates
    // (instead of silently breaking with a setRemoteDescription throw).
    // 20.2 hotfix lifted the same check earlier in boot() to gate the
    // auth redirect; this is the SAME `isDemoMode` const, hoisted up
    // to that earlier declaration.
    mountStreamPanel(root.querySelector(".stream-panel-slot"), {
        simulateOnly: isDemoMode,
    });

    mountFlock(root.querySelector(".flock-slot"), {
        fetchFlock: listFlock,
        fetchSettings: getSettings,
        fetchSystemInfo: getSystemInfo,
        fetchDiscover: getFlockDiscover,
        onAdd: addFlockPeer,
        onUpdate: updateFlockPeer,
        onUpdateSelfSync: async (enabled) => {
            // Global flock_sync_enabled flag lives in SystemSettings.
            // PUT the whole object so we don't disturb fields we aren't
            // touching; merge on top of the current state.
            const current = await getSettings();
            await saveSettings({ ...current, flock_sync_enabled: enabled });
            document.dispatchEvent(
                new CustomEvent("openmarquee:settings-updated", {
                    detail: { settings: { ...current, flock_sync_enabled: enabled } },
                }),
            );
        },
        onDelete: deleteFlockPeer,
    });

    // Re-mount on settings change so the canvas always matches current
    // display config.
    document.addEventListener("openmarquee:settings-updated", async () => {
        const dims = await resolvePanelDims();
        mountDimensionedPanels(dims);
        refreshBrandSignName();
        // FYS bug 7: on a display-rotation change, every stored slide's
        // asset.png is now the wrong orientation for the reshaped
        // dashboard tiles. Bulk re-render them at the new rotation,
        // then re-mount so the slide browser picks up the fresh
        // thumbnails (their updated_at bump busts the <img> cache).
        const newRotation = dims.rotation || 0;
        if (newRotation !== lastKnownRotation) {
            lastKnownRotation = newRotation;
            try {
                const summary = await rerenderAllSlidesForRotation(newRotation);
                console.info("[bug7] rotation thumbnail re-render:", summary);
            } catch (err) {
                console.warn("[bug7] rotation thumbnail re-render failed:", err);
            }
            mountDimensionedPanels(dims);
        }
    });

    // Device name shown in the topbar (right of the wordmark). Refreshed
    // on boot + on settings-save so a rename in the Settings panel
    // shows up immediately.
    async function refreshBrandSignName() {
        const el = document.querySelector("[data-sign-name]");
        if (!el) return;
        try {
            const settings = await getSettings();
            el.textContent = settings.sign_name || "openMarquee";
        } catch {
            el.textContent = "openMarquee";
        }
    }
    refreshBrandSignName();

    // ─── Sidebar/topbar chrome populated dynamically ───────────────
    // The shell HTML in index.html has placeholder slots for icons,
    // peer dots, the broadcasting pill, and the section breadcrumb.
    // Populate each from its data source on boot, then keep the
    // Flock card + topbar pill in sync via a periodic poll.

    function paintIcon(slot, name, opts = {}) {
        if (!slot) return;
        const fn = Icons[name];
        if (!fn) return;
        slot.innerHTML = fn(opts);
    }
    // Sidebar icons (one per static row). data-icon names map to Icons keys.
    const ICON_MAP = {
        flock: "Flock",
        slides: "Slides",
        playlist: "Playlist",
        stream: "Stream",
        schedule: "Schedule",
        settings: "Settings",
        menu: "Menu",
    };
    for (const slot of document.querySelectorAll("[data-icon]")) {
        const key = slot.getAttribute("data-icon");
        const name = ICON_MAP[key];
        if (name) paintIcon(slot, name);
    }
    // The peer-count pill in the topbar — render the Flock icon at 11px.
    const peerPillIconSlot = document.querySelector(
        "[data-peer-pill] [data-icon]",
    );
    if (peerPillIconSlot) paintIcon(peerPillIconSlot, "Flock", { size: 11 });

    // Topbar breadcrumb: read the current route and convert to a label.
    // Updates on every hashchange via the existing nav so the title
    // tracks what the operator is looking at.
    // The `slides/*` keys still live here on purpose: we read the *raw*
    // hash (not the nav-resolved section), so deep-link URLs like
    // `#/slides/image` produce a tab-specific breadcrumb even though
    // they resolve to the parent `slides` section in nav.js.
    const SECTION_TITLES = {
        slides: "Slides",
        "slides/text": "Text slides",
        "slides/image": "Image slides",
        "slides/video": "Video slides",
        "slides/stream": "Streams",
        playlists: "Playlists",
        stream: "Stream",
        flock: "Flock",
        schedule: "Schedule",
        settings: "Settings",
    };
    function refreshSectionTitle() {
        const slot = document.querySelector("[data-section-title]");
        if (!slot) return;
        const hash = (window.location.hash || "").replace(/^#\/?/, "");
        slot.textContent = SECTION_TITLES[hash] || SECTION_TITLES.slides;
    }
    window.addEventListener("hashchange", refreshSectionTitle);
    refreshSectionTitle();

    // Mobile sheet nav. The hamburger button (.om-sheet-open) only
    // shows ≤760px (CSS media query); tapping it overlays the sidebar's
    // nav as a bottom-anchored sheet. Tapping any nav link or the
    // backdrop closes it. Re-uses the existing sidebar's DOM by
    // cloning into the sheet body so we don't maintain two nav lists.
    // The clone captures `.active` at open time; we also re-mirror on
    // hashchange so back/forward navigation updates the sheet too.
    const hamburger = document.querySelector(".om-sheet-open");
    if (hamburger) {
        let openSheet = null;
        const mirrorActive = () => {
            if (!openSheet) return;
            const orig = document.querySelectorAll(".om-side .nav-link");
            const cloned = openSheet.sheet.querySelectorAll(".nav-link");
            for (let i = 0; i < orig.length && i < cloned.length; i++) {
                const isActive = orig[i].classList.contains("active");
                cloned[i].classList.toggle("active", isActive);
                if (isActive) {
                    cloned[i].setAttribute("aria-current", "page");
                } else {
                    cloned[i].removeAttribute("aria-current");
                }
            }
        };
        const closeSheet = () => {
            if (!openSheet) return;
            window.removeEventListener("hashchange", mirrorActive);
            openSheet.back.remove();
            openSheet.sheet.remove();
            openSheet = null;
        };
        hamburger.addEventListener("click", () => {
            if (openSheet) {
                closeSheet();
                return;
            }
            const back = document.createElement("div");
            back.className = "om-sheet-back";
            back.addEventListener("click", closeSheet);
            const sheet = document.createElement("div");
            sheet.className = "om-sheet";
            sheet.innerHTML = `
                <div class="om-sheet-grip"></div>
                <div class="om-sheet-head">
                    <button type="button" class="om-btn ghost icon sm" aria-label="Close menu" data-icon="close"></button>
                </div>
                <div class="om-sheet-body"></div>
            `;
            // Clone the sidebar nav into the sheet so the same items show.
            const sideClone = document
                .querySelector(".om-side")
                .cloneNode(true);
            sheet.querySelector(".om-sheet-body").appendChild(sideClone);
            // Wire the close button + auto-close on any nav click.
            const closeBtn = sheet.querySelector("[aria-label='Close menu']");
            paintIcon(closeBtn.querySelector("[data-icon]"), "Close");
            closeBtn.addEventListener("click", closeSheet);
            sheet
                .querySelectorAll(".nav-link")
                .forEach((a) => a.addEventListener("click", closeSheet));
            document.body.appendChild(back);
            document.body.appendChild(sheet);
            openSheet = { back, sheet };
            window.addEventListener("hashchange", mirrorActive);
        });
    }

    // Flock card (sidebar) + peer pill (topbar). Two pieces of UI driven
    // by the same data — peers list with online flags. Poll every 8s
    // so the dots reflect reality even without a hashchange.
    async function refreshFlockChrome() {
        let peers = [];
        try {
            const data = await listFlock();
            peers = data?.peers || [];
        } catch {
            // On failure, keep last-known UI rather than blanking.
            return;
        }
        // Treat last_seen_at within ~30s as "online" for now.
        const now = Date.now();
        const onlinePeers = peers.filter(
            (p) =>
                p.last_seen_at &&
                now - new Date(p.last_seen_at).getTime() < 30_000,
        ).length;
        // Both tallies include self — the device serving this UI is
        // assumed online (it's right here, responding) and counts in
        // the flock's headcount. Aligns the sidebar's "X/Y online"
        // with the flock-page eyebrow's "X of Y signs online" — both
        // now treat self as a flock member, matching the "all peers
        // equal, no privileged home device" framing from the 2026-04-28
        // design adoption. Assumes this UI is rendered ON the device
        // it's counting; a future admin-console scenario where the UI
        // runs on something OTHER than a flock peer would need to
        // demote self from the +1.
        const onlineTotal = onlinePeers + 1;
        const peersTotal = peers.length + 1;
        const dots = document.querySelector("[data-flock-dots]");
        if (dots) {
            // Self dot first (always fresh); peers after.
            const peerDots = peers
                .map((p) => {
                    const fresh =
                        p.last_seen_at &&
                        now - new Date(p.last_seen_at).getTime() < 30_000;
                    return `<i class="${fresh ? "" : "off"}"></i>`;
                })
                .join("");
            dots.innerHTML = `<i></i>${peerDots}`;
        }
        const count = document.querySelector("[data-flock-count]");
        if (count) {
            count.textContent = `${onlineTotal}/${peersTotal} online`;
        }
        const pill = document.querySelector("[data-peer-pill-text]");
        if (pill) {
            pill.textContent = `${onlineTotal}/${peersTotal}`;
        }
    }
    refreshFlockChrome();
    setInterval(() => {
        if (document.visibilityState !== "hidden") refreshFlockChrome();
    }, 8000);
    // When the tab returns from background, refresh immediately so the
    // operator doesn't see stale dots while waiting for the next tick.
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") refreshFlockChrome();
    });

    const nav = mountNav({
        main: root,
        sidebar: document.querySelector(".om-side"),
        sections: SECTIONS,
        defaultSection: DEFAULT_SECTION,
    });

    // Click-to-edit wiring: playlist-track.js dispatches this event
    // when an operator clicks the ✎ affordance on a pallet tile. We
    // route by the slide's type to the right subpage + uploader /
    // editor. The route table closes over the mutable handles above
    // so it keeps working after a re-mount. The `slides/*` URL form
    // is intentional: nav.js prefix-walks it to the parent `slides`
    // section, AND the slides shell's hashchange listener picks the
    // correct tab from the same URL — one mutation, both effects.
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
        stream: {
            section: "slides/stream",
            load: (slide) => streamUploader.loadForEdit(slide),
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
            streamUploader?.refreshBrowser?.(),
            slidesShell?.refreshCounts?.(),
            refreshSidebarCounts(),
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
