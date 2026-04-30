// Text-slide editor: form controls + live canvas preview. Canvas is at the
// sign's native resolution; the browser scales it up for display via CSS
// (image-rendering: pixelated) so what you see is what the sign will show.
//
// The editor handles both creating new slides AND editing existing ones.
// `loadForEdit(slide)` pre-fills the form from a stored TextSlide; Save
// then dispatches to `onSaveExisting(id, payload)` instead of `onSave`.
// Click "New slide" to exit edit-mode.
//
// The horizontal slide-browser at the top is the primary way operators
// move between slides: click a tile to edit, click "+ New" to create.
// The Playlists-page pallet's ✎ edit button still works too — either
// surface feels natural in its own context.

import { attachAutoSave } from "./auto-save.js";
import { formatAutoText } from "./auto-format.js";
import { mountSlideBrowser, nextAutoName } from "./slide-browser.js";

// Fixed asset rasterize target. Decoupled from device W×H so a panel
// resize never degrades a stored text slide — playback cover-fits the
// 4K bitmap down to whatever the panel is.
const RASTERIZE_W = 3840;
const RASTERIZE_H = 2160;

// Signage-friendly color presets. Per SYSTEM_SPEC §5.1: most users just want
// "white on red" and to be done — not to fiddle with a color picker.
const PRESETS = [
    { name: "White on black", text: "#FFFFFF", bg: "#000000" },
    { name: "White on red", text: "#FFFFFF", bg: "#CC0000" },
    { name: "Yellow on blue", text: "#FFD23A", bg: "#1538A8" },
    { name: "Black on yellow", text: "#000000", bg: "#FFD23A" },
    { name: "White on green", text: "#FFFFFF", bg: "#1F7A3A" },
    { name: "Green on black", text: "#39FF14", bg: "#000000" },
];

// Generic CSS font families — operator picks the shape ("sans / serif /
// mono"); the specific face comes from whatever the rendering device has.
// Kept generic so the canvas render matches the device render.
// `weight` = the numeric CSS font-weight to request in `ctx.font`. Using
// each face's *native* weight avoids browser-synthesized fake bold on
// single-weight display fonts (Pacifico, Bebas Neue, etc.), which looks
// smeary. Variable fonts (Inter, Oswald, Roboto Slab, Cinzel) cover 700
// natively and look correct at bold. UnifrakturCook ships only as the
// Bold cut, so 700 matches the file.
export const FONT_FAMILIES = [
    // System generics — render with whatever the device has bundled at OS level.
    { value: "sans-serif",            label: "Sans-serif",            category: "System",     weight: 700 },
    { value: "serif",                 label: "Serif",                 category: "System",     weight: 700 },
    { value: "monospace",             label: "Monospace",             category: "System",     weight: 700 },
    // Block / display — bold, attention-grabbing.
    { value: "Inter",                 label: "Inter",                 category: "Block",      weight: 700 },
    { value: "Oswald",                label: "Oswald",                category: "Block",      weight: 700 },
    { value: "Bebas Neue",            label: "Bebas Neue",            category: "Block",      weight: 400 },
    { value: "Bowlby One SC",         label: "Bowlby One SC",         category: "Block",      weight: 400 },
    { value: "Anton",                 label: "Anton",                 category: "Block",      weight: 400 },
    { value: "Archivo Black",         label: "Archivo Black",         category: "Block",      weight: 400 },
    { value: "Alfa Slab One",         label: "Alfa Slab One",         category: "Block",      weight: 400 },
    // Serif — classical / editorial.
    { value: "Roboto Slab",           label: "Roboto Slab",           category: "Serif",      weight: 700 },
    { value: "Cinzel",                label: "Cinzel",                category: "Serif",      weight: 700 },
    { value: "Playfair Display",      label: "Playfair Display",      category: "Serif",      weight: 700 },
    { value: "DM Serif Display",      label: "DM Serif Display",      category: "Serif",      weight: 400 },
    { value: "UnifrakturCook",        label: "UnifrakturCook",        category: "Serif",      weight: 700 },
    // Mono / scoreboard — countdown / retro digital.
    { value: "VT323",                 label: "VT323",                 category: "Mono",       weight: 400 },
    { value: "JetBrains Mono",        label: "JetBrains Mono",        category: "Mono",       weight: 700 },
    { value: "Space Mono",            label: "Space Mono",            category: "Mono",       weight: 700 },
    // Script — flowing scripts, themed display, casual handwriting,
    // and chalk/marker letterforms all share the same use-case bucket
    // for the operator (decorative + personality vs typographic class).
    { value: "Pacifico",              label: "Pacifico",              category: "Script",     weight: 400 },
    { value: "Rye",                   label: "Rye",                   category: "Script",     weight: 400 },
    { value: "Sedgwick Ave Display",  label: "Sedgwick Ave Display",  category: "Script",     weight: 400 },
    { value: "Caveat Brush",          label: "Caveat Brush",          category: "Script",     weight: 400 },
    { value: "Permanent Marker",      label: "Permanent Marker",      category: "Script",     weight: 400 },
    { value: "Caveat",                label: "Caveat",                category: "Script",     weight: 700 },
    { value: "Reenie Beanie",         label: "Reenie Beanie",         category: "Script",     weight: 400 },
    { value: "Shadows Into Light",    label: "Shadows Into Light",    category: "Script",     weight: 400 },
];

const FONT_WEIGHT_BY_VALUE = new Map(FONT_FAMILIES.map((f) => [f.value, f.weight]));

/**
 * Wire up the visual font picker around an existing hidden <select> +
 * trigger button + popover. The hidden <select> stays the source of
 * truth (existing read/write code reads `.value`); this layer just
 * paints tiles in their own faces and syncs both directions.
 */
