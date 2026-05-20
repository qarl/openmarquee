// VLC-stream slide editor — a pure-metadata form for a playlist slide
// that plays an RTSP stream the operator's VLC is publishing
// (docs/STREAM_VLC_PROPOSAL.md §6, Mode B).
//
// Unlike the image/video editors there is NO file upload: the video is
// a live RTSP feed and the slide's thumbnail card is synthesised
// server-side (storage.save_vlc_stream). So this module is just a
// metadata form — name, RTSP URL, duration, and the on_unreachable
// fallback — wired to the same auto-save + slide-browser scaffolding
// the other slide editors use.
//
// Transitions are not edited here (consistent with the image/video
// editors); the slide's transition/transition_ms round-trip through
// `state` so editing a slide's name doesn't reset a transition set
// elsewhere.

import { attachAutoSave } from "./auto-save.js";
import { mountSlideBrowser, nextAutoName } from "./slide-browser.js";

const TEMPLATE = `
    <section class="vlc-stream-upload">
        <div class="slide-browser-slot"></div>
        <form class="controls" autocomplete="off">
            <div class="om-card" style="margin-bottom: 12px;">
                <div class="om-row" style="gap: 10px;">
                    <label class="om-field" style="flex: 1;">
                        <span>Slide name</span>
                        <input type="text" class="om-input field-name" value="VLC stream" maxlength="200">
                    </label>
                    <label class="om-field" style="width: 110px;">
                        <span>Duration (s)</span>
                        <input type="number" class="om-input field-duration" value="10" min="1" max="86400" step="1">
                    </label>
                </div>
            </div>
            <div class="om-card" style="margin-bottom: 12px;">
                <label class="om-field">
                    <span>RTSP URL</span>
                    <input type="text" class="om-input field-rtsp-url"
                           placeholder="rtsp://your-laptop:8554/live"
                           autocomplete="off" spellcheck="false">
                </label>
                <fieldset class="om-field vlc-unreachable">
                    <span>If the stream isn't running</span>
                    <label><input type="radio" name="vlc-on-unreachable" value="hold_last_frame" checked> Hold last frame</label>
                    <label><input type="radio" name="vlc-on-unreachable" value="black"> Show black</label>
                    <label><input type="radio" name="vlc-on-unreachable" value="skip"> Skip slide</label>
                </fieldset>
            </div>
            <div class="om-card vlc-stream-preview">
                <div class="vlc-stream-preview-card">
                    <span class="vlc-stream-preview-title">&#9654; Live VLC stream</span>
                    <span class="vlc-stream-preview-url"></span>
                    <span class="vlc-stream-preview-note">Plays live on the sign &mdash; not previewed in the editor.</span>
                </div>
            </div>
            <p class="om-save-status vlc-stream-upload-status" role="status" aria-live="polite" data-state="idle"></p>
        </form>
    </section>
`;

/**
 * Mount the VLC-stream slide editor into `container`.
 *
 * @param {HTMLElement} container — parent element (emptied and replaced).
 * @param {object} options
 * @param {(payload) => Promise<any>} options.onSave — called for NEW slides.
 * @param {(id, payload) => Promise<any>} [options.onSaveExisting] — called
 *     on save when editing an existing slide.
 * @param {() => Promise<any[]>} [options.fetchItems] — content list, for
 *     the slide browser + auto-naming.
 * @returns {{ loadForEdit, createNew, refreshBrowser }}
 */
