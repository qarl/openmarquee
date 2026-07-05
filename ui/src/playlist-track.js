// Playlist page: vertical stack of track blocks for the active playlist +
// a pallet of every saved slide on the right. Drag within the track to
// reorder; drag from the pallet onto the track to add; click × on a track
// block to remove.
//
// qarl's spec: single default playlist (named playlists are hidden for
// now), slide duration shown on the bottom of each track block, pallet
// = all available content.
//
// Backend still supports the multi-playlist collection (schedule rules
// ref by name) — this UI just pins to the "default" playlist. Named
// playlists can come back via a later commit if the schedule UX needs
// them visible.

// Batch 7.3: sortablejs is the largest dep in the playlist bundle
// (~36 KB minified). Loaded lazily inside refresh() so the cold
// path that doesn't open the Playlists panel doesn't pay for it.

import { contentTileThumbnailAttrs } from "./api.js";
import { attachAutoSave } from "./auto-save.js";
import { attachAutoTextOverlay } from "./auto-text-overlay.js";
import { DEFAULT_PLAYLIST_ID } from "./constants.js";
import { markEnd, markStart } from "./perf.js";

// Per-block transition selector options. The pulldown UX replaced the
// prior cycle-button per the 2026-04-28 design batch; this list is the
// single source of truth for both the renderer (renders <option>s in
// order) and downstream additions (new transitions land here as their
// renderers ship — server-side in playback.py + browser-side in
// inline-preview.js — atomically with their tokens, so we never list
// a transition that secretly falls back to fade).
const TRANSITION_OPTIONS = [
    { value: "cut", label: "cut" },
    { value: "fade", label: "fade" },
    { value: "wipe", label: "wipe" },
    { value: "slide", label: "slide" },
    { value: "iris", label: "iris" },
    { value: "scroll", label: "scroll" },
    { value: "flip", label: "flip" },
    { value: "marquee", label: "marquee" },
    { value: "dissolve", label: "dissolve" },
    { value: "pixelate", label: "pixelate" },
    { value: "halftone", label: "halftone" },
    { value: "scanline", label: "scanline" },
    { value: "glitch", label: "glitch" },
    { value: "push", label: "push" },
    { value: "blinds", label: "blinds" },
    { value: "shutter", label: "shutter" },
];

function transitionOptionsHTML(selected) {
    // escapeHtml is defined further down (function decl, hoisted) and
    // attribute-safe — handles `&`, `<`, `>`, `"` identically to what a
    // separate attr-escaper would.
    return TRANSITION_OPTIONS.map(
        (opt) =>
            `<option value="${escapeHtml(opt.value)}"${opt.value === selected ? " selected" : ""}>${escapeHtml(opt.label)}</option>`,
    ).join("");
}

// r52: format the chip label so the dense scannable row shows BOTH the
// transition kind and (for animated kinds) its duration. "cut" has no
// duration semantically — show just the kind. Others render "kind · 500ms"
// or "kind · 1.0s" (the latter once we cross 1000ms, to keep the chip
// compact). Returns a plain string; caller is responsible for escaping
// before innerHTML use.
function transitionChipLabel(kind, ms) {
    const k = String(kind || "cut");
    if (k === "cut") return "cut";
    const n = Math.max(0, Math.round(Number(ms) || 0));
    if (n === 0) return k;
    if (n >= 1000) {
        // Drop the trailing ".0" — "1s" reads better than "1.0s" but we keep
        // one decimal for fractional values like "1.5s".
        const sec = n / 1000;
        const formatted = Number.isInteger(sec) ? String(sec) : sec.toFixed(1);
        return `${k} · ${formatted}s`;
    }
    return `${k} · ${n}ms`;
}

// r52: clamp arbitrary user input (string from <input type="number">) to
// the Pydantic field constraint (0-5000 ms, integer). Returns 0 on
// non-finite/negative.
function clampTransitionMs(value) {
    const n = Math.round(Number(value));
    if (!Number.isFinite(n)) return 0;
    return Math.max(0, Math.min(5000, n));
}

// Mode-locking went away when videos became resolution-independent H.264
// MP4s that every renderer can consume. Keep the signature so existing
// callers don't explode; always return false.
function isModeLockedVideo() {
    return false;
}

// The default playlist's name is fixed (the rename API rejects it).
// Disabling the input alone left operators wondering if the field was
// broken; surface the reason via title + a descriptive aria-label, and
// clear them when switching back to a renamable playlist so stale
// attributes don't linger. Both refresh()'s memo-fast-path and slow-path
// call this so the hints stay in sync regardless of which branch ran.
function applyDefaultPlaylistNameHints(nameEl, activeName) {
    const isDefault = activeName === "default";
    nameEl.disabled = isDefault;
    if (isDefault) {
        nameEl.title = "Cannot rename the default playlist.";
        nameEl.setAttribute("aria-label", "Default playlist (immutable)");
    } else {
        nameEl.title = "";
        nameEl.removeAttribute("aria-label");
    }
}