function setupFontPicker(container) {
    const selectEl = container.querySelector(".field-font-family");
    const trigger = container.querySelector(".font-picker-trigger");
    const triggerLabel = container.querySelector(".font-picker-trigger-label");
    const popover = container.querySelector(".font-picker-popover");
    if (!selectEl || !trigger || !popover) return;

    const byCategory = new Map();
    for (const f of FONT_FAMILIES) {
        if (!byCategory.has(f.category)) byCategory.set(f.category, []);
        byCategory.get(f.category).push(f);
    }
    for (const [cat, fonts] of byCategory) {
        const section = document.createElement("div");
        section.className = "font-picker-section";
        section.innerHTML = `<div class="font-picker-section-head">${cat}</div>`;
        const grid = document.createElement("div");
        grid.className = "font-picker-grid";
        for (const f of fonts) {
            const tile = document.createElement("button");
            tile.type = "button";
            tile.className = "font-picker-tile";
            tile.dataset.value = f.value;
            tile.textContent = f.label;
            tile.style.fontFamily = cssFontFamily(f.value);
            tile.style.fontWeight = String(f.weight);
            tile.setAttribute("role", "option");
            grid.appendChild(tile);
        }
        section.appendChild(grid);
        popover.appendChild(section);
    }

    function syncTrigger() {
        const value = selectEl.value || FONT_FAMILIES[0].value;
        const meta = FONT_FAMILIES.find((f) => f.value === value) || FONT_FAMILIES[0];
        triggerLabel.textContent = meta.label;
        triggerLabel.style.fontFamily = cssFontFamily(meta.value);
        triggerLabel.style.fontWeight = String(meta.weight);
        for (const tile of popover.querySelectorAll(".font-picker-tile")) {
            tile.classList.toggle("selected", tile.dataset.value === value);
            tile.setAttribute("aria-selected", tile.dataset.value === value ? "true" : "false");
        }
    }

    function setOpen(open) {
        popover.hidden = !open;
        trigger.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
            // Scroll the selected tile into view so the operator sees
            // their current pick instead of starting at "Sans-serif".
            const sel = popover.querySelector(".font-picker-tile.selected");
            sel?.scrollIntoView?.({ block: "nearest" });
        }
    }

    trigger.addEventListener("click", (ev) => {
        ev.stopPropagation();
        setOpen(popover.hidden);
    });

    popover.addEventListener("click", (ev) => {
        const tile = ev.target.closest(".font-picker-tile");
        if (!tile) return;
        const value = tile.dataset.value;
        if (selectEl.value === value) {
            setOpen(false);
            return;
        }
        selectEl.value = value;
        // Native <select> fires both `input` and `change` on user pick;
        // the editor wires `input` to syncAndRender (state + canvas
        // redraw) and `change` to font-load-then-redraw. Dispatching
        // only `change` leaves the live preview stale until the next
        // input on any field. Mimic the native pair to keep both
        // pathways in sync.
        selectEl.dispatchEvent(new Event("input", { bubbles: true }));
        selectEl.dispatchEvent(new Event("change", { bubbles: true }));
        syncTrigger();
        setOpen(false);
    });

    document.addEventListener("click", (ev) => {
        if (popover.hidden) return;
        if (popover.contains(ev.target) || trigger.contains(ev.target)) return;
        setOpen(false);
    });

    // External `.value = ...` doesn't fire `change`, so re-sync on each
    // editor refresh path. The `change` listener still covers user input.
    selectEl.addEventListener("change", syncTrigger);
    selectEl.addEventListener("font-picker-sync", syncTrigger);
    syncTrigger();
}

/**
 * Return a CSS font-family string safe for use in `ctx.font` / `style.font`.
 * Generic keywords (sans-serif, serif, monospace) must NOT be quoted; named
 * families with spaces must be. Canvas will silently drop an unquoted
 * "Bebas Neue" otherwise.
 */
function cssFontFamily(value) {
    const GENERICS = new Set([
        "sans-serif", "serif", "monospace", "cursive", "fantasy",
        "system-ui", "ui-sans-serif", "ui-serif", "ui-monospace",
    ]);
    return GENERICS.has(value) ? value : `"${value}"`;
}

function presetButtonsHtml() {
    return PRESETS.map(
        (p, i) => `
        <button type="button" class="preset" data-preset-index="${i}"
                aria-label="${p.name}"
                title="${p.name}"
                style="background:${p.bg};color:${p.text};">Aa</button>
    `,
    ).join("");
}

