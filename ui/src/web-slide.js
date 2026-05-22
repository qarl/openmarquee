// Web slide editor — a pure-metadata form for a playlist slide that
// shows a screenshot of an operator-supplied web page.
//
// The Pi renders the page itself with headless Chromium and bakes a
// screenshot. Architecturally the Web slide is "an image slide whose
// asset.png is auto-refreshed from an on-device render". On create
// there is no screenshot yet — the backend synthesises a placeholder
// card (storage.save_web). So this
// module, like the Stream editor, is just a metadata form — name,
// page URL, refresh interval, and duration — wired to the same
// auto-save + slide-browser scaffolding the other slide editors use.
//
// There is NO file upload and NO in-editor preview: the editor can't
// run a browser any more than the Pi can; the helper screenshots the
// page on the operator's machine and the result only appears on the
// sign.
//
// Transitions are not edited here (consistent with the image / video /
// stream editors); the slide's transition/transition_ms round-trip
// through `state` so editing a slide's name doesn't reset a transition
// set elsewhere.

import { attachAutoSave } from "./auto-save.js";
import { mountSlideBrowser, nextAutoName } from "./slide-browser.js";

// Common refresh cadences offered in the select. Values are seconds and
// stay within the backend's bound (refresh_interval_s: 10s..86400s,
// WebSlide in content/__init__.py). 3600s (1 hour) is the default — an
// on-device render is multi-minute on the Pi, so a tighter default
// would re-render almost continuously.
const REFRESH_OPTIONS = [
    { value: 60, label: "Every minute" },
    { value: 300, label: "Every 5 minutes" },
    { value: 900, label: "Every 15 minutes" },
    { value: 1800, label: "Every 30 minutes" },
    { value: 3600, label: "Every hour" },
    { value: 21600, label: "Every 6 hours" },
    { value: 86400, label: "Once a day" },
];
const DEFAULT_REFRESH_S = 3600;

const TEMPLATE = `
    <section class="web-slide">
        <div class="slide-browser-slot"></div>
        <form class="controls" autocomplete="off">
            <div class="om-card" style="margin-bottom: 12px;">
                <div class="om-row" style="gap: 10px;">
                    <label class="om-field" style="flex: 1;">
                        <span>Slide name</span>
                        <input type="text" class="om-input field-name" value="Web" maxlength="200">
                    </label>
                    <label class="om-field" style="width: 110px;">
                        <span>Duration (s)</span>
                        <input type="number" class="om-input field-duration" value="10" min="1" max="86400" step="1">
                    </label>
                </div>
            </div>
            <div class="om-card" style="margin-bottom: 12px;">
                <label class="om-field">
                    <span>Page URL</span>
                    <input type="text" class="om-input field-web-url"
                           placeholder="https://status.example.com"
                           autocomplete="off" spellcheck="false">
                </label>
                <label class="om-field field-web-refresh-wrap">
                    <span>Refresh the screenshot</span>
                    <select class="om-pulldown om-pulldown-cased field-web-refresh"></select>
                </label>
                <p class="web-refresh-note">
                    The screenshot only refreshes while the helper machine
                    you run is reachable from the sign.
                </p>
            </div>
            <div class="om-card web-slide-preview">
                <div class="web-preview-card">
                    <span class="web-preview-title">&#127760; Web page</span>
                    <span class="web-preview-url"></span>
                    <span class="web-preview-note">Screenshotted on the sign &mdash; not previewed in the editor.</span>
                </div>
            </div>
            <p class="om-save-status web-slide-status" role="status" aria-live="polite" data-state="idle"></p>
        </form>
    </section>
`;

/**
 * Mount the web slide editor into `container`.
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
export function mountWebSlideEditor(
    container,
    { onSave, onSaveExisting, fetchItems },
) {
    container.innerHTML = TEMPLATE;

    const form = container.querySelector(".controls");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const webUrlEl = container.querySelector(".field-web-url");
    const refreshEl = container.querySelector(".field-web-refresh");
    const statusEl = container.querySelector(".web-slide-status");
    const previewUrlEl = container.querySelector(".web-preview-url");

    // Populate the refresh-interval select once.
    for (const opt of REFRESH_OPTIONS) {
        const el = document.createElement("option");
        el.value = String(opt.value);
        el.textContent = opt.label;
        refreshEl.appendChild(el);
    }
    refreshEl.value = String(DEFAULT_REFRESH_S);

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

    function refreshIntervalValue() {
        const n = Number(refreshEl.value);
        return Number.isFinite(n) && n > 0 ? n : DEFAULT_REFRESH_S;
    }

    // The select only carries the curated cadences; an existing slide
    // may store a refresh_interval_s outside that list (an older slide,
    // a flock-synced one). Snap to the nearest offered option so the
    // select always shows *something* and a metadata-only re-save
    // doesn't silently churn the interval.
    function setRefreshInterval(seconds) {
        const target = Number(seconds);
        if (!Number.isFinite(target) || target <= 0) {
            refreshEl.value = String(DEFAULT_REFRESH_S);
            return;
        }
        if (REFRESH_OPTIONS.some((o) => o.value === target)) {
            refreshEl.value = String(target);
            return;
        }
        let nearest = REFRESH_OPTIONS[0];
        for (const o of REFRESH_OPTIONS) {
            if (
                Math.abs(o.value - target) <
                Math.abs(nearest.value - target)
            ) {
                nearest = o;
            }
        }
        refreshEl.value = String(nearest.value);
    }

    function refreshPreviewUrl() {
        previewUrlEl.textContent =
            webUrlEl.value.trim() || "(no page URL yet)";
    }
    webUrlEl.addEventListener("input", refreshPreviewUrl);

    async function performSave() {
        const durationSeconds = Number(durationEl.value) || 10;
        const payload = {
            name: nameEl.value || "Web",
            url: webUrlEl.value.trim(),
            refresh_interval_s: refreshIntervalValue(),
            duration_ms: Math.round(durationSeconds * 1000),
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
        // A web slide is useless without a page URL — gate create-mode
        // saves on a non-empty URL. Editing an existing slide is always
        // saveable (the URL is already on the server).
        canSave: () =>
            Boolean(state.editingId) || Boolean(webUrlEl.value.trim()),
    });

    async function computeDefaultName() {
        if (!fetchItems) return "Web 1";
        try {
            const items = await fetchItems();
            return nextAutoName(
                items.filter((i) => i.type === "web"),
                "Web",
            );
        } catch {
            return "Web 1";
        }
    }

    async function resetToBlank() {
        state.editingId = null;
        state.transition = "cut";
        state.transitionMs = 500;
        webUrlEl.value = "";
        durationEl.value = "10";
        setRefreshInterval(DEFAULT_REFRESH_S);
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
        if (!slide || slide.type !== "web") {
            statusEl.textContent = "Only web slides are editable here.";
            return;
        }
        state.editingId = String(slide.id);
        state.transition = slide.transition || "cut";
        state.transitionMs = slide.transition_ms ?? 500;
        nameEl.value = slide.name || "Web";
        webUrlEl.value = slide.url || "";
        setRefreshInterval(slide.refresh_interval_s ?? DEFAULT_REFRESH_S);
        durationEl.value = String(
            Math.max(1, (slide.duration_ms || 10_000) / 1000),
        );
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
                type: "web",
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
     * there is nothing to upload — the operator types a page URL and
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