const TEMPLATE = `
    <section class="playlist-track">
        <div class="om-page-head">
            <div>
                <span class="om-eyebrow" data-playlist-stats>Loops · drag to reorder</span>
                <h1>Playlists</h1>
                <p>Drag blocks to reorder. Drop slides from the pallet at the bottom. The current loop runs end-to-end on the device.</p>
            </div>
            <div class="om-page-head-actions">
                <button type="button" class="om-btn primary playlist-new-btn">+ New playlist</button>
            </div>
        </div>
        <div class="playlist-browser-slot"></div>
        <div class="playlist-cols">
            <div class="playlist-track-inline-preview"></div>

            <div class="playlist-form-stack">
                <div class="om-card" style="margin-bottom: 12px;">
                    <label class="om-field">
                        <span>Playlist name</span>
                        <input type="text" class="om-input field-playlist-name" maxlength="64" pattern="[a-z0-9_-]+">
                    </label>
                </div>

                <div class="om-card om-card-tight playlist-track-scroll" role="region" aria-label="playlist timeline" style="padding: 12px;">
                    <ul class="playlist-track-list" role="list" data-empty-hint="Drag slides from the pallet below to build your playlist."></ul>
                </div>

                <div class="om-pallet" style="margin-top: 14px;">
                    <div class="om-pallet-head">
                        <h3>Pallet · saved slides</h3>
                    </div>
                    <ul class="playlist-pallet" role="list"></ul>
                </div>

                <p class="om-save-status playlist-track-status" role="status" aria-live="polite" data-state="idle"></p>
            </div>
        </div>
    </section>
`;

/**
 * Mount the single-playlist track editor into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {() => Promise<Array>} options.fetchItems — all content items
 * @param {() => Promise<object>} options.fetchPlaylists — { playlists: { default: {items, item_ids}, ... } }
 * @param {(items: Array<{item_id, transition, transition_ms}>) => Promise<any>} options.onReorder
 *     — PUT /api/playlists/{id} with the canonical items shape
 * @param {object} [options.inlinePreview] — optional injection:
 *     `{ width, height, outputMode, mount(slot, dims) }`. When set, the
 *     mount is called once right under the header and is expected to
 *     render its own transport controls (play / scrub / time).
 * @param {string} [options.outputMode] — current device output_mode
 *     ("hdmi" / "hub75" / "ws281x" / "composite"). Videos whose stored
 *     pipeline doesn't match the mode get a mode-locked badge on their
 *     pallet + track tiles and won't play until re-uploaded.
 * @returns {{ refresh: () => Promise<void> }}
 */