const EDITOR_TEMPLATE = `
    <div class="editor">
        <div class="slide-browser-slot"></div>
        <form class="controls" autocomplete="off">
            <div class="om-card" style="margin-bottom: 12px;">
                <div class="om-row" style="gap: 10px;">
                    <label class="om-field" style="flex: 1;">
                        <span>Slide name</span>
                        <input type="text" class="om-input field-name" value="Untitled" maxlength="200">
                    </label>
                    <label class="om-field" style="width: 110px;">
                        <span>Duration (s)</span>
                        <input type="number" class="om-input field-duration" value="5" min="1" max="300" step="1">
                    </label>
                </div>
            </div>
            <div class="preview-wrap">
                <canvas class="editor-canvas" aria-label="slide preview"></canvas>
            </div>
            <div class="om-card">
                <div class="om-stack" style="gap: 12px;">
                    <label class="om-field">
                        <span>Text</span>
                        <textarea class="om-textarea field-text" rows="3" placeholder="(enter text here)"></textarea>
                    </label>
                    <label class="om-field">
                        <span>Dynamic Text</span>
                        <select class="om-select field-auto-mode">
                            <option value="" selected>Off</option>
                            <option value="time">Current time</option>
                            <option value="date">Today's date</option>
                            <option value="day">Day of week</option>
                        </select>
                    </label>
                    <label class="om-field field-auto-format-wrap" hidden>
                        <span>Format</span>
                        <select class="om-select field-auto-format"></select>
                    </label>
                    <p class="field-hint field-auto-mode-hint" hidden style="margin: 0; color: var(--om-text-dim); font-size: 12.5px;">
                        When Dynamic Text is set, the typed text is a preview-only
                        fallback — the device re-renders each second at playback
                        time using the configured timezone.
                    </p>
                </div>
            </div>

            <div class="om-card">
                <div class="om-stack" style="gap: 12px;">
                    <div class="om-field">
                        <span>Quick colors</span>
                        <div class="presets">${presetButtonsHtml()}</div>
                    </div>
                    <div class="om-row" style="gap: 10px;">
                        <label class="om-field" style="flex: 1;">
                            <span>Text color</span>
                            <input type="color" class="field-text-color" value="#FFFFFF" style="width: 100%; height: 40px; border-radius: 9px; border: 1px solid var(--om-line); background: var(--om-surface-2);">
                        </label>
                        <label class="om-field" style="flex: 1;">
                            <span>Solid background</span>
                            <input type="color" class="field-bg-color" value="#000000" style="width: 100%; height: 40px; border-radius: 9px; border: 1px solid var(--om-line); background: var(--om-surface-2);">
                        </label>
                    </div>
                    <div class="om-row" style="gap: 10px;">
                        <div class="om-field font-picker" style="flex: 1;">
                            <span id="font-picker-label">Font</span>
                            <select class="om-select field-font-family" aria-labelledby="font-picker-label"></select>
                            <button type="button" class="font-picker-trigger" aria-haspopup="listbox" aria-expanded="false" aria-labelledby="font-picker-label">
                                <span class="font-picker-trigger-label">Sans-serif</span>
                                <span class="font-picker-trigger-caret" aria-hidden="true">▾</span>
                            </button>
                            <div class="font-picker-popover" role="listbox" hidden></div>
                        </div>
                        <label class="om-field" style="width: 140px;">
                            <span>Font size (% of height)</span>
                            <input type="number" class="om-input field-font-size" min="1" max="100" step="0.5">
                        </label>
                    </div>
                </div>
            </div>

            <div class="om-card editor-bg-picker">
                <div class="om-eyebrow" style="margin-bottom: 10px; font-family: var(--om-mono); letter-spacing: 0.14em; font-size: 10.5px; color: var(--om-text-fade); text-transform: uppercase;">Background source</div>
                <div class="om-stack" style="gap: 10px;">
                    <label class="om-row" style="gap: 8px; cursor: pointer;">
                        <input type="radio" name="editor-bg-source" class="field-bg-source" value="color" checked>
                        <span>Solid color (above)</span>
                    </label>
                    <label class="om-row" style="gap: 8px; cursor: pointer;">
                        <input type="radio" name="editor-bg-source" class="field-bg-source" value="slide">
                        <span>Image slide</span>
                    </label>
                    <label class="om-field editor-bg-slide-wrap" hidden>
                        <span>Saved image slide</span>
                        <select class="om-select field-bg-slide"><option value="">(pick a slide)</option></select>
                    </label>
                    <label class="om-row" style="gap: 8px; cursor: pointer;">
                        <input type="radio" name="editor-bg-source" class="field-bg-source" value="video">
                        <span>Video slide</span>
                    </label>
                    <label class="om-field editor-bg-video-wrap" hidden>
                        <span>Saved video slide</span>
                        <select class="om-select field-bg-video"><option value="">(pick a video)</option></select>
                    </label>
                    <div class="editor-bg-generate" hidden>
                        <label class="om-field">
                            <span>Generate a new background (10-30s)</span>
                            <input type="text" class="om-input field-bg-generate-prompt"
                                   placeholder="abstract gradient, minimal, signage-friendly"
                                   maxlength="4000">
                        </label>
                        <button type="button" class="om-btn ghost bg-generate-btn" style="margin-top: 8px;">Generate…</button>
                        <p class="bg-generate-status" role="status" aria-live="polite" style="margin: 8px 0 0; font-family: var(--om-mono); font-size: 11.5px; color: var(--om-text-dim);"></p>
                    </div>
                </div>
            </div>

            <p class="om-save-status editor-status" role="status" aria-live="polite" data-state="idle"></p>
            <p style="margin: 4px 0 0; font-family: var(--om-mono); font-size: 11px; color: var(--om-text-fade); text-align: center;">
                <kbd>Esc</kbd> in the text field to clear.
            </p>
        </form>
    </div>
`;

/**
 * Mount the text-slide editor.
 *
 * @param {HTMLElement} container — parent (emptied + replaced).
 * @param {object} options
 * @param {number} options.width  — sign width in pixels.
 * @param {number} options.height — sign height in pixels.
 * @param {(payload) => Promise<any>} options.onSave — called for NEW
 *     slides; payload is the TextSlideUpload shape.
 * @param {(id, payload) => Promise<any>} [options.onSaveExisting] — called
 *     for edit-mode saves. When omitted the editor still works but any
 *     edit attempt falls back to onSave (create-new).
 * @param {() => Promise<Array>} [options.fetchItems] — populates the
 *     background-slide dropdown with available content items. Omit to
 *     disable the "From saved slide" picker.
 * @param {({prompt}) => Promise<any>} [options.onGenerateBackground] —
 *     optional hook for the free AI background generator. When provided,
 *     a Generate… control surfaces in the background fieldset; on
 *     success the returned ImageSlide becomes the active background.
 * @returns {{ loadForEdit: (slide) => Promise<void> }}
 *     caller-facing handle so the playlist-track pallet can wire a
 *     click-to-edit affordance.
 */