export function mountVlcStreamUploader(
    container,
    { onSave, onSaveExisting, fetchItems },
) {
    container.innerHTML = TEMPLATE;

    const form = container.querySelector(".controls");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const rtspUrlEl = container.querySelector(".field-rtsp-url");
    const statusEl = container.querySelector(".vlc-stream-upload-status");
    const previewUrlEl = container.querySelector(".vlc-stream-preview-url");

    const state = {
        // Non-null once an existing slide is loaded OR a create-mode
        // save returns an id — subsequent saves PUT the same id.
        editingId: null,
        // transition / transition_ms are not edited in this form, but
        // they round-trip so editing a slide here doesn't reset a
        // transition set in the playlist editor. Defaults for a new
        // slide; overwritten from the slide on loadForEdit.
        transition: "cut",
        transitionMs: 500,
    };

    function onUnreachableValue() {
        const checked = form.querySelector(
            'input[name="vlc-on-unreachable"]:checked',
        );
        return checked ? checked.value : "hold_last_frame";
    }

    function setOnUnreachable(value) {
        const radio = form.querySelector(
            `input[name="vlc-on-unreachable"][value="${value}"]`,
        );
        (radio || form.querySelector('input[name="vlc-on-unreachable"]')).checked = true;
    }

    function refreshPreviewUrl() {
        previewUrlEl.textContent =
            rtspUrlEl.value.trim() || "(no RTSP URL yet)";
    }
    rtspUrlEl.addEventListener("input", refreshPreviewUrl);

    async function performSave() {
        const durationSeconds = Number(durationEl.value) || 10;
        const payload = {
            name: nameEl.value || "VLC stream",
            rtsp_url: rtspUrlEl.value.trim(),
            duration_ms: Math.round(durationSeconds * 1000),
            on_unreachable: onUnreachableValue(),
            transition: state.transition,
            transition_ms: state.transitionMs,
        };
        if (state.editingId && onSaveExisting) {
            await onSaveExisting(state.editingId, payload);
            return;
        }
        const created = await onSave(payload);
        if (created?.id) {
            state.editingId = String(created.id);
            if (browser) {
                await browser.refresh();
                browser.highlight(state.editingId);
            }
        }
    }

    const autoSave = attachAutoSave(form, {
        save: performSave,
        status: statusEl,
        // A VLC slide is useless without an RTSP URL — gate create-mode
        // saves on a non-empty URL. Editing an existing slide is always
        // saveable (the URL is already on the server).
        canSave: () =>
            Boolean(state.editingId) || Boolean(rtspUrlEl.value.trim()),
    });

    async function computeDefaultName() {
        if (!fetchItems) return "VLC Stream 1";
        try {
            const items = await fetchItems();
            return nextAutoName(
                items.filter((i) => i.type === "vlc_stream"),
                "VLC Stream",
            );
        } catch {
            return "VLC Stream 1";
        }
    }

    async function resetToBlank() {
        state.editingId = null;
        state.transition = "cut";
        state.transitionMs = 500;
        rtspUrlEl.value = "";
        durationEl.value = "10";
        setOnUnreachable("hold_last_frame");
        refreshPreviewUrl();
        autoSave.cancel();
        statusEl.textContent = "";
        statusEl.dataset.state = "idle";

        const defaultName = await computeDefaultName();
        // A loadForEdit may have raced in while we awaited — don't
        // clobber it.
        if (state.editingId !== null) return;
        nameEl.value = defaultName;
        if (browser) {
            await browser.refresh();
            browser.highlight(null);
        }
    }

    function loadForEdit(slide) {
        if (!slide || slide.type !== "vlc_stream") {
            statusEl.textContent =
                "Only VLC-stream slides are editable here.";
            return;
        }
        state.editingId = String(slide.id);
        state.transition = slide.transition || "cut";
        state.transitionMs = slide.transition_ms ?? 500;
        nameEl.value = slide.name || "VLC stream";
        rtspUrlEl.value = slide.rtsp_url || "";
        durationEl.value = String(
            Math.max(1, (slide.duration_ms || 10_000) / 1000),
        );
        setOnUnreachable(slide.on_unreachable || "hold_last_frame");
        refreshPreviewUrl();
        if (browser) browser.highlight(slide.id);
        // Loading an existing slide is not a user edit — drop any
        // auto-save the field mutations above scheduled.
        autoSave.cancel();
    }

    let browser = null;
    if (fetchItems) {
        browser = mountSlideBrowser(
            container.querySelector(".slide-browser-slot"),
            {
                type: "vlc_stream",
                fetchItems,
                onSelect: (item) => loadForEdit(item),
                onCreate: () => resetToBlank(),
            },
        );
    }

    (async () => {
        await resetToBlank();
    })();

    /**
     * +New flow: a blank create form. Unlike the image/video editors
     * there is nothing to upload — the operator types an RTSP URL and
     * the auto-save persists the slide once the URL is non-empty.
     */
    async function createNew() {
        await resetToBlank();
    }

    return {
        loadForEdit,
        createNew,
        refreshBrowser: () => browser?.refresh(),
        flushAutoSave: () => autoSave.flush(),
    };
}