export function mountPlaylistTrack(container, options) {
    const {
        fetchItems,
        fetchPlaylists,
        onSavePlaylist,
        onUpdateDuration,
        inlinePreview,
        outputMode,
        getCurrentPlaylistId,
        playlistBrowser,
        // Fires on every drag / transition / name change with the
        // current in-memory draft. Used by the inline preview to render
        // unsaved state — otherwise the preview lags the operator's
        // edits by one Save click.
        onDraftChange,
        // + New playlist button (page-head). Optional — falls back to a
        // no-op so callers that don't want the affordance can omit.
        onCreatePlaylist,
    } = options;
    // Fallback when the caller doesn't want multi-playlist — always
    // operate on the well-known DEFAULT_PLAYLIST_ID.
    const resolveId = getCurrentPlaylistId || (() => DEFAULT_PLAYLIST_ID);

    container.innerHTML = TEMPLATE;
    const trackEl = container.querySelector(".playlist-track-list");
    const palletEl = container.querySelector(".playlist-pallet");
    const statusEl = container.querySelector(".playlist-track-status");
    const nameEl = container.querySelector(".field-playlist-name");
    const newBtn = container.querySelector(".playlist-new-btn");
    if (newBtn) {
        newBtn.addEventListener("click", () => onCreatePlaylist?.());
    }
    // Heading element is gone from the template (the name input above
    // doubles as the playlist label). Keep the lookup for backward
    // compat: any caller that still injects a `[data-field="heading"]`
    // gets it updated; otherwise we no-op.
    const headingEl = container.querySelector('[data-field="heading"]');
    const inlinePreviewSlot = container.querySelector(
        ".playlist-track-inline-preview",
    );
    const playlistBrowserSlot = container.querySelector(
        ".playlist-browser-slot",
    );

    if (inlinePreview && inlinePreview.mount) {
        inlinePreview.mount(inlinePreviewSlot, {
            width: inlinePreview.width,
            height: inlinePreview.height,
            outputMode: inlinePreview.outputMode,
        });
    }

    if (playlistBrowser && playlistBrowser.mount) {
        playlistBrowser.mount(playlistBrowserSlot);
    }

    let trackSortable = null;
    let palletSortable = null;
    // Generation guard: bindTrackSortable + bindPalletSortable await a
    // lazy `import("sortablejs")` (~30-100ms cold). If a second refresh()
    // fires while the first is still awaiting, both refreshes hit the
    // pre-await destroy(), then BOTH assign new instances to the module-
    // scoped trackSortable/palletSortable; the earlier pair dangles
    // without a destroy(). Same shape as editor.js's sortableBindGeneration
    // (L1185-1205). Bumped at the start of every slow-path refresh; the
    // post-await assignment bails if a newer refresh has incremented it.
    let sortableBindGeneration = 0;
    // Bumped on every refresh() so tile thumbnail URLs force a refetch
    // (edits don't change created_at, so the HTTP cache would otherwise
    // serve stale bytes after an in-place save).
    let refreshVersion = 0;
    // Memoization key for diff-based refresh (Batch 8.3). When the
    // playlist + content snapshot hasn't changed since the last
    // refresh, the entire wipe + rebuild + Sortable-recreate cycle
    // is a no-op. The key encodes everything that affects rendered
    // chrome: playlist entries (id+transition+transition_ms order),
    // content items (id+duration_ms+updated_at), and the active
    // playlist name. Auto-save fires refresh() every 600ms in some
    // edit flows; this lets unchanged-state polls short-circuit.
    let lastRefreshKey = null;
    // Closure-scoped lookup so the track Sortable's `onEnd` can re-skin a
    // cross-list drop (pallet → track) from the Sortable-clone's default
    // `.pallet-tile` shape into a proper `.track-block` *before* waiting
    // on the server round-trip. Refreshed on every refresh() call.
    let itemByIdRef = new Map();

    async function performSave() {
        const entries = collectTrackEntries(trackEl);
        const name = (nameEl.value || "").trim();
        const playlistId = resolveId();
        // Renames are safe — the id is immutable, so any schedule rule
        // referencing this playlist keeps working across name edits.
        await onSavePlaylist({
            playlistId,
            name,
            entries,
        });
    }

    // Drag-and-drop fires no `input` / `change` event on the form, so we
    // attach the helper without an auto-wire form and trigger via kick().
    // Name typing IS captured by the input listener below.
    const autoSave = attachAutoSave(null, {
        save: performSave,
        status: statusEl,
        debounceMs: 400,
    });

    function markDirty() {
        autoSave.kick();
    }
    /**
     * Like markDirty but ALSO notifies the host (main.js) so the inline
     * preview re-renders against the in-progress draft. Use this for
     * changes that affect the rendered sequence (drag, transition,
     * remove) — NOT the name input, which the preview doesn't care
     * about and would otherwise trigger a fetch per keystroke.
     */
    function markContentDirty() {
        markDirty();
        if (onDraftChange) {
            try {
                onDraftChange({
                    playlistId: resolveId(),
                    entries: collectTrackEntries(trackEl),
                });
            } catch (err) {
                console.error("[playlist-track] onDraftChange failed:", err);
            }
        }
    }

    nameEl.addEventListener("input", markDirty);

    function bindAddedBlockButtons() {
        // After a drag-add or reorder, fresh DOM nodes may not have
        // their click handlers wired yet. Re-bind everything; the
        // initial-render path uses these same fns. Content-mutating
        // actions (remove × + transition chip) use markContentDirty
        // so the inline preview re-renders against the draft.
        bindTrackRemoveButtons(trackEl, markContentDirty);
        bindTrackDurationButtons(trackEl, onUpdateDuration, refresh, statusEl);
    }

    async function refresh() {
        markStart("playlist-track.refresh");
        statusEl.textContent = "";
        try {
            const [items, collection] = await Promise.all([
                fetchItems(),
                fetchPlaylists(),
            ]);
            itemByIdRef = new Map(items.map((it) => [String(it.id), it]));
            const itemById = itemByIdRef;
            const activeId = resolveId();
            // v4 collection: `playlists` is a list of {id, name, items}.
            const active = (collection.playlists || []).find(
                (p) => String(p.id) === String(activeId),
            );
            const activeName = active?.name || "default";
            const playlistRaw = active?.items;
            const defaultEntries = Array.isArray(playlistRaw)
                ? playlistRaw.map((e) => ({
                      item_id: String(e.item_id),
                      transition: e.transition || "cut",
                      transition_ms: Number(e.transition_ms) || 500,
                  }))
                : (active?.item_ids || []).map((id) => ({
                      item_id: String(id),
                      transition: "cut",
                      transition_ms: 500,
                  }));

            // Batch 8.3 memoization: compute a fingerprint covering
            // everything that affects rendered chrome. If unchanged
            // since last refresh AND the track has content, skip the
            // wipe-and-rebuild. Auto-save can fire refresh() rapidly
            // while the operator types in an unrelated field; this
            // short-circuits those redundant ticks.
            const entriesKey = defaultEntries
                .map((e) => `${e.item_id}/${e.transition}/${e.transition_ms}`)
                .join(",");
            const itemsKey = items
                .map((i) => `${i.id}/${i.duration_ms}/${i.updated_at || ""}`)
                .join(",");
            // Sibling-playlist count goes into the eyebrow stats
            // line; key on the full playlists list so a non-active
            // playlist add/delete invalidates the memo.
            const playlistsKey = (collection.playlists || [])
                .map((p) => String(p.id))
                .join(",");
            const refreshKey = `${activeName}|${entriesKey}|${itemsKey}|${playlistsKey}`;
            if (
                refreshKey === lastRefreshKey
                && trackSortable !== null
                && palletSortable !== null
            ) {
                // Nothing observable changed; keep the existing DOM +
                // Sortable instances intact. autoSave + statusEl reset
                // still need to happen (refresh is a "loaded fresh from
                // server" reset signal, not just a render trigger).
                // Guard the name-field clobber + autosave-cancel when
                // the operator is mid-edit on the name input — an
                // external refresh in the 400ms debounce window would
                // otherwise wipe in-flight keystrokes AND drop the
                // pending save. applyDefaultPlaylistNameHints stays
                // unconditional (only matters when activeName=='default'
                // which makes the input disabled — operator can't
                // realistically be editing in that case).
                if (document.activeElement !== nameEl) {
                    nameEl.value = activeName;
                    autoSave.cancel();
                }
                applyDefaultPlaylistNameHints(nameEl, activeName);
                statusEl.dataset.state = "idle";
                return;
            }
            lastRefreshKey = refreshKey;
            refreshVersion += 1;
            if (headingEl) {
                headingEl.textContent =
                    activeName === "default" ? "Default playlist" : activeName;
            }
            // Destroy existing Sortables BEFORE re-rendering tiles.
            // Sortable.js `destroy()` strips the `draggable` attribute
            // from every child it managed — doing this AFTER rendering
            // the new tiles would wipe their draggable=false and let
            // native <img> drag preempt Sortable.
            if (trackSortable) { trackSortable.destroy(); trackSortable = null; }
            if (palletSortable) { palletSortable.destroy(); palletSortable = null; }

            trackEl.innerHTML = "";
            for (const entry of defaultEntries) {
                const item = itemById.get(entry.item_id);
                if (!item) continue; // stale ref — skip
                trackEl.appendChild(
                    renderTrackBlock(item, entry, { locked: false, cacheBust: refreshVersion }),
                );
            }
            // Wire × + transition buttons → mark content dirty (no
            // save until the operator clicks Save playlist; preview
            // re-renders against the draft via onDraftChange).
            bindTrackRemoveButtons(trackEl, markContentDirty);
            // Duration is a SLIDE attribute — auto-saves immediately
            // outside the playlist's draft flow.
            bindTrackDurationButtons(
                trackEl, onUpdateDuration, refresh, statusEl,
            );

            // Sync the name input to the freshly-loaded playlist. Refresh
            // is not an edit, so cancel any pending auto-save and clear
            // the status pill. Guard both clobber-name + cancel when
            // the operator is mid-edit on the name input; see the
            // memo-hit branch above for the same reasoning.
            if (document.activeElement !== nameEl) {
                nameEl.value = activeName;
                autoSave.cancel();
            }
            applyDefaultPlaylistNameHints(nameEl, activeName);
            statusEl.textContent = "";
            statusEl.dataset.state = "idle";

            palletEl.innerHTML = "";
            for (const item of items) {
                palletEl.appendChild(
                    renderPalletTile(item, { locked: false, cacheBust: refreshVersion }),
                );
            }

            // Lazy-import sortablejs; the two binders await it. Parallel
            // since they're independent. Generation guard (see declaration
            // comment above) handles the race where a second refresh()
            // fires while we're awaiting these — without it, the loser's
            // Sortable pair dangles without a destroy().
            const myGeneration = ++sortableBindGeneration;
            const [newTrack, newPallet] = await Promise.all([
                bindTrackSortable(
                    trackEl,
                    markContentDirty,
                    itemByIdRef,
                    bindAddedBlockButtons,
                    () => refreshVersion,
                ),
                bindPalletSortable(palletEl),
            ]);
            if (myGeneration !== sortableBindGeneration) {
                // A newer refresh started while we were awaiting. Destroy
                // what we created so we don't leak; the winner has its own
                // pair queued up.
                newTrack?.destroy();
                newPallet?.destroy();
                return;
            }
            [trackSortable, palletSortable] = [newTrack, newPallet];

            // Eyebrow stats: total playlists, then this loop's block count + duration.
            // Inside the gated branch so a stale refresh can't clobber the
            // winner's stats either.
            const statsEl = container.querySelector("[data-playlist-stats]");
            if (statsEl) {
                const playlistCount = (collection.playlists || []).length;
                const blockCount = defaultEntries.length;
                const loopMs = defaultEntries.reduce((acc, entry) => {
                    const it = itemById.get(entry.item_id);
                    return acc + (Number(it?.duration_ms) || 5000);
                }, 0);
                const loopSec = Math.round(loopMs / 1000);
                statsEl.textContent = `${playlistCount} playlist${playlistCount === 1 ? "" : "s"} · this loop ${blockCount} block${blockCount === 1 ? "" : "s"} · ${loopSec}s`;
            }
        } catch (err) {
            statusEl.textContent = `Could not load playlist: ${err.message}`;
        } finally {
            markEnd("playlist-track.refresh");
        }
    }

    refresh();
    return {
        refresh,
        flushAutoSave: () => autoSave.flush(),
    };
}