export function mountEditor(
    container,
    { width, height, onSave, onSaveExisting, fetchItems, onGenerateBackground },
) {
    container.innerHTML = EDITOR_TEMPLATE;

    const canvas = container.querySelector(".editor-canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = `${width} / ${height}`;

    const textEl = container.querySelector(".field-text");
    const textColorEl = container.querySelector(".field-text-color");
    const bgColorEl = container.querySelector(".field-bg-color");
    const fontFamilyEl = container.querySelector(".field-font-family");
    const bgSlideEl = container.querySelector(".field-bg-slide");
    const bgSlideWrapEl = container.querySelector(".editor-bg-slide-wrap");
    const bgVideoEl = container.querySelector(".field-bg-video");
    const bgVideoWrapEl = container.querySelector(".editor-bg-video-wrap");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const fontSizeEl = container.querySelector(".field-font-size");
    const autoModeEl = container.querySelector(".field-auto-mode");
    const autoModeHintEl = container.querySelector(".field-auto-mode-hint");
    const autoFormatEl = container.querySelector(".field-auto-format");
    const autoFormatWrapEl = container.querySelector(".field-auto-format-wrap");
    const form = container.querySelector(".controls");
    const statusEl = container.querySelector(".editor-status");

    for (const f of FONT_FAMILIES) {
        const opt = document.createElement("option");
        opt.value = f.value;
        opt.textContent = f.label;
        fontFamilyEl.appendChild(opt);
    }
    setupFontPicker(container);
    fontSizeEl.value = String(pickFontSizePct());

    const state = {
        name: nameEl.value,
        text: "",
        textColor: textColorEl.value,
        backgroundColor: bgColorEl.value,
        fontSizePct: Number(fontSizeEl.value),
        fontFamily: fontFamilyEl.value,
        bgSource: "color",
        bgSlideId: null,
        bgVideoId: null,
        bgImage: null, // decoded <img> for "slide" or "video" preview (video uses its thumbnail)
        // Edit-mode tracking: when non-null, Save dispatches to
        // onSaveExisting(editingId, payload) instead of onSave.
        editingId: null,
    };

    function syncAndRender() {
        state.name = nameEl.value;
        state.text = textEl.value;
        state.textColor = textColorEl.value;
        state.backgroundColor = bgColorEl.value;
        state.fontFamily = fontFamilyEl.value;
        // Auto-mode tokens (time / date / day / weather …): the canvas
        // shows the current formatted value so the preview matches what
        // the device renders at playout (B6, qarl 2026-04-29). Saved
        // asset gets the same value frozen at save-moment; auto_render
        // overlays a live composite at playback time so the on-disk
        // freeze is invisible.
        state.autoMode = autoModeEl.value || null;
        state.autoFormat = state.autoMode ? autoFormatEl.value || null : null;
        const parsedSize = Number(fontSizeEl.value);
        if (Number.isFinite(parsedSize) && parsedSize > 0) {
            state.fontSizePct = parsedSize;
        }
        drawCanvas(canvas, state);
    }

    for (const el of [
        textEl,
        textColorEl,
        bgColorEl,
        nameEl,
        fontSizeEl,
        fontFamilyEl,
    ]) {
        el.addEventListener("input", syncAndRender);
    }

    // When the user picks a bundled @font-face family that hasn't finished
    // downloading yet, canvas draws with a fallback glyph set (serif) until
    // the TTF resolves. Kick off an explicit load on selection and redraw
    // once it's ready — so the preview catches up without the user having
    // to touch another field.
    fontFamilyEl.addEventListener("change", async () => {
        const family = fontFamilyEl.value;
        const weight = FONT_WEIGHT_BY_VALUE.get(family) ?? 700;
        if (document.fonts?.load) {
            try {
                await document.fonts.load(`${weight} 40px ${cssFontFamily(family)}`);
            } catch {
                // Load failures bubble back as the fallback rendering;
                // no need to surface — the draw-on-input path already ran.
                return;
            }
            if (state.fontFamily === family) syncAndRender();
        }
    });

    // Mode → list of [value, label] pairs for the format dropdown.
    // Labels include an example so the operator knows exactly what the
    // saved slide will render at playback time.
    const AUTO_FORMAT_OPTIONS = {
        time: [
            ["time_hm", "HH:MM — 14:30"],
            ["time_hms", "HH:MM:SS — 14:30:45"],
        ],
        date: [
            ["date_iso", "YYYY-MM-DD — 2026-04-21"],
            ["date_long", "Long — April 21, 2026"],
            ["date_medium", "Medium — Apr 21"],
        ],
        day: [
            ["day_long", "Full — Monday"],
            ["day_short", "Short — Mon"],
        ],
    };

    function populateAutoFormatOptions(mode, selected = null) {
        autoFormatEl.innerHTML = "";
        const options = AUTO_FORMAT_OPTIONS[mode] || [];
        for (const [value, label] of options) {
            const opt = document.createElement("option");
            opt.value = value;
            opt.textContent = label;
            if (value === selected) opt.selected = true;
            autoFormatEl.appendChild(opt);
        }
        autoFormatWrapEl.hidden = options.length === 0;
    }

    autoModeEl.addEventListener("change", () => {
        autoModeHintEl.hidden = !autoModeEl.value;
        populateAutoFormatOptions(autoModeEl.value);
        // B6: flip the canvas immediately so the preview shows the
        // current formatted token (or the operator's text when auto_mode
        // is cleared) without waiting for the next input event.
        syncAndRender();
    });
    autoFormatEl.addEventListener("change", () => {
        syncAndRender();
    });

    // Background-source radios toggle the slide picker. When "slide" or
    // "video" is selected, populate the dropdown lazily (first time only)
    // via fetchItems so a first-mount doesn't burn a fetch on an operator
    // who's going to stick with solid-color anyway.
    const bgGenerateWrap = container.querySelector(".editor-bg-generate");
    let bgSlidePopulated = false;
    let bgVideoPopulated = false;
    for (const radio of container.querySelectorAll(".field-bg-source")) {
        radio.addEventListener("change", async () => {
            state.bgSource = radio.value;
            bgSlideWrapEl.hidden = state.bgSource !== "slide";
            bgVideoWrapEl.hidden = state.bgSource !== "video";
            // The AI-generate-background flow only makes sense for the
            // image-slide path (it produces ImageSlides). Hide for
            // color + video.
            bgGenerateWrap.hidden =
                state.bgSource !== "slide" || !onGenerateBackground;
            if (state.bgSource === "slide" && fetchItems && !bgSlidePopulated) {
                await populateBgSlideOptions(bgSlideEl, fetchItems, statusEl);
                bgSlidePopulated = true;
            }
            if (state.bgSource === "video" && fetchItems && !bgVideoPopulated) {
                await populateBgVideoOptions(bgVideoEl, fetchItems, statusEl);
                bgVideoPopulated = true;
            }
            // Clear references for the inactive paths so the save payload
            // never carries both a bg image and a bg video (the backend's
            // mutual-exclusion validator would reject — see
            // content/__init__.py::TextSlide::_bg_layers_are_exclusive).
            if (state.bgSource === "color") {
                state.bgImage = null;
                state.bgSlideId = null;
                state.bgVideoId = null;
            } else if (state.bgSource === "slide") {
                state.bgVideoId = null;
            } else if (state.bgSource === "video") {
                state.bgSlideId = null;
            }
            syncAndRender();
        });
    }

    // Generate-a-background flow. Drops a fresh ImageSlide into the
    // catalog (the provider-pluggable /api/backgrounds/generate endpoint
    // handles that), then selects it as this slide's background so the
    // operator sees the result immediately without a second click.
    if (onGenerateBackground) {
        const generateBtn = container.querySelector(".bg-generate-btn");
        const generatePromptEl = container.querySelector(".field-bg-generate-prompt");
        const generateStatusEl = container.querySelector(".bg-generate-status");
        generateBtn.addEventListener("click", async () => {
            const prompt = generatePromptEl.value.trim();
            if (!prompt) {
                generateStatusEl.textContent = "Type a prompt first.";
                return;
            }
            generateBtn.disabled = true;
            generateStatusEl.textContent = "Generating… (can take 10-30 seconds)";
            try {
                const slide = await onGenerateBackground({ prompt });
                if (fetchItems) {
                    await populateBgSlideOptions(bgSlideEl, fetchItems, statusEl);
                    bgSlidePopulated = true;
                }
                bgSlideEl.value = String(slide.id);
                bgSlideEl.dispatchEvent(new Event("change"));
                generatePromptEl.value = "";
                generateStatusEl.textContent = `Generated: ${slide.name}`;
            } catch (err) {
                generateStatusEl.textContent = `${err.message}`;
            } finally {
                generateBtn.disabled = false;
            }
        });
    }
    bgSlideEl.addEventListener("change", async () => {
        state.bgSlideId = bgSlideEl.value || null;
        state.bgImage = state.bgSlideId
            ? await loadImageForSlide(state.bgSlideId).catch(() => null)
            : null;
        syncAndRender();
    });

    bgVideoEl.addEventListener("change", async () => {
        // For the editor's preview canvas, render the video's THUMBNAIL
        // (asset.png at /api/content/{id}/asset) as a static bg under the
        // text. Live moving-video preview lives in the playlist panel's
        // inline-preview, not here — the editor stays a single static
        // canvas for predictable rasterize-on-save output. The stored
        // PNG that ships to the device carries the thumbnail-as-bg too;
        // playback's compositing path replaces it with live frames.
        state.bgVideoId = bgVideoEl.value || null;
        state.bgImage = state.bgVideoId
            ? await loadImageForSlide(state.bgVideoId).catch(() => null)
            : null;
        syncAndRender();
    });

    form.addEventListener("keydown", (event) => {
        // Plain Enter inside a single-line <input> would otherwise submit
        // the form (browser default). Suppress unless focus is in the
        // <textarea>, where Enter means "newline" and should stay. We no
        // longer have a submit button, but Enter on a name/duration field
        // would otherwise trigger an implicit submit attempt.
        if (event.key === "Enter" && event.target?.tagName !== "TEXTAREA") {
            event.preventDefault();
        }
    });

    textEl.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            event.preventDefault();
            textEl.value = "";
            syncAndRender();
        }
    });

    container.querySelectorAll(".preset").forEach((btn) => {
        btn.addEventListener("click", () => {
            const preset = PRESETS[Number(btn.dataset.presetIndex)];
            if (!preset) return;
            textColorEl.value = preset.text;
            bgColorEl.value = preset.bg;
            textColorEl.dispatchEvent(new Event("input", { bubbles: true }));
            bgColorEl.dispatchEvent(new Event("input", { bubbles: true }));
        });
    });

    async function performSave() {
        // Make sure any pending @font-face bytes have loaded before we
        // rasterize — otherwise the stored PNG might fall back to a
        // default font while the live preview already has the real one.
        if (document.fonts?.ready) await document.fonts.ready;
        // Rasterize the asset at a fixed 4K target so the stored PNG
        // is resolution-independent — playback cover-fits down to the
        // current panel dims at slide entry.
        const png_base64 = rasterizeAtTarget(state);
        const durationSeconds = Number(durationEl.value) || 5;
        const payload = {
            name: state.name || "Untitled",
            text: state.text,
            text_color: state.textColor.toUpperCase(),
            background_color: state.backgroundColor.toUpperCase(),
            font_family: state.fontFamily,
            font_size_pct: state.fontSizePct,
            background_image_slide_id: state.bgSlideId || null,
            background_video_slide_id: state.bgVideoId || null,
            auto_mode: autoModeEl.value || null,
            auto_format: autoModeEl.value ? autoFormatEl.value || null : null,
            duration_ms: Math.round(durationSeconds * 1000),
            png_base64,
        };
        const wasEdit = Boolean(state.editingId);
        const result = wasEdit && onSaveExisting
            ? await onSaveExisting(state.editingId, payload)
            : await onSave(payload);
        // Promote a freshly-created slide so subsequent auto-saves PATCH
        // the same id instead of creating a twin on every keystroke.
        if (!wasEdit && result?.id) {
            state.editingId = String(result.id);
        }
        if (browser && state.editingId) browser.highlight(state.editingId);
    }

    const autoSave = attachAutoSave(form, {
        save: performSave,
        status: statusEl,
        // Create-mode requires non-empty text to bother saving (otherwise
        // an empty form auto-creates a junk slide on first focus). Edit
        // mode allows empty text — the operator is intentionally clearing
        // a saved slide and the PATCH must reach the server.
        canSave: () => Boolean(state.editingId) || state.text.trim().length > 0,
        debounceMs: 900,
    });

    async function resetToBlank() {
        // Sync blank-state setup. Anything here can be safely
        // overridden by a loadForEdit that interleaves later.
        state.editingId = null;
        state.bgImage = null;
        state.bgSlideId = null;
        state.bgVideoId = null;
        textEl.value = "";
        // Safari quirk: after a textarea held content and got cleared,
        // the placeholder renders inside a stale narrow box and clips
        // mid-glyph until the user clicks into it. Focus() + blur()
        // aren't enough; the only reliable nudge is to detach and
        // re-attach the element so Safari drops the cached layout.
        // Chromium no-ops either way. Event listeners survive a
        // detach/re-attach cycle.
        const parent = textEl.parentNode;
        if (parent) {
            const next = textEl.nextSibling;
            parent.removeChild(textEl);
            parent.insertBefore(textEl, next);
        }
        autoModeEl.value = "";
        autoModeHintEl.hidden = true;
        populateAutoFormatOptions("");
        const colorRadio = container.querySelector(
            '.field-bg-source[value="color"]',
        );
        colorRadio.checked = true;
        bgSlideWrapEl.hidden = true;
        bgVideoWrapEl.hidden = true;
        state.bgSource = "color";
        syncAndRender();
        // Form is being cleared, not edited — drop any pending save.
        autoSave.cancel();
        statusEl.textContent = "";
        statusEl.dataset.state = "idle";

        // Async tail: gap-filled default name + browser refresh, both
        // no-ops if loadForEdit took ownership during the await.
        const defaultName = await computeDefaultName();
        if (state.editingId !== null) return;
        nameEl.value = defaultName;
        if (browser) {
            await browser.refresh();
            browser.highlight(null);
        }
    }

    async function computeDefaultName() {
        if (!fetchItems) return "Text Slide 1";
        try {
            const items = await fetchItems();
            return nextAutoName(
                items.filter((i) => i.type === "text_slide"),
                "Text Slide",
            );
        } catch {
            return "Text Slide 1";
        }
    }

    /**
     * +New flow: create a server-side slide IMMEDIATELY (so the operator
     * sees a fresh tile in the pallet) and drop into edit mode against it.
     * The slide is seeded with the auto-name as the placeholder text so
     * the backend's "text non-empty" validation passes — operators
     * typically replace it with their first keystroke.
     */
    async function createNew() {
        await resetToBlank();
        // Seed text with a single space so the backend's "non-empty"
        // create-mode validation passes WITHOUT painting a literal "Text
        // Slide N" on the device if the operator wanders off mid-create.
        // First keystroke replaces it.
        textEl.value = " ";
        syncAndRender();
        try {
            await performSave();
        } catch (err) {
            statusEl.textContent = `Could not create slide: ${err?.message || err}`;
            statusEl.dataset.state = "error";
            return;
        }
        // performSave promotes editingId on success; refresh the browser
        // so the new tile appears (and stays highlighted via the
        // browser.highlight call performSave already did).
        if (browser) await browser.refresh();
    }

    async function loadForEdit(slide) {
        if (!slide || slide.type !== "text_slide") {
            statusEl.textContent =
                "Only text slides are editable — delete + re-upload for images or videos.";
            return;
        }
        state.editingId = String(slide.id);
        if (browser) browser.highlight(slide.id);

        nameEl.value = slide.name || "Untitled";
        textEl.value = slide.text || "";
        textColorEl.value = slide.text_color || "#FFFFFF";
        bgColorEl.value = slide.background_color || "#000000";
        fontFamilyEl.value = slide.font_family || "sans-serif";
        fontFamilyEl.dispatchEvent(new Event("font-picker-sync"));
        // Prefer the new pct field. Old slides only carry font_size_px;
        // back-derive a percent so re-editing migrates them in place.
        const pct =
            slide.font_size_pct ??
            (slide.font_size_px
                ? (slide.font_size_px / width) * 100
                : pickFontSizePct());
        fontSizeEl.value = String(pct);
        durationEl.value = String(Math.max(1, (slide.duration_ms || 5000) / 1000));
        autoModeEl.value = slide.auto_mode || "";
        autoModeHintEl.hidden = !slide.auto_mode;
        populateAutoFormatOptions(slide.auto_mode || "", slide.auto_format || null);

        if (slide.background_image_slide_id) {
            // Switch to "slide" background and select the referenced image.
            const slideRadio = container.querySelector(
                '.field-bg-source[value="slide"]',
            );
            slideRadio.checked = true;
            if (fetchItems && !bgSlidePopulated) {
                await populateBgSlideOptions(bgSlideEl, fetchItems, statusEl);
                bgSlidePopulated = true;
            }
            bgSlideWrapEl.hidden = false;
            bgVideoWrapEl.hidden = true;
            bgSlideEl.value = String(slide.background_image_slide_id);
            state.bgSource = "slide";
            state.bgSlideId = String(slide.background_image_slide_id);
            state.bgVideoId = null;
            state.bgImage = await loadImageForSlide(state.bgSlideId).catch(
                () => null,
            );
        } else if (slide.background_video_slide_id) {
            // Switch to "video" background and select the referenced video.
            const videoRadio = container.querySelector(
                '.field-bg-source[value="video"]',
            );
            videoRadio.checked = true;
            if (fetchItems && !bgVideoPopulated) {
                await populateBgVideoOptions(bgVideoEl, fetchItems, statusEl);
                bgVideoPopulated = true;
            }
            bgSlideWrapEl.hidden = true;
            bgVideoWrapEl.hidden = false;
            bgVideoEl.value = String(slide.background_video_slide_id);
            state.bgSource = "video";
            state.bgSlideId = null;
            state.bgVideoId = String(slide.background_video_slide_id);
            // For the editor's static preview, load the video's thumbnail
            // (asset.png at /api/content/{id}/asset). Live frame compositing
            // happens in the playlist panel's inline-preview, not here.
            state.bgImage = await loadImageForSlide(state.bgVideoId).catch(
                () => null,
            );
        } else {
            const colorRadio = container.querySelector(
                '.field-bg-source[value="color"]',
            );
            colorRadio.checked = true;
            bgSlideWrapEl.hidden = true;
            bgVideoWrapEl.hidden = true;
            state.bgSource = "color";
            state.bgSlideId = null;
            state.bgVideoId = null;
            state.bgImage = null;
        }
        syncAndRender();

        // Bundled @font-face fonts load lazily — on the FIRST loadForEdit
        // for a slide that uses one (e.g. Pacifico), the canvas paints
        // with the fallback before the .ttf finishes downloading. Wait
        // for the font, give the browser a paint cycle (canvas keeps a
        // separate font cache that lags behind document.fonts.load by
        // a tick), then re-render. document.fonts.ready settles AFTER
        // all pending fonts are usable for canvas drawing on every
        // current browser engine.
        const family = fontFamilyEl.value;
        if (family && document.fonts?.load) {
            const weight = FONT_WEIGHT_BY_VALUE.get(family) ?? 700;
            try {
                await document.fonts.load(`${weight} 40px ${cssFontFamily(family)}`);
                if (document.fonts?.ready) await document.fonts.ready;
                await new Promise((resolve) =>
                    requestAnimationFrame(() => resolve()),
                );
            } catch {
                return;
            }
            if (state.fontFamily === family && state.editingId === String(slide.id)) {
                syncAndRender();
            }
        }
        // Loading is not an edit — drop any auto-save scheduled by the
        // field mutations above.
        autoSave.cancel();
    }

    // Mount the slide browser at the top of the subpage — each tile
    // click dispatches loadForEdit, "+ New" → resetToBlank. We do this
    // after all callbacks are defined so they can reference each other.
    let browser = null;
    if (fetchItems) {
        browser = mountSlideBrowser(
            container.querySelector(".slide-browser-slot"),
            {
                type: "text_slide",
                fetchItems,
                onSelect: (item) => loadForEdit(item),
                onCreate: () => resetToBlank(),
            },
        );
    }

    // Initial state: prefer editing the most-recent existing slide of
    // this type. The operator's expectation is "open the editor → see
    // something to edit," not "see a blank create form." If there are no
    // saved slides yet (fresh device), fall back to a blank-create form.
    // +New explicitly resets to blank.
    (async () => {
        let firstItem = null;
        if (fetchItems) {
            try {
                const items = await fetchItems();
                firstItem = items
                    .filter((it) => it.type === "text_slide")
                    .sort((a, b) =>
                        String(b.created_at || "").localeCompare(
                            String(a.created_at || ""),
                        ),
                    )[0] || null;
            } catch {
                // fall through to blank
            }
        }
        if (firstItem) {
            await loadForEdit(firstItem);
        } else {
            resetToBlank();
            syncAndRender();
        }
    })();

    return {
        loadForEdit,
        reset: resetToBlank,
        createNew,
        refreshBrowser: () => browser?.refresh(),
        // Test hook: drains any pending debounced auto-save synchronously
        // so assertions don't have to race the timer. Production code
        // should not rely on this — auto-save is debounced for a reason.
        flushAutoSave: () => autoSave.flush(),
    };
}