async function bindTrackSortable(trackEl, markDirty, itemByIdRef, rebindButtons, getCacheBust) {
    const { default: Sortable } = await import("sortablejs");
    return Sortable.create(trackEl, {
        group: { name: "playlist-track", pull: true, put: ["playlist-pallet"] },
        animation: 150,
        ghostClass: "track-ghost",
        filter: ".track-remove, .track-block-transition, .track-block-duration",
        preventOnFilter: false,
        onAdd: (evt) => {
            // Cross-list drop from the pallet → Sortable cloned a
            // `.pallet-tile` into the track. Re-skin in place to the
            // proper `.track-block` shape (with duration label + default
            // transition chrome) so the operator sees correct chrome
            // *immediately*.
            const dropped = evt.item;
            const id = dropped?.dataset?.id;
            // Defensive: malformed pallet tile with no dataset.id.
            // Shouldn't happen — but if it does, remove the orphan
            // before bailing so it doesn't sit unstyled in the track.
            if (!id) {
                dropped?.remove();
                return;
            }
            // Race: pallet still shows a tile for an item another
            // session just deleted. Pallet's itemByIdRef is stale until
            // the next refresh fires; meanwhile the operator dropped
            // it. Without remove(), the orphan would sit as a bare
            // .pallet-tile-shaped element in the track (no controls,
            // confusing but harmless — collectTrackEntries filters on
            // .track-block[data-id] so saves stay clean). Silent
            // remove: bindTrackSortable doesn't have statusEl in scope
            // and threading it through would be scope creep here.
            const item = itemByIdRef.get(id);
            if (!item) {
                dropped.remove();
                return;
            }
            const rebuilt = renderTrackBlock(
                item,
                { item_id: id, transition: "cut", transition_ms: 500 },
                { cacheBust: getCacheBust ? getCacheBust() : 0 },
            );
            dropped.replaceWith(rebuilt);
            // Sortable's `end` event dispatches against the SOURCE list of
            // the drag. For a pallet → track drop, that's the pallet
            // Sortable (which doesn't configure onEnd), so the track's
            // onEnd never fires and the new block's × / transition chip /
            // duration controls would stay unbound until a remount. Wire
            // them here. bindTrackRemoveButtons is data-bound-guarded, so
            // overlap with onEnd's rebindButtons() on same-list reorders
            // is a no-op.
            if (rebindButtons) rebindButtons();
            markDirty();
        },
        onEnd: () => {
            // Same-list reorder inside the track. Re-wire any newly-revealed
            // blocks (idempotent via the data-bound guard) and dirty the
            // playlist so Save lights up.
            if (rebindButtons) rebindButtons();
            markDirty();
        },
    });
}