export async function populateBgSlideOptions(selectEl, fetchItems, statusEl) {
    try {
        const items = await fetchItems();
        selectEl.innerHTML = '<option value="">(pick a slide)</option>';
        // The image-slide bg path filters to ImageSlides only — VideoSlides
        // get their own picker (populateBgVideoOptions) since the
        // playback-time compositing path is different (§5.10) and the
        // editor stores them in a separate field for the mutual-exclusion
        // validator.
        for (const item of items) {
            if (item.type !== "image") continue;
            const opt = document.createElement("option");
            opt.value = String(item.id);
            opt.textContent = item.name || item.text || "Untitled";
            selectEl.appendChild(opt);
        }
    } catch (err) {
        statusEl.textContent = `Could not load slides: ${err.message}`;
    }
}

/**
 * Populate the video-bg dropdown with VideoSlide entries. Phase 5b: the
 * editor's bg-picker can pick a saved VideoSlide as the background; the
 * device composites text over the live video frames at playback per
 * SYSTEM_SPEC §5.10. The editor's preview canvas uses the video's
 * thumbnail (asset.png) as a static stand-in — the playlist panel's
 * inline-preview is where the operator sees moving frames.
 */
export async function populateBgVideoOptions(selectEl, fetchItems, statusEl) {
    try {
        const items = await fetchItems();
        selectEl.innerHTML = '<option value="">(pick a video)</option>';
        for (const item of items) {
            if (item.type !== "video") continue;
            const opt = document.createElement("option");
            opt.value = String(item.id);
            opt.textContent = item.name || "Untitled";
            selectEl.appendChild(opt);
        }
    } catch (err) {
        statusEl.textContent = `Could not load videos: ${err.message}`;
    }
}