async function bindPalletSortable(palletEl) {
    const { default: Sortable } = await import("sortablejs");
    return Sortable.create(palletEl, {
        // Dragging out of the pallet creates a clone (pallet tile stays put)
        // so the same slide can be added to the track multiple times.
        group: {
            name: "playlist-pallet",
            pull: "clone",
            put: false,
        },
        sort: false,
        ghostClass: "pallet-ghost",
        // Clicks on the edit / delete buttons must not initiate a drag —
        // otherwise Sortable intercepts the pointerdown and the button's
        // click handler never fires.
        filter: ".pallet-tile-edit, .pallet-tile-delete",
        preventOnFilter: false,
    });
}

function collectTrackIds(trackEl) {
    return Array.from(trackEl.querySelectorAll("[data-id]")).map(
        (el) => el.dataset.id,
    );
}

// The canonical-shape reader for save calls: returns the full
// `{item_id, transition, transition_ms}` tuple per track block in DOM
// order, so PUT /api/playlists/{id} can round-trip transitions too.
function collectTrackEntries(trackEl) {
    return Array.from(trackEl.querySelectorAll(".track-block[data-id]")).map(
        (li) => ({
            item_id: li.dataset.id,
            transition: li.dataset.transition || "cut",
            transition_ms: Number(li.dataset.transitionMs) || 500,
        }),
    );
}

function bindTrackRemoveButtons(trackEl, markDirty) {
    for (const btn of trackEl.querySelectorAll(".track-remove")) {
        // Idempotent re-binding: skip if already wired (we may be
        // called multiple times after drag-adds).
        if (btn.dataset.bound === "1") continue;
        btn.dataset.bound = "1";
        btn.addEventListener("click", () => {
            const block = btn.closest("[data-id]");
            if (!block) return;
            block.remove();
            markDirty();
        });
    }
    // r52: transition chip is a click-to-open popover (kind + duration).
    // Cut-coupling logic:
    //   - kind=cut FORCES ms=0 + disables the ms input
    //   - if operator changes kind from cut → other, restore the
    //     remembered "lastNonZeroMs" (or 500 default) so the duration
    //     control isn't suddenly a no-op
    //   - if operator types non-zero ms while kind=cut, auto-switch
    //     kind to the remembered "lastNonCutKind" (default "fade")
    // Source of truth stays the block's dataset (transition + transitionMs);
    // collectTrackEntries picks up both at Save time.
    for (const wrap of trackEl.querySelectorAll(".track-block-transition-wrap")) {
        if (wrap.dataset.bound === "1") continue;
        wrap.dataset.bound = "1";
        const chip = wrap.querySelector(".track-block-transition");
        const pop = wrap.querySelector(".track-block-transition-popover");
        const kindSel = wrap.querySelector(".track-block-transition-kind");
        const msInput = wrap.querySelector(".track-block-transition-ms");
        const block = chip?.closest(".track-block");
        if (!chip || !pop || !kindSel || !msInput || !block) continue;
        // Initial sync: enforce cut-locks-ms-to-0 on first render.
        if (kindSel.value === "cut") {
            msInput.value = "0";
            msInput.disabled = true;
            block.dataset.transitionMs = "0";
        }
        chip.addEventListener("click", (e) => {
            e.stopPropagation();
            const willOpen = pop.hidden;
            // Close any other open popover so we don't stack.
            for (const other of trackEl.querySelectorAll(
                ".track-block-transition-popover",
            )) {
                if (other !== pop) other.hidden = true;
            }
            pop.hidden = !willOpen;
            chip.setAttribute("aria-expanded", String(willOpen));
            if (willOpen) {
                // Focus the kind select for keyboard operators.
                kindSel.focus();
            }
        });
        // Clicks inside the popover should not bubble up and close it via
        // the outside-click handler below.
        pop.addEventListener("click", (e) => e.stopPropagation());
        kindSel.addEventListener("change", () => {
            const newKind = kindSel.value;
            if (newKind === "cut") {
                // Remember the current ms so a future kind-flip restores it.
                const m = clampTransitionMs(msInput.value);
                if (m > 0) block.dataset.lastNonZeroMs = String(m);
                msInput.value = "0";
                msInput.disabled = true;
                block.dataset.transitionMs = "0";
            } else {
                msInput.disabled = false;
                block.dataset.lastNonCutKind = newKind;
                if (clampTransitionMs(msInput.value) === 0) {
                    const restored = clampTransitionMs(
                        block.dataset.lastNonZeroMs,
                    ) || 500;
                    msInput.value = String(restored);
                }
                block.dataset.transitionMs = String(
                    clampTransitionMs(msInput.value),
                );
            }
            block.dataset.transition = newKind;
            chip.textContent = transitionChipLabel(
                newKind,
                clampTransitionMs(msInput.value),
            );
            markDirty();
        });
        msInput.addEventListener("input", () => {
            const m = clampTransitionMs(msInput.value);
            // Surface the clamp in the input itself so user feedback is
            // immediate. Only rewrite if the canonicalised value differs
            // to avoid caret-jump on every keystroke.
            if (String(m) !== msInput.value) msInput.value = String(m);
            if (m > 0 && kindSel.value === "cut") {
                // Operator typed a duration while kind=cut → auto-switch
                // to the last non-cut kind (default "fade") so they don't
                // have to make two trips through the popover.
                const restoredKind = block.dataset.lastNonCutKind || "fade";
                kindSel.value = restoredKind;
                block.dataset.transition = restoredKind;
                msInput.disabled = false;
            }
            if (kindSel.value !== "cut") {
                block.dataset.lastNonCutKind = kindSel.value;
                if (m > 0) block.dataset.lastNonZeroMs = String(m);
            }
            block.dataset.transitionMs = String(m);
            chip.textContent = transitionChipLabel(kindSel.value, m);
            markDirty();
        });
    }

    // One per-track outside-click + Escape handler closes any open
    // transition popover so the chip click target is "open" only.
    if (trackEl.dataset.transitionPopoverBound !== "1") {
        trackEl.dataset.transitionPopoverBound = "1";
        const closeAll = () => {
            for (const pop of trackEl.querySelectorAll(
                ".track-block-transition-popover",
            )) {
                if (!pop.hidden) pop.hidden = true;
            }
            for (const chip of trackEl.querySelectorAll(
                ".track-block-transition[aria-expanded='true']",
            )) {
                chip.setAttribute("aria-expanded", "false");
            }
        };
        // Use the trackEl-scoped document for jsdom + a real browser.
        const doc = trackEl.ownerDocument || document;
        doc.addEventListener("click", (e) => {
            if (!e.target.closest?.(".track-block-transition-wrap")) {
                closeAll();
            }
        });
        doc.addEventListener("keydown", (e) => {
            if (e.key === "Escape") closeAll();
        });
    }
}