function loadImageForSlide(slideId) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error("could not load slide image"));
        img.src = `/api/content/${slideId}/asset`;
    });
}

/**
 * Draw the slide onto `canvas`. Pure in the sense that it only reads
 * `state` and writes pixels — no DOM wiring, no event handlers.
 */
/**
 * Render only the text layer of a TextSlide onto `canvas`, leaving the
 * canvas's background transparent. Used by the inline-preview to overlay
 * text on top of a live video frame for Text-over-Video slides
 * (Phase 5b — SYSTEM_SPEC §5.10).
 *
 * Accepts the on-the-wire ContentItem shape (text, text_color,
 * font_family, font_size_pct, font_size_px) — not the editor's
 * internal `state` — because the inline-preview consumes ContentItem
 * directly from the playlist.
 *
 * @param {HTMLCanvasElement} canvas — sized to the desired output.
 * @param {object} item — TextSlide ContentItem (wire shape).
 */
export function drawTextOnly(canvas, item) {
    const ctx = canvas.getContext("2d");
    const text = item.text || "";
    const textColor = item.text_color || "#FFFFFF";
    const fontSize = item.font_size_px;
    const fontSizePct = item.font_size_pct;
    const fontFamily = item.font_family || "sans-serif";

    ctx.save();
    try {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        if (!text) return;

        let fontSizePx;
        if (Number.isFinite(fontSizePct) && fontSizePct > 0) {
            fontSizePx = Math.max(4, Math.round((canvas.height * fontSizePct) / 100));
        } else if (Number.isFinite(fontSize) && fontSize > 0) {
            fontSizePx = fontSize;
        } else {
            fontSizePx = pickFontSize(canvas.height);
        }
        ctx.fillStyle = textColor;
        const weight = FONT_WEIGHT_BY_VALUE.get(fontFamily) ?? 700;
        ctx.font = `${weight} ${fontSizePx}px ${cssFontFamily(fontFamily)}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";

        const lines = text.split(/\r?\n/);
        const lineHeight = fontSizePx * 1.1;
        const totalHeight = lineHeight * lines.length;
        const startY = canvas.height / 2 - totalHeight / 2 + lineHeight / 2;
        const maxWidth = Math.max(1, canvas.width - 4);
        for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], canvas.width / 2, startY + i * lineHeight, maxWidth);
        }
    } finally {
        ctx.restore();
    }
}


export function drawCanvas(canvas, state) {
    const ctx = canvas.getContext("2d");
    const {
        text: rawText = "",
        textColor = "#FFFFFF",
        backgroundColor = "#000000",
        fontSize,
        fontSizePct,
        fontFamily = "sans-serif",
        bgSource = "color",
        bgImage = null,
        autoMode = null,
        autoFormat = null,
    } = state;
    // Auto-mode slides surface the current formatted value (time / date /
    // day token) so the preview matches what the device renders at
    // playout. Operator's typed text becomes a fallback shown only when
    // auto_mode is unset (B6, qarl 2026-04-29).
    const text = autoMode
        ? formatAutoText(autoMode, autoFormat, new Date()) || rawText
        : rawText;

    ctx.save();
    try {
        // Background layer: image (cover-fit) or solid color. Cover-fit
        // keeps the saved PNG matching what plays — letterbox bars or a
        // stretch would both show up on the device.
        if (bgSource === "slide" && bgImage) {
            const scale = Math.max(
                canvas.width / bgImage.width,
                canvas.height / bgImage.height,
            );
            const drawW = bgImage.width * scale;
            const drawH = bgImage.height * scale;
            ctx.drawImage(
                bgImage,
                (canvas.width - drawW) / 2,
                (canvas.height - drawH) / 2,
                drawW,
                drawH,
            );
        } else {
            ctx.fillStyle = backgroundColor;
            ctx.fillRect(0, 0, canvas.width, canvas.height);
        }

        if (!text) return;

        let fontSizePx;
        if (Number.isFinite(fontSizePct) && fontSizePct > 0) {
            fontSizePx = Math.max(4, Math.round((canvas.height * fontSizePct) / 100));
        } else if (Number.isFinite(fontSize) && fontSize > 0) {
            fontSizePx = fontSize;
        } else {
            fontSizePx = pickFontSize(canvas.height);
        }
        ctx.fillStyle = textColor;
        const weight = FONT_WEIGHT_BY_VALUE.get(fontFamily) ?? 700;
        ctx.font = `${weight} ${fontSizePx}px ${cssFontFamily(fontFamily)}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";

        const lines = text.split(/\r?\n/);
        const lineHeight = fontSizePx * 1.1;
        const totalHeight = lineHeight * lines.length;
        const startY = canvas.height / 2 - totalHeight / 2 + lineHeight / 2;
        const maxWidth = Math.max(1, canvas.width - 4);
        for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], canvas.width / 2, startY + i * lineHeight, maxWidth);
        }
    } finally {
        ctx.restore();
    }
}

export function pickFontSize(panelHeight) {
    return Math.max(12, Math.floor(panelHeight * 0.4));
}

// Default percent-of-width for a brand-new auto-mode-less text slide.
// 30% reads cleanly as a single-word slogan on common 4:3 / 16:9 panels;
// the operator can dial in something more specific from the field.
export function pickFontSizePct() {
    return 30;
}

export function canvasToBase64(canvas) {
    const dataUrl = canvas.toDataURL("image/png");
    return dataUrl.split(",")[1];
}

/**
 * Render the editor scene onto a fresh offscreen 4K canvas and return
 * its base64 PNG body. Decouples the saved asset from the on-screen
 * preview canvas (which stays at panel dims for visual fidelity).
 */
export function rasterizeAtTarget(state) {
    const off = document.createElement("canvas");
    off.width = RASTERIZE_W;
    off.height = RASTERIZE_H;
    drawCanvas(off, state);
    return canvasToBase64(off);
}