function bindTrackDurationButtons(trackEl, onUpdateDuration, refresh, statusEl) {
    if (!onUpdateDuration) return;
    for (const btn of trackEl.querySelectorAll(".track-block-duration")) {
        if (btn.dataset.bound === "1") continue;
        btn.dataset.bound = "1";
        btn.addEventListener("click", async () => {
            const block = btn.closest("[data-id]");
            if (!block) return;
            const id = block.dataset.id;
            const currentMs = Number(btn.dataset.durationMs) || 5000;
            const currentSec = Math.round((currentMs / 1000) * 10) / 10;
            // Browser prompt is unglamorous but unambiguous and works
            // on touch + desktop. Upgrade to a popover if it grates.
            const next = window.prompt(
                "Duration in seconds:",
                String(currentSec),
            );
            if (next == null) return;
            const seconds = Number(next);
            if (!Number.isFinite(seconds) || seconds <= 0) return;
            const ms = Math.round(seconds * 1000);
            // Duration is a slide-level attribute — auto-save
            // immediately, outside the playlist's draft flow.
            try {
                await onUpdateDuration(id, ms);
                if (refresh) await refresh();
            } catch (err) {
                console.error("[playlist-track] duration save failed:", err);
                // Surface to operator via the panel's status pill so
                // the failure isn't a console-only silent drop.
                if (statusEl) {
                    statusEl.textContent = `Couldn't save duration · ${err?.message || err}`;
                    statusEl.dataset.state = "error";
                }
            }
        });
    }
}

// Shared thumb-wrap builder for the two playlist-track renderers
// (renderTrackBlock + renderPalletTile). Returns the thumb-wrap HTML
// fragment as a string -- callers inline it into their li.innerHTML
// template and then querySelector(`.${classPrefix}-thumb-wrap`) to
// pass into attachAutoTextOverlay.
//
// Parameterized bits:
//   classPrefix:   "track-block" | "pallet-tile" -- drives the
//                  `${prefix}-thumb-wrap` + `${prefix}-thumb` +
//                  `${prefix}-lock` class names.
//   locked:        toggles the locked-badge "⚠" span.
//   cacheBust:     fallback `?v=` for the asset URL when neither
//                  updated_at nor created_at is set.
//   draggable:     img-level drag attribute. true (default) leaves
//                  the attribute off (HTML's img-default is
//                  draggable=true); false stamps `draggable="false"`
//                  so the img-level drag-ghost doesn't fight the
//                  LI-level Sortable.js drag. Track-block omits the
//                  attribute; pallet-tile sets it to false.
//   extraChildren: extra HTML to inject INSIDE the thumb-wrap,
//                  AFTER the locked-badge. Pallet-tile uses this for
//                  the hover-revealed edit + delete buttons (which
//                  reference the caller-side escaped name in
//                  aria-label/title attributes); track-block leaves
//                  it empty.
//
// Sister DRY commits this session: bg-picker.js (7aeec9a),
// auto-text-overlay::renderOne (8b682b5).
function renderThumbWrap(item, {
    classPrefix,
    locked = false,
    cacheBust = 0,
    draggable = true,
    extraChildren = "",
}) {
    const lockedBadge = locked
        ? `<span class="${classPrefix}-lock" title="Stored for a different output mode — won't play on this device">⚠</span>`
        : "";
    const draggableAttr = draggable ? "" : ` draggable="false"`;
    // 2026-07-02 (handover-blocker fix): swap to the small /thumbnail
    // JPEG endpoint + lazy-load + onerror placeholder — the raw
    // /asset PNG was blowing up memory on the Pi. Emits the whole
    // src / loading / decoding / onerror block so future call sites
    // stay consistent.
    const thumbAttrs = contentTileThumbnailAttrs(
        item.id,
        item.updated_at || item.created_at || cacheBust,
    );
    return `<div class="${classPrefix}-thumb-wrap">
            <img class="${classPrefix}-thumb" alt=""${draggableAttr}
                 ${thumbAttrs}>
            ${lockedBadge}${extraChildren}
        </div>`;
}

function renderTrackBlock(
    item,
    entry = { transition: "cut", transition_ms: 500 },
    { locked = false, cacheBust = 0 } = {},
) {
    const li = document.createElement("li");
    li.className = locked ? "track-block track-block--locked" : "track-block";
    li.dataset.id = String(item.id);
    li.dataset.transition = entry.transition;
    li.dataset.transitionMs = String(entry.transition_ms);

    const safeName = escapeHtml(item.name || "Untitled");
    const safeType = escapeHtml((item.type || "").toUpperCase() || "—");
    const seconds = (Number(item.duration_ms) || 5000) / 1000;
    // Big numeral on the right, "SEC" small below — split label so we can
    // typeset them separately. Click anywhere on the dur cell to edit.
    const durationLabel = Number.isInteger(seconds)
        ? String(seconds)
        : seconds.toFixed(1);
    // r52: the .track-block-transition chip is now a button that opens an
    // inline popover with BOTH kind and duration controls (Option B from
    // the UX sketches). Cut transitions structurally have ms=0; the popover
    // disables the ms input when kind=cut and auto-switches kind to "fade"
    // (or the last-non-cut kind) if the operator types a non-zero duration
    // while kind=cut. The chip itself stays compact in the row — its label
    // is built by transitionChipLabel(kind, ms) so a scannable read of the
    // playlist shows the duration without opening every popover.
    const chipLabel = transitionChipLabel(
        entry.transition,
        entry.transition_ms,
    );
    li.innerHTML = `
        <div class="track-grip" aria-hidden="true">
            <span class="track-grip-dots">⋮⋮</span>
        </div>
        ${renderThumbWrap(item, { classPrefix: "track-block", locked, cacheBust })}
        <div class="track-block-meta">
            <b class="track-block-name">${safeName}</b>
            <span class="track-block-sub">${safeType} · #${String(item.id).slice(0, 6)}</span>
            <div class="track-block-transition-wrap">
                <button type="button" class="om-pulldown track-block-transition"
                        aria-haspopup="true" aria-expanded="false"
                        title="Click to change transition kind and duration">${escapeHtml(chipLabel)}</button>
                <div class="track-block-transition-popover" hidden>
                    <label class="track-block-transition-row">
                        <span>Kind</span>
                        <select class="om-pulldown track-block-transition-kind">${transitionOptionsHTML(entry.transition)}</select>
                    </label>
                    <label class="track-block-transition-row">
                        <span>Duration</span>
                        <input type="number" class="track-block-transition-ms"
                               min="0" max="5000" step="50"
                               value="${Number(entry.transition_ms) || 0}">
                        <span class="track-block-transition-ms-unit">ms</span>
                    </label>
                </div>
            </div>
        </div>
        <button type="button" class="track-block-duration track-block-dur"
                title="Click to change this slide's duration"
                data-duration-ms="${Number(item.duration_ms) || 5000}">
            <span class="track-block-num">${durationLabel}</span>
            <span class="track-block-unit">SEC</span>
        </button>
        <button type="button" class="track-remove" aria-label="Remove from playlist" title="Remove from playlist">×</button>
    `;
    attachAutoTextOverlay(li.querySelector(".track-block-thumb-wrap"), item);
    return li;
}

function renderPalletTile(item, { locked = false, cacheBust = 0 } = {}) {
    const li = document.createElement("li");
    li.className = locked ? "pallet-tile pallet-tile--locked" : "pallet-tile";
    li.dataset.id = String(item.id);
    li.dataset.type = item.type;
    const safeName = escapeHtml(item.name || "Untitled");
    // Pallet cards: thumbnail on top (2:1 aspect, snap target) + name
    // bar below. Edit/delete buttons stay hover-revealed in the corners.
    const palletButtons = `
            <button type="button" class="pallet-tile-edit"
                    aria-label="Edit ${safeName}" title="Edit ${safeName}">✎</button>
            <button type="button" class="pallet-tile-delete"
                    aria-label="Delete ${safeName}" title="Delete ${safeName}">×</button>`;
    li.innerHTML = `
        ${renderThumbWrap(item, {
            classPrefix: "pallet-tile",
            locked,
            cacheBust,
            draggable: false,
            extraChildren: palletButtons,
        })}
        <div class="pallet-tile-name" title="${safeName}">${safeName}</div>
    `;
    attachAutoTextOverlay(li.querySelector(".pallet-tile-thumb-wrap"), item);
    li.querySelector(".pallet-tile-edit").addEventListener("click", (event) => {
        // Bubble a custom event so main.js (which owns the editors +
        // router) can navigate + pre-fill without playlist-track having
        // to import either.
        event.stopPropagation();
        document.dispatchEvent(
            new CustomEvent("openmarquee:edit-slide", {
                detail: { id: String(item.id), type: item.type },
            }),
        );
    });
    li.querySelector(".pallet-tile-delete").addEventListener("click", (event) => {
        event.stopPropagation();
        document.dispatchEvent(
            new CustomEvent("openmarquee:delete-slide", {
                detail: { id: String(item.id), type: item.type, name: item.name },
            }),
        );
    });
    return li;
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
