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
//
// §5.10a v3 (qarl 2026-05-01): TextSlide carries a `text_layers` list.
// Each layer has its own text / font / color / dynamic-text / box. The
// editor surfaces one **layer group** per layer (with its own field
// panel), drag-reorderable; the box overlay attaches to the focused
// (selected) layer.

import Sortable from "sortablejs";

import { attachAutoSave } from "./auto-save.js";
import { formatAutoText } from "./auto-format.js";
import {
    anyLayerAnimated,
    paintLayerWithMotion,
} from "./canvas-motion.js";
import { mountSlideBrowser, nextAutoName } from "./slide-browser.js";

const RASTERIZE_W = 3840;
const RASTERIZE_H = 2160;


// `weight` = numeric CSS font-weight to request in `ctx.font`. Using
// each face's *native* weight avoids browser-synthesized fake bold on
// single-weight display fonts (Pacifico, Bebas Neue, etc.).
export const FONT_FAMILIES = [
    { value: "sans-serif",            label: "Sans-serif",            category: "System",     weight: 700 },
    { value: "serif",                 label: "Serif",                 category: "System",     weight: 700 },
    { value: "monospace",             label: "Monospace",             category: "System",     weight: 700 },
    { value: "Inter",                 label: "Inter",                 category: "Block",      weight: 700 },
    { value: "Oswald",                label: "Oswald",                category: "Block",      weight: 700 },
    { value: "Bebas Neue",            label: "Bebas Neue",            category: "Block",      weight: 400 },
    { value: "Bowlby One SC",         label: "Bowlby One SC",         category: "Block",      weight: 400 },
    { value: "Anton",                 label: "Anton",                 category: "Block",      weight: 400 },
    { value: "Archivo Black",         label: "Archivo Black",         category: "Block",      weight: 400 },
    { value: "Alfa Slab One",         label: "Alfa Slab One",         category: "Block",      weight: 400 },
    { value: "Roboto Slab",           label: "Roboto Slab",           category: "Serif",      weight: 700 },
    { value: "Cinzel",                label: "Cinzel",                category: "Serif",      weight: 700 },
    { value: "Playfair Display",      label: "Playfair Display",      category: "Serif",      weight: 700 },
    { value: "DM Serif Display",      label: "DM Serif Display",      category: "Serif",      weight: 400 },
    { value: "UnifrakturCook",        label: "UnifrakturCook",        category: "Serif",      weight: 700 },
    { value: "VT323",                 label: "VT323",                 category: "Mono",       weight: 400 },
    { value: "JetBrains Mono",        label: "JetBrains Mono",        category: "Mono",       weight: 700 },
    { value: "Space Mono",            label: "Space Mono",            category: "Mono",       weight: 700 },
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

/**
 * Wire up the visual font picker around a layer's hidden <select> +
 * trigger button + popover. Idempotent per `layerEl`: each layer gets
 * its own popover wired once at insert time.
 */
function setupFontPicker(layerEl) {
    const selectEl = layerEl.querySelector(".field-font-family");
    const trigger = layerEl.querySelector(".font-picker-trigger");
    const triggerLabel = layerEl.querySelector(".font-picker-trigger-label");
    const popover = layerEl.querySelector(".font-picker-popover");
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
        // syncAndRender wires `input`, font-load-then-redraw wires
        // `change`. Dispatching only `change` leaves the live preview
        // stale until the next input on any field.
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

    selectEl.addEventListener("change", syncTrigger);
    selectEl.addEventListener("font-picker-sync", syncTrigger);
    syncTrigger();
}

/**
 * Return a CSS font-family string safe for use in `ctx.font` / `style.font`.
 * Generic keywords (sans-serif, serif, monospace) must NOT be quoted; named
 * families with spaces must be. Canvas will silently drop an unquoted
 * "Bebas Neue" otherwise.
 *
 * Appends "Noto Color Emoji" as the canvas-side fallback so the browser
 * picks emoji glyphs from the bundled color-emoji TTF (loaded via
 * @font-face in styles.css, populated at build time by
 * scripts/download-emoji-font.sh) rather than the system's default
 * emoji font. Pairs with the device-side codepoint segmentation in
 * backend/openmarquee/seed.py:_draw_text_runs so editor preview and
 * device output use the same color glyphs.
 */
function cssFontFamily(value) {
    const GENERICS = new Set([
        "sans-serif", "serif", "monospace", "cursive", "fantasy",
        "system-ui", "ui-sans-serif", "ui-serif", "ui-monospace",
    ]);
    const primary = GENERICS.has(value) ? value : `"${value}"`;
    return `${primary}, "Noto Color Emoji"`;
}

const EDITOR_TEMPLATE = `
    <div class="editor">
        <div class="slide-browser-slot"></div>
        <form class="controls" autocomplete="off">
            <div class="editor-cols">
            <div class="preview-wrap">
                <div class="editor-canvas-stack">
                    <canvas class="editor-canvas" aria-label="slide preview"></canvas>
                    <div class="editor-box-overlay" aria-hidden="true">
                        <div class="editor-box-handle" data-handle="nw"></div>
                        <div class="editor-box-handle" data-handle="n"></div>
                        <div class="editor-box-handle" data-handle="ne"></div>
                        <div class="editor-box-handle" data-handle="e"></div>
                        <div class="editor-box-handle" data-handle="se"></div>
                        <div class="editor-box-handle" data-handle="s"></div>
                        <div class="editor-box-handle" data-handle="sw"></div>
                        <div class="editor-box-handle" data-handle="w"></div>
                    </div>
                </div>
            </div>
            <div class="editor-form-stack">
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

            <div class="om-card editor-layers">
                <div class="om-row" style="align-items: center; justify-content: space-between; margin-bottom: 10px;">
                    <div class="om-eyebrow" style="font-family: var(--om-mono); letter-spacing: 0.14em; font-size: 10.5px; color: var(--om-text-fade); text-transform: uppercase;">Layers</div>
                    <button type="button" class="om-btn ghost editor-add-layer">+ New layer</button>
                </div>
                <div class="editor-layers-list"></div>
            </div>

            <div class="om-card editor-bg-picker">
                <div class="om-eyebrow" style="margin-bottom: 10px; font-family: var(--om-mono); letter-spacing: 0.14em; font-size: 10.5px; color: var(--om-text-fade); text-transform: uppercase;">Background source</div>
                <div class="om-stack" style="gap: 10px;">
                    <label class="om-row" style="gap: 8px; cursor: pointer;">
                        <input type="radio" name="editor-bg-source" class="field-bg-source" value="color" checked>
                        <span>Solid color</span>
                    </label>
                    <label class="om-field editor-bg-color-wrap">
                        <span>Background color</span>
                        <input type="color" class="field-bg-color" value="#000000" style="width: 100%; height: 40px; border-radius: 9px; border: 1px solid var(--om-line); background: var(--om-surface-2);">
                    </label>
                    <label class="om-row" style="gap: 8px; cursor: pointer;">
                        <input type="radio" name="editor-bg-source" class="field-bg-source" value="gradient">
                        <span>Gradient</span>
                    </label>
                    <div class="editor-bg-gradient-wrap" hidden>
                        <div class="om-row" style="gap: 10px;">
                            <label class="om-field" style="flex: 1;">
                                <span>Start color</span>
                                <input type="color" class="field-bg-grad-start" value="#FF6B6B" style="width: 100%; height: 40px; border-radius: 9px; border: 1px solid var(--om-line); background: var(--om-surface-2);">
                            </label>
                            <label class="om-field" style="flex: 1;">
                                <span>End color</span>
                                <input type="color" class="field-bg-grad-end" value="#4ECDC4" style="width: 100%; height: 40px; border-radius: 9px; border: 1px solid var(--om-line); background: var(--om-surface-2);">
                            </label>
                        </div>
                        <label class="om-field" style="margin-top: 8px;">
                            <span>Angle (<span class="field-bg-grad-angle-label">0</span>°)</span>
                            <input type="range" class="field-bg-grad-angle" min="0" max="359" step="1" value="0" style="width: 100%;">
                        </label>
                    </div>
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
                <kbd>Esc</kbd> in a text field to clear.
            </p>
            </div>  <!-- /.editor-form-stack -->
            </div>  <!-- /.editor-cols -->
        </form>
    </div>
`;

// Quick text-color swatches for the per-layer color row (§5.10a v3.1
// accordion-editor handoff). Per-layer COLOR ONLY — bg color stayed
// slide-level and lives in the Background-source card. Pulls the same
// nine values the design ref uses (handoff/reference/app/layer-variants.jsx).
const LAYER_QUICK_COLORS = [
    "#FFFFFF", "#FFB43C", "#FF5FA7", "#5AF095", "#5FD5FF",
    "#FF5A3C", "#A06CFF", "#1A1610", "#F4ECD8",
];

function quickColorSwatchesHtml() {
    return LAYER_QUICK_COLORS.map(
        (c) => `<button type="button" class="editor-color-swatch" data-color="${c}"
                  aria-label="${c}" title="${c}"
                  style="background:${c};"></button>`,
    ).join("");
}

// Per-layer accordion card. Inserted dynamically into .editor-layers-list,
// one element per layer in state.layers. The header row is always visible;
// the body is only rendered open for the *expanded* layer (one-open-at-
// a-time accordion).
//
// Field selectors stay class-based (.field-text, .field-text-color,
// .field-font-family, etc.) so per-layer querySelector under the group
// root finds the right control. The slide-level slide-name field uses
// .field-name (TOP of the editor) — the per-layer name field below is
// .field-layer-name to avoid the collision.
const LAYER_GROUP_TEMPLATE = `
    <header class="editor-layer-head">
        <span class="editor-layer-handle" aria-label="drag to reorder" title="drag to reorder">⋮⋮</span>
        <div class="editor-layer-thumb" aria-hidden="true"></div>
        <div class="editor-layer-titleblock">
            <div class="editor-layer-name-display"></div>
            <div class="editor-layer-meta">
                <span class="editor-layer-meta-swatch" aria-hidden="true"></span>
                <span class="editor-layer-meta-font"></span>
                <span aria-hidden="true">·</span>
                <span class="editor-layer-meta-size"></span>
                <span class="editor-layer-meta-motion-sep" aria-hidden="true" hidden>·</span>
                <span class="editor-layer-meta-motion editor-layer-meta-motion" hidden></span>
            </div>
        </div>
        <button type="button" class="editor-layer-eye" aria-label="toggle visibility" title="toggle visibility">
            <span class="editor-layer-eye-glyph">●</span>
        </button>
        <span class="editor-layer-chevron" aria-hidden="true">▾</span>
    </header>
    <div class="editor-layer-body">
        <label class="om-field">
            <span>Layer name</span>
            <input type="text" class="om-input field-layer-name" placeholder="Headline" maxlength="200">
        </label>
        <label class="om-field">
            <span>Text</span>
            <textarea class="om-textarea field-text" rows="2" placeholder="(enter text here)"></textarea>
        </label>
        <div class="om-field">
            <span>Dynamic Text</span>
            <div class="editor-segmented field-auto-mode-segmented" role="group" aria-label="dynamic source">
                <button type="button" data-value="" aria-pressed="true">Off</button>
                <button type="button" data-value="time" aria-pressed="false">Time</button>
                <button type="button" data-value="date" aria-pressed="false">Date</button>
                <button type="button" data-value="day" aria-pressed="false">Day</button>
            </div>
            <input type="hidden" class="field-auto-mode" value="">
        </div>
        <label class="om-field field-auto-format-wrap" hidden>
            <span>Format</span>
            <select class="om-select field-auto-format"></select>
        </label>
        <p class="field-hint field-auto-mode-hint" hidden style="margin: 0; color: var(--om-text-dim); font-size: 12.5px;">
            When Dynamic Text is set, the typed text is a preview-only
            fallback — the device re-renders each second at playback
            time using the configured timezone.
        </p>
        <div class="om-field">
            <span>Text color</span>
            <div class="editor-color-row">
                ${quickColorSwatchesHtml()}
                <input type="color" class="field-text-color" value="#FFFFFF">
            </div>
        </div>
        <div class="om-row" style="gap: 10px; align-items: end;">
            <div class="om-field font-picker" style="flex: 1;">
                <span class="font-picker-label">Font</span>
                <select class="om-select field-font-family"></select>
                <button type="button" class="font-picker-trigger" aria-haspopup="listbox" aria-expanded="false">
                    <span class="font-picker-trigger-label">Sans-serif</span>
                    <span class="font-picker-trigger-caret" aria-hidden="true">▾</span>
                </button>
                <div class="font-picker-popover" role="listbox" hidden></div>
            </div>
            <label class="om-field" style="width: 160px;">
                <span>Font size (% of box width) <span class="field-font-size-display"></span></span>
                <input type="range" class="om-range field-font-size" min="8" max="100" step="0.5">
            </label>
        </div>
        <div class="om-row" style="gap: 10px; align-items: end;">
            <label class="om-field" style="flex: 1;">
                <span>Motion</span>
                <select class="om-select field-motion" aria-label="layer motion">
                    <option value="static">Static</option>
                    <option value="ticker">Ticker (horizontal travel)</option>
                    <option value="breathe">Breathe (scale)</option>
                    <option value="pulse">Pulse (alpha)</option>
                    <option value="bounce">Bounce (vertical bob)</option>
                    <option value="shake">Shake (jitter)</option>
                    <option value="blink">Blink (on/off)</option>
                </select>
            </label>
            <button type="button" class="om-btn ghost editor-layer-delete" aria-label="delete layer" title="delete layer" style="color: var(--om-bad); align-self: end; margin-bottom: 1px;">🗑</button>
        </div>
        <div class="om-row field-motion-controls" style="gap: 10px; align-items: end;" hidden>
            <label class="om-field" style="flex: 1;">
                <span>Intensity <span class="field-motion-intensity-display"></span></span>
                <input type="range" class="om-range field-motion-intensity" min="0" max="100" step="1" value="50">
            </label>
            <label class="om-field" style="flex: 1;">
                <span>Phase <span class="field-motion-phase-display"></span></span>
                <input type="range" class="om-range field-motion-phase" min="0" max="1" step="0.01" value="0">
            </label>
        </div>
    </div>
`;

/**
 * Construct an empty layer (default values matching backend TextLayer).
 * §5.10a v3.1: includes the new editor-driven fields (name / motion /
 * visible / etc.) that landed in commit 20cb506.
 */
function defaultLayer() {
    return {
        text: "",
        name: "",
        textColor: "#FFFFFF",
        fontFamily: FONT_FAMILIES[0].value,
        fontSizePct: pickFontSizePct(),
        autoMode: null,
        autoFormat: null,
        motion: "static",
        motionIntensity: 50,
        motionPhase: 0,
        visible: true,
        box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 },
    };
}

/**
 * Smallest unused "Layer N" given the slide's current layer names.
 * Custom-named layers ("Headline", "Hours") don't reserve a slot —
 * deleting "Layer 2" and adding fills back as "Layer 2". Mirrors
 * slide-browser's nextAutoName for the slide-title field. qarl
 * 2026-05-01.
 */
function nextLayerName(layers) {
    const pattern = /^Layer (\d+)$/;
    const used = new Set();
    for (const layer of layers || []) {
        const m = (layer?.name || "").match(pattern);
        if (m) used.add(Number(m[1]));
    }
    let n = 1;
    while (used.has(n)) n += 1;
    return `Layer ${n}`;
}

/**
 * defaultLayer() pre-populated with the next-unused "Layer N" name —
 * used at editor mount, resetToBlank, and the +New affordance so a
 * fresh layer always has a real label.
 */
function makeAutoNamedLayer(existingLayers) {
    const layer = defaultLayer();
    layer.name = nextLayerName(existingLayers);
    return layer;
}

/**
 * Mount the text-slide editor.
 *
 * @param {HTMLElement} container — parent (emptied + replaced).
 * @param {object} options
 * @param {number} options.width  — sign width in pixels.
 * @param {number} options.height — sign height in pixels.
 * @param {(payload) => Promise<any>} options.onSave — called for NEW
 *     slides; payload is the TextSlideUpload shape with text_layers list.
 * @param {(id, payload) => Promise<any>} [options.onSaveExisting] — called
 *     for edit-mode saves.
 * @param {() => Promise<Array>} [options.fetchItems] — populates the
 *     background-slide dropdown with available content items.
 * @param {({prompt}) => Promise<any>} [options.onGenerateBackground] —
 *     optional hook for the AI background generator.
 * @returns {{ loadForEdit: (slide) => Promise<void> }}
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

    const bgColorEl = container.querySelector(".field-bg-color");
    const bgSlideEl = container.querySelector(".field-bg-slide");
    const bgSlideWrapEl = container.querySelector(".editor-bg-slide-wrap");
    const bgVideoEl = container.querySelector(".field-bg-video");
    const bgVideoWrapEl = container.querySelector(".editor-bg-video-wrap");
    const bgColorWrapEl = container.querySelector(".editor-bg-color-wrap");
    const bgGradWrapEl = container.querySelector(".editor-bg-gradient-wrap");
    const bgGradStartEl = container.querySelector(".field-bg-grad-start");
    const bgGradEndEl = container.querySelector(".field-bg-grad-end");
    const bgGradAngleEl = container.querySelector(".field-bg-grad-angle");
    const bgGradAngleLabelEl = container.querySelector(".field-bg-grad-angle-label");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const form = container.querySelector(".controls");
    const statusEl = container.querySelector(".editor-status");
    const layersListEl = container.querySelector(".editor-layers-list");
    const addLayerBtn = container.querySelector(".editor-add-layer");

    const state = {
        name: nameEl.value,
        backgroundColor: bgColorEl.value,
        bgSource: "color",
        bgSlideId: null,
        bgVideoId: null,
        bgImage: null,
        // Gradient-source state. start_color / end_color stay
        // synced with the color inputs; angle_deg comes from the
        // range slider (0-359). Mutex with the other bg sources —
        // bgSource = "gradient" activates this object, otherwise
        // it's ignored at save time.
        bgGradient: {
            start_color: bgGradStartEl.value,
            end_color: bgGradEndEl.value,
            angle_deg: parseInt(bgGradAngleEl.value, 10) || 0,
        },
        layers: [makeAutoNamedLayer([])],
        // Selection drives the box overlay's binding. Expansion drives
        // which accordion card has its body visible. They stay coupled
        // by default (clicking a header sets BOTH to that index) but
        // are tracked separately because the focus-driven selection
        // path (typing in a field) updates the active index without
        // collapsing other cards.
        activeLayerIndex: 0,
        expandedLayerIndex: 0,
        editingId: null,
    };

    const boxOverlay = container.querySelector(".editor-box-overlay");
    const BOX_MIN = 0.1;
    const BOX_MAX = 0.9;
    const DRAG_THRESHOLD_PX = 5;

    function activeLayer() {
        return state.layers[state.activeLayerIndex] || state.layers[0];
    }

    function positionBoxOverlay() {
        if (!boxOverlay) return;
        const layer = activeLayer();
        if (!layer) return;
        boxOverlay.style.left = `${layer.box.x * 100}%`;
        boxOverlay.style.top = `${layer.box.y * 100}%`;
        boxOverlay.style.width = `${layer.box.w * 100}%`;
        boxOverlay.style.height = `${layer.box.h * 100}%`;
    }

    function applyHandleDrag(start, mode, dx, dy) {
        let x = start.x;
        let y = start.y;
        let w = start.w;
        let h = start.h;
        switch (mode) {
            case "move":
                x = start.x + dx; y = start.y + dy;
                break;
            case "n":
                h = start.h - dy;
                break;
            case "s":
                h = start.h + dy;
                break;
            case "e":
                w = start.w + dx;
                break;
            case "w":
                w = start.w - dx;
                break;
            case "nw":
                w = start.w - dx; h = start.h - dy;
                break;
            case "ne":
                w = start.w + dx; h = start.h - dy;
                break;
            case "sw":
                w = start.w - dx; h = start.h + dy;
                break;
            case "se":
                w = start.w + dx; h = start.h + dy;
                break;
        }
        w = Math.min(BOX_MAX, Math.max(BOX_MIN, w));
        h = Math.min(BOX_MAX, Math.max(BOX_MIN, h));
        // Recompute the moving corner so the fixed edge stays put.
        if (mode === "n" || mode === "nw" || mode === "ne") {
            y = start.y + start.h - h;
        }
        if (mode === "w" || mode === "nw" || mode === "sw") {
            x = start.x + start.w - w;
        }
        x = Math.min(1 - w, Math.max(0, x));
        y = Math.min(1 - h, Math.max(0, y));
        return { x, y, w, h };
    }

    let autoSave = null;
    let activeDrag = null;

    // Motion preview rAF loop. Runs only when at least one visible
    // layer in state.layers has motion != "static" (qarl 2026-05-02
    // demo eyeball: ops want to see motion in the BIG preview, not
    // just the layer-thumb chip). Each tick re-renders the canvas
    // with `elapsed_s` since the loop started; static-input drawCanvas
    // calls (focus / typing / dragging the box) still fire and may
    // briefly render a static frame, but the next tick (~16 ms later)
    // overwrites with motion-aware pixels — imperceptible flicker.
    // Loop self-stops when motion goes back to all-static so static
    // slides don't burn idle CPU.
    let motionRafId = null;
    let motionT0 = 0;
    function maybeStartMotionLoop() {
        if (motionRafId !== null) return;
        if (!anyLayerAnimated(state.layers)) return;
        // Don't even enqueue a rAF for a canvas that isn't in the
        // document — covers test-runner cases where mountEditor's
        // container isn't appended to document.body. jsdom's rAF is
        // backed by setTimeout, which would keep the test process
        // event loop alive forever otherwise (each tick reschedules
        // the next).
        const doc = canvas.ownerDocument;
        if (!doc || !doc.contains(canvas)) return;
        motionT0 = performance.now();
        const tick = (now) => {
            // Same guard at tick time — covers the case where the
            // editor was unmounted (DOM detached) between scheduling
            // and the rAF firing.
            if (!doc.contains(canvas)) {
                motionRafId = null;
                return;
            }
            if (!anyLayerAnimated(state.layers)) {
                motionRafId = null;
                drawCanvas(canvas, state); // settle on a static final frame
                return;
            }
            const elapsed = (now - motionT0) / 1000;
            drawCanvas(canvas, state, { elapsed_s: elapsed });
            motionRafId = requestAnimationFrame(tick);
        };
        motionRafId = requestAnimationFrame(tick);
    }
    function stopMotionLoop() {
        if (motionRafId !== null) {
            cancelAnimationFrame(motionRafId);
            motionRafId = null;
        }
    }

    function onBoxPointerDown(event) {
        if (event.button !== undefined && event.button !== 0) return;
        const handle = event.target?.dataset?.handle ?? null;
        const isMove = !handle && event.currentTarget === boxOverlay;
        if (!handle && !isMove) return;
        const rect = canvas.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;
        const layer = activeLayer();
        if (!layer) return;
        activeDrag = {
            mode: handle || "move",
            startX: event.clientX,
            startY: event.clientY,
            canvasW: rect.width,
            canvasH: rect.height,
            startBox: { ...layer.box },
            crossedThreshold: false,
        };
        boxOverlay.setPointerCapture?.(event.pointerId);
        event.preventDefault();
    }

    function onBoxPointerMove(event) {
        if (!activeDrag) return;
        const dxPx = event.clientX - activeDrag.startX;
        const dyPx = event.clientY - activeDrag.startY;
        if (
            !activeDrag.crossedThreshold &&
            Math.abs(dxPx) < DRAG_THRESHOLD_PX &&
            Math.abs(dyPx) < DRAG_THRESHOLD_PX
        ) {
            return;
        }
        if (!activeDrag.crossedThreshold) {
            activeDrag.crossedThreshold = true;
            boxOverlay.dataset.state = "dragging";
        }
        const dx = dxPx / activeDrag.canvasW;
        const dy = dyPx / activeDrag.canvasH;
        const layer = activeLayer();
        layer.box = applyHandleDrag(activeDrag.startBox, activeDrag.mode, dx, dy);
        positionBoxOverlay();
        drawCanvas(canvas, state);
    }

    function onBoxPointerUp(event) {
        if (!activeDrag) return;
        const crossed = activeDrag.crossedThreshold;
        activeDrag = null;
        delete boxOverlay.dataset.state;
        try {
            boxOverlay.releasePointerCapture?.(event.pointerId);
        } catch {
            // Capture may not have been granted; ignore.
        }
        if (crossed) {
            autoSave?.kick();
        }
    }

    if (boxOverlay) {
        boxOverlay.addEventListener("pointerdown", onBoxPointerDown);
        boxOverlay.addEventListener("pointermove", onBoxPointerMove);
        boxOverlay.addEventListener("pointerup", onBoxPointerUp);
        boxOverlay.addEventListener("pointercancel", onBoxPointerUp);
    }

    function syncSlideFromForm() {
        state.name = nameEl.value;
        state.backgroundColor = bgColorEl.value;
        drawCanvas(canvas, state);
    }

    function syncLayerFromForm(groupEl) {
        // The UI renders layers in reverse-array order, so DOM-child
        // position ≠ array index. Read both off the group element to
        // avoid the mix-up.
        const arrayIdx = layerIndexOfGroup(groupEl);
        if (arrayIdx < 0) return;
        const layer = state.layers[arrayIdx];
        if (!layer) return;
        layer.text = groupEl.querySelector(".field-text").value;
        layer.name = groupEl.querySelector(".field-layer-name").value;
        layer.textColor = groupEl.querySelector(".field-text-color").value;
        layer.fontFamily = groupEl.querySelector(".field-font-family").value;
        const sizeEl = groupEl.querySelector(".field-font-size");
        const parsedSize = Number(sizeEl.value);
        if (Number.isFinite(parsedSize) && parsedSize > 0) {
            layer.fontSizePct = parsedSize;
        }
        // Live numeric readout next to the range slider's label, so the
        // operator sees the current % without having to inspect the
        // slider thumb.
        const sizeDisplayEl = groupEl.querySelector(".field-font-size-display");
        if (sizeDisplayEl) {
            sizeDisplayEl.textContent = `(${Math.round(layer.fontSizePct ?? pickFontSizePct())}%)`;
        }
        layer.autoMode = groupEl.querySelector(".field-auto-mode").value || null;
        const fmtEl = groupEl.querySelector(".field-auto-format");
        layer.autoFormat = layer.autoMode ? fmtEl.value || null : null;
        const motionEl = groupEl.querySelector(".field-motion");
        layer.motion = motionEl ? motionEl.value || "static" : "static";
        const intensityEl = groupEl.querySelector(".field-motion-intensity");
        const parsedIntensity = Number(intensityEl?.value);
        if (Number.isFinite(parsedIntensity)) {
            layer.motionIntensity = Math.max(0, Math.min(100, Math.round(parsedIntensity)));
        }
        const phaseEl = groupEl.querySelector(".field-motion-phase");
        const parsedPhase = Number(phaseEl?.value);
        if (Number.isFinite(parsedPhase)) {
            layer.motionPhase = Math.max(0, Math.min(1, parsedPhase));
        }
        // Update the inline numeric readouts next to the slider labels.
        const intensityDisplayEl = groupEl.querySelector(".field-motion-intensity-display");
        if (intensityDisplayEl) {
            intensityDisplayEl.textContent = `(${layer.motionIntensity ?? 50})`;
        }
        const phaseDisplayEl = groupEl.querySelector(".field-motion-phase-display");
        if (phaseDisplayEl) {
            phaseDisplayEl.textContent = `(${(layer.motionPhase ?? 0).toFixed(2)})`;
        }
        // Hide the intensity+phase row when motion=static — those knobs
        // have no meaning without a non-static effect picked.
        const motionControlsEl = groupEl.querySelector(".field-motion-controls");
        if (motionControlsEl) {
            motionControlsEl.hidden = (layer.motion ?? "static") === "static";
        }
        // The header chrome (name/meta/thumbnail) drives off layer state;
        // refresh it so an in-card edit immediately mirrors up to the row.
        refreshLayerHeader(groupEl, layer, arrayIdx);
        drawCanvas(canvas, state);
        // Motion may have just been toggled on/off — kick the rAF loop
        // (no-op if already running, stops itself if no layers are
        // animated anymore).
        maybeStartMotionLoop();
    }

    function populateAutoFormatOptions(groupEl, mode, selected = null) {
        const fmtEl = groupEl.querySelector(".field-auto-format");
        const wrapEl = groupEl.querySelector(".field-auto-format-wrap");
        fmtEl.innerHTML = "";
        const options = AUTO_FORMAT_OPTIONS[mode] || [];
        for (const [value, label] of options) {
            const opt = document.createElement("option");
            opt.value = value;
            opt.textContent = label;
            if (value === selected) opt.selected = true;
            fmtEl.appendChild(opt);
        }
        wrapEl.hidden = options.length === 0;
    }

    let layerSortable = null;

    function bindLayerGroupListeners(groupEl) {
        const fontFamilyEl = groupEl.querySelector(".field-font-family");
        for (const f of FONT_FAMILIES) {
            const opt = document.createElement("option");
            opt.value = f.value;
            opt.textContent = f.label;
            fontFamilyEl.appendChild(opt);
        }
        setupFontPicker(groupEl);

        const textEl = groupEl.querySelector(".field-text");
        const layerNameEl = groupEl.querySelector(".field-layer-name");
        const textColorEl = groupEl.querySelector(".field-text-color");
        const fontSizeEl = groupEl.querySelector(".field-font-size");
        const autoModeEl = groupEl.querySelector(".field-auto-mode");
        const autoModeSegEl = groupEl.querySelector(".field-auto-mode-segmented");
        const autoFormatEl = groupEl.querySelector(".field-auto-format");
        const autoModeHintEl = groupEl.querySelector(".field-auto-mode-hint");
        const motionEl = groupEl.querySelector(".field-motion");
        const motionIntensityEl = groupEl.querySelector(".field-motion-intensity");
        const motionPhaseEl = groupEl.querySelector(".field-motion-phase");

        for (const el of [
            textEl, layerNameEl, textColorEl, fontSizeEl, fontFamilyEl,
            motionEl, motionIntensityEl, motionPhaseEl,
        ]) {
            if (!el) continue;
            // <select> fires "change" on commit; <input> fires "input"
            // continuously while the operator drags or types. Bind both
            // so each control flushes the layer state on its native
            // event without needing an explicit autoSave?.kick() (the
            // hidden-input pattern that needed kicks doesn't apply
            // here — these are real form controls).
            el.addEventListener("input", () => syncLayerFromForm(groupEl));
            el.addEventListener("change", () => syncLayerFromForm(groupEl));
            // Selecting any field in this layer makes this the active
            // layer (drives the box overlay).
            el.addEventListener("focus", () => {
                const idx = layerIndexOfGroup(groupEl);
                if (idx >= 0) selectLayer(idx);
            });
        }

        // Dynamic-source segmented control. Buttons drive a hidden
        // `.field-auto-mode` input (so the existing read-from-form code
        // path doesn't care that the chrome moved from <select> to
        // pill-group). Per QA 2026-05-01: bind to existing auto_mode
        // values (off/time/date/day) — the temp + next-event modes from
        // the design handoff are queued for qarl's call.
        //
        // Setting `.value =` on a hidden input doesn't dispatch any
        // event, so attachAutoSave's form-level input/change listener
        // wouldn't see the change → debounce never schedules → save
        // never fires (qarl 2026-05-01 ask #3 root cause for "motion
        // edits don't update tile thumbnails"). Explicitly kick the
        // debounce here, same shape as the box-drag's onBoxPointerUp.
        autoModeSegEl.querySelectorAll("button").forEach((btn) => {
            btn.addEventListener("click", () => {
                const value = btn.dataset.value;
                if (autoModeEl.value === value) return;
                autoModeEl.value = value;
                autoModeHintEl.hidden = !value;
                populateAutoFormatOptions(groupEl, value);
                refreshSegmentedPressed(autoModeSegEl, value);
                syncLayerFromForm(groupEl);
                autoSave?.kick();
            });
        });
        autoFormatEl.addEventListener("change", () => syncLayerFromForm(groupEl));


        // Quick-color swatches set this layer's text color (the BG color
        // is slide-level and lives in the Background-source card —
        // per-layer presets that paired text+bg colors are gone in v3.1).
        groupEl.querySelectorAll(".editor-color-swatch").forEach((btn) => {
            btn.addEventListener("click", (ev) => {
                ev.preventDefault();
                const color = btn.dataset.color;
                if (!color) return;
                textColorEl.value = color;
                textColorEl.dispatchEvent(new Event("input", { bubbles: true }));
            });
        });

        // Bundled @font-face fonts load lazily — kick an explicit load on
        // selection and redraw once it's ready, so the preview catches up
        // without the user having to touch another field.
        fontFamilyEl.addEventListener("change", async () => {
            const family = fontFamilyEl.value;
            const weight = FONT_WEIGHT_BY_VALUE.get(family) ?? 700;
            if (document.fonts?.load) {
                try {
                    await document.fonts.load(`${weight} 40px ${cssFontFamily(family)}`);
                } catch {
                    return;
                }
                const idx = layerIndexOfGroup(groupEl);
                if (idx >= 0 && state.layers[idx]?.fontFamily === family) {
                    drawCanvas(canvas, state);
                }
            }
        });

        textEl.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                textEl.value = "";
                syncLayerFromForm(groupEl);
            }
        });

        // Click on the layer head (anywhere except a button/input/handle)
        // expands+selects this layer; collapses the previously expanded
        // one (one-open-at-a-time accordion, per design handoff).
        const headEl = groupEl.querySelector(".editor-layer-head");
        headEl.addEventListener("click", (ev) => {
            if (ev.target.closest(".editor-layer-handle, .editor-layer-eye, button, input, textarea, select")) {
                return;
            }
            const idx = layerIndexOfGroup(groupEl);
            if (idx < 0) return;
            // Toggle: clicking the already-expanded layer's header
            // collapses it (matches the reference behavior).
            if (state.expandedLayerIndex === idx) {
                state.expandedLayerIndex = null;
            } else {
                state.expandedLayerIndex = idx;
                state.activeLayerIndex = idx;
            }
            updateActiveLayerStyling();
            positionBoxOverlay();
        });

        // Eye toggle — stop-prop so the head's click handler doesn't fire.
        // Toggles layer.visible. Hidden layers are excluded from save-time
        // rasterization (drawCanvas + rasterizeAtTarget skip them), and
        // the thumbnail fades to 30%.
        groupEl.querySelector(".editor-layer-eye").addEventListener("click", (ev) => {
            ev.stopPropagation();
            const idx = layerIndexOfGroup(groupEl);
            if (idx < 0) return;
            const layer = state.layers[idx];
            layer.visible = !layer.visible;
            refreshLayerHeader(groupEl, layer, idx);
            drawCanvas(canvas, state);
            autoSave?.kick();
        });

        groupEl.querySelector(".editor-layer-delete").addEventListener("click", (ev) => {
            ev.stopPropagation();
            const idx = layerIndexOfGroup(groupEl);
            if (idx >= 0) deleteLayerAt(idx);
        });
    }

    /**
     * Repaint a segmented control's [aria-pressed] state to reflect the
     * picked value. Called after every set-from-state and every user-
     * initiated change.
     */
    function refreshSegmentedPressed(segEl, value) {
        segEl.querySelectorAll("button").forEach((btn) => {
            btn.setAttribute(
                "aria-pressed",
                btn.dataset.value === value ? "true" : "false",
            );
        });
    }

    /**
     * Repaint the always-visible header row (name display, meta chips,
     * thumbnail, eye glyph, expand/visible/active class flags) off the
     * layer model. Called whenever the layer state mutates — drag-end,
     * delete, eye toggle, in-card field edit. Body inputs aren't touched
     * here (they're driven directly by the operator's typing).
     */
    function refreshLayerHeader(groupEl, layer, arrayIdx) {
        const total = state.layers.length;
        // Visual position: array tail = TOP of UI = "Layer 1".
        const visualPosition = total - 1 - arrayIdx;
        // Visual fallback ALWAYS shows "Layer N" (qarl 2026-05-01 review #1) —
        // no special-case for single-layer slides. Numbers track visual
        // position (DOM[0] = Layer 1) so the fallback reads naturally
        // even when the saved name field is empty.
        const fallbackName = `Layer ${visualPosition + 1}`;
        groupEl.querySelector(".editor-layer-name-display").textContent =
            layer.name?.trim() || fallbackName;

        const fontMeta =
            FONT_FAMILIES.find((f) => f.value === layer.fontFamily) ||
            FONT_FAMILIES[0];
        const sizePct = Math.round(layer.fontSizePct ?? pickFontSizePct());
        groupEl.querySelector(".editor-layer-meta-swatch").style.background =
            layer.textColor || "#FFFFFF";
        groupEl.querySelector(".editor-layer-meta-font").textContent = fontMeta.label;
        groupEl.querySelector(".editor-layer-meta-size").textContent = `${sizePct}%`;
        const motionEl = groupEl.querySelector(".editor-layer-meta-motion");
        const motionSepEl = groupEl.querySelector(".editor-layer-meta-motion-sep");
        const motion = layer.motion || "static";
        if (motion === "static") {
            motionEl.hidden = true;
            motionSepEl.hidden = true;
        } else {
            motionEl.hidden = false;
            motionSepEl.hidden = false;
            motionEl.textContent = motion;
        }

        const thumbEl = groupEl.querySelector(".editor-layer-thumb");
        const preview = (layer.text || "").slice(0, 8) || "—";
        thumbEl.style.color = layer.textColor || "#FFFFFF";
        // CSS-keyframes preview of the picked motion effect — visually
        // approximate, not pixel-identical to the device renderer (per
        // docs/text-layer-motion-spec.md Q3 lock). The thumb is small
        // (36×18) so the animation reads as "yes, this layer animates"
        // rather than as a faithful simulation.
        for (const cls of [...thumbEl.classList]) {
            if (cls.startsWith("motion-")) thumbEl.classList.remove(cls);
        }
        const motionForThumb = layer.motion || "static";
        if (motionForThumb !== "static") {
            thumbEl.classList.add(`motion-${motionForThumb}`);
        }
        // Thumb content: keyframes target an INNER span so the thumb's
        // overflow:hidden clips the animation. Animating the thumb
        // itself moves its clipping box too — ticker text would spill
        // outside the 36×18 footprint (qarl 2026-05-02 demo eyeball).
        // Ticker also needs visible repeat so the animation reads as
        // a continuous strip rather than "text exits, gap, text returns";
        // the trick is two text copies inside a track that animates
        // translateX 0 → -50% (= one copy width), loops seamlessly.
        thumbEl.replaceChildren();
        if (motionForThumb === "ticker") {
            const track = document.createElement("span");
            track.className = "editor-layer-thumb-ticker-track";
            const a = document.createElement("span");
            a.textContent = preview;
            const b = document.createElement("span");
            b.textContent = preview;
            track.append(a, b);
            thumbEl.append(track);
        } else {
            const span = document.createElement("span");
            span.className = "editor-layer-thumb-text";
            span.textContent = preview;
            thumbEl.append(span);
        }

        const visible = layer.visible !== false;
        groupEl.classList.toggle("editor-layer-hidden", !visible);
        groupEl.querySelector(".editor-layer-eye-glyph").textContent = visible ? "●" : "○";
    }

    function layerIndexOfGroup(groupEl) {
        // The UI renders layers in REVERSE of array order — the visual
        // top is `text_layers[N-1]` (drawn last, composited on top), per
        // qarl's §5.10a v3 spec. Convert DOM-child position back to
        // array index here.
        const domIdx = Array.prototype.indexOf.call(layersListEl.children, groupEl);
        if (domIdx < 0) return -1;
        return state.layers.length - 1 - domIdx;
    }

    function buildLayerGroupEl(layer, idx) {
        const groupEl = document.createElement("div");
        // .editor-layer-group kept on the wrapping element for back-compat
        // with tests that select via `.editor-layer-group`. .editor-layer-
        // card is the v3.1 accordion-chrome class; both apply at once.
        groupEl.className = "editor-layer-card editor-layer-group";
        groupEl.innerHTML = LAYER_GROUP_TEMPLATE;
        groupEl.dataset.layerIndex = String(idx);

        // Hydrate fields from the layer model.
        groupEl.querySelector(".field-text").value = layer.text || "";
        groupEl.querySelector(".field-layer-name").value = layer.name || "";
        groupEl.querySelector(".field-text-color").value = layer.textColor || "#FFFFFF";
        const fontFamilyEl = groupEl.querySelector(".field-font-family");
        const fontSizeEl = groupEl.querySelector(".field-font-size");
        const autoModeEl = groupEl.querySelector(".field-auto-mode");
        const autoModeHintEl = groupEl.querySelector(".field-auto-mode-hint");
        const motionEl = groupEl.querySelector(".field-motion");

        // bindLayerGroupListeners populates the <select> options before
        // we set its value below — so insert listeners-first, then write
        // values, then notify the picker.
        bindLayerGroupListeners(groupEl);

        fontFamilyEl.value = layer.fontFamily || FONT_FAMILIES[0].value;
        fontFamilyEl.dispatchEvent(new Event("font-picker-sync"));
        fontSizeEl.value = String(layer.fontSizePct ?? pickFontSizePct());
        const sizeDisplayEl = groupEl.querySelector(".field-font-size-display");
        if (sizeDisplayEl) {
            sizeDisplayEl.textContent = `(${Math.round(layer.fontSizePct ?? pickFontSizePct())}%)`;
        }
        autoModeEl.value = layer.autoMode || "";
        autoModeHintEl.hidden = !layer.autoMode;
        populateAutoFormatOptions(groupEl, layer.autoMode || "", layer.autoFormat || null);
        refreshSegmentedPressed(
            groupEl.querySelector(".field-auto-mode-segmented"),
            layer.autoMode || "",
        );
        motionEl.value = layer.motion || "static";
        const intensityEl = groupEl.querySelector(".field-motion-intensity");
        const phaseEl = groupEl.querySelector(".field-motion-phase");
        const intensityVal = layer.motionIntensity ?? 50;
        const phaseVal = layer.motionPhase ?? 0;
        intensityEl.value = String(intensityVal);
        phaseEl.value = String(phaseVal);
        groupEl.querySelector(".field-motion-intensity-display").textContent =
            `(${intensityVal})`;
        groupEl.querySelector(".field-motion-phase-display").textContent =
            `(${Number(phaseVal).toFixed(2)})`;
        groupEl.querySelector(".field-motion-controls").hidden =
            (layer.motion || "static") === "static";
        refreshLayerHeader(groupEl, layer, idx);

        return groupEl;
    }

    function renderLayers() {
        // Wholesale rebuild keeps the array → DOM mapping trivial. Layer
        // groups are cheap (a few dozen nodes); a per-input edit doesn't
        // re-enter renderLayers, so we don't lose focus on each keystroke.
        //
        // Render in REVERSE of array order so the visual TOP of the UI
        // list is the layer drawn LAST (top z-order). This matches the
        // Photoshop convention operators expect AND qarl's §5.10a v3
        // contract: "+ New layer adds at the top of the list (drawn last
        // → composited on top)" — addLayer pushes to the array tail,
        // which appears at DOM child[0] under this iteration order.
        if (layerSortable) {
            layerSortable.destroy();
            layerSortable = null;
        }
        layersListEl.innerHTML = "";
        for (let i = state.layers.length - 1; i >= 0; i--) {
            const groupEl = buildLayerGroupEl(state.layers[i], i);
            layersListEl.appendChild(groupEl);
        }
        updateActiveLayerStyling();
        updateDeleteButtonAvailability();
        updateLayersCountEyebrow();
        positionBoxOverlay();
        bindLayerSortable();
        // After a wholesale rebuild (loadForEdit / addLayer / delete /
        // reorder), the layer set may have changed — re-evaluate
        // whether the motion rAF loop should be running.
        maybeStartMotionLoop();
    }

    function updateLayersCountEyebrow() {
        // The eyebrow above the layer list reads "LAYERS · N" per the
        // accordion-editor handoff.
        const eyebrowEl = container.querySelector(".editor-layers .om-eyebrow");
        if (eyebrowEl) {
            eyebrowEl.textContent = `Layers · ${state.layers.length}`;
        }
    }

    function updateActiveLayerStyling() {
        const total = state.layers.length;
        for (let domIdx = 0; domIdx < layersListEl.children.length; domIdx++) {
            const arrayIdx = total - 1 - domIdx;
            const groupEl = layersListEl.children[domIdx];
            const isActive = arrayIdx === state.activeLayerIndex;
            const isExpanded = arrayIdx === state.expandedLayerIndex;
            groupEl.classList.toggle("editor-layer-active", isActive);
            groupEl.classList.toggle("editor-layer-expanded", isExpanded);
        }
    }

    function updateDeleteButtonAvailability() {
        // Backend min_length=1 — never let the operator delete the last
        // layer through the UI. Hide rather than disable so the chrome is
        // crisp.
        const single = state.layers.length === 1;
        for (const groupEl of layersListEl.children) {
            const btn = groupEl.querySelector(".editor-layer-delete");
            btn.hidden = single;
        }
    }

    function bindLayerSortable() {
        layerSortable = Sortable.create(layersListEl, {
            handle: ".editor-layer-handle",
            animation: 150,
            onEnd: (ev) => {
                if (ev.oldIndex === ev.newIndex) return;
                // Sortable reports DOM-child positions; convert to array
                // indices (UI is rendered in reverse of array order).
                const total = state.layers.length;
                const oldArrayIdx = total - 1 - ev.oldIndex;
                const newArrayIdx = total - 1 - ev.newIndex;
                const moved = state.layers.splice(oldArrayIdx, 1)[0];
                state.layers.splice(newArrayIdx, 0, moved);
                state.activeLayerIndex = shiftIndexForReorder(
                    state.activeLayerIndex, oldArrayIdx, newArrayIdx,
                );
                state.expandedLayerIndex = shiftIndexForReorder(
                    state.expandedLayerIndex, oldArrayIdx, newArrayIdx,
                );
                renderLayers();
                drawCanvas(canvas, state);
                autoSave?.kick();
            },
        });
    }

    /**
     * After a Sortable reorder, return where a tracked array index
     * (e.g. activeLayerIndex) ends up. Handles all four cases:
     *   - the tracked index IS the moved layer → follows it to newIdx
     *   - moved across forward (oldIdx < tracked, newIdx ≥ tracked) → tracked - 1
     *   - moved across backward (oldIdx > tracked, newIdx ≤ tracked) → tracked + 1
     *   - moved entirely on the other side → unchanged
     */
    function shiftIndexForReorder(tracked, oldIdx, newIdx) {
        if (tracked === null || tracked === undefined) return tracked;
        if (tracked === oldIdx) return newIdx;
        if (oldIdx < tracked && newIdx >= tracked) return tracked - 1;
        if (oldIdx > tracked && newIdx <= tracked) return tracked + 1;
        return tracked;
    }

    function selectLayer(idx) {
        if (idx < 0 || idx >= state.layers.length) return;
        if (state.activeLayerIndex === idx) return;
        state.activeLayerIndex = idx;
        updateActiveLayerStyling();
        positionBoxOverlay();
    }

    function addLayer() {
        // New layer inserts at the END of the array (drawn last →
        // composited on top), matching qarl's spec note: "+ New layer
        // adds at the top of the list (drawn last → composited on top)".
        // The new layer becomes the active + expanded one (selection
        // and expansion are coupled in the accordion).
        state.layers.push(makeAutoNamedLayer(state.layers));
        state.activeLayerIndex = state.layers.length - 1;
        state.expandedLayerIndex = state.layers.length - 1;
        renderLayers();
        drawCanvas(canvas, state);
        autoSave?.kick();
    }

    function deleteLayerAt(idx) {
        if (state.layers.length <= 1) return;
        state.layers.splice(idx, 1);
        if (state.activeLayerIndex >= state.layers.length) {
            state.activeLayerIndex = state.layers.length - 1;
        } else if (state.activeLayerIndex > idx) {
            state.activeLayerIndex -= 1;
        }
        if (
            state.expandedLayerIndex === null ||
            state.expandedLayerIndex >= state.layers.length
        ) {
            state.expandedLayerIndex = state.layers.length - 1;
        } else if (state.expandedLayerIndex > idx) {
            state.expandedLayerIndex -= 1;
        }
        renderLayers();
        drawCanvas(canvas, state);
        autoSave?.kick();
    }

    addLayerBtn.addEventListener("click", addLayer);

    nameEl.addEventListener("input", syncSlideFromForm);
    bgColorEl.addEventListener("input", syncSlideFromForm);

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
            bgColorWrapEl.hidden = state.bgSource !== "color";
            bgSlideWrapEl.hidden = state.bgSource !== "slide";
            bgVideoWrapEl.hidden = state.bgSource !== "video";
            bgGradWrapEl.hidden = state.bgSource !== "gradient";
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
            // never carries multiple bg sources at once.
            if (state.bgSource === "color") {
                state.bgImage = null;
                state.bgSlideId = null;
                state.bgVideoId = null;
            } else if (state.bgSource === "slide") {
                state.bgVideoId = null;
            } else if (state.bgSource === "video") {
                state.bgSlideId = null;
            } else if (state.bgSource === "gradient") {
                state.bgImage = null;
                state.bgSlideId = null;
                state.bgVideoId = null;
            }
            drawCanvas(canvas, state);
        });
    }

    // Gradient-source live editing: any of start, end, or angle change
    // re-runs the canvas paint so the operator sees the result instantly.
    bgGradStartEl.addEventListener("input", () => {
        state.bgGradient.start_color = bgGradStartEl.value;
        if (state.bgSource === "gradient") drawCanvas(canvas, state);
    });
    bgGradEndEl.addEventListener("input", () => {
        state.bgGradient.end_color = bgGradEndEl.value;
        if (state.bgSource === "gradient") drawCanvas(canvas, state);
    });
    bgGradAngleEl.addEventListener("input", () => {
        const angle = parseInt(bgGradAngleEl.value, 10) || 0;
        state.bgGradient.angle_deg = angle;
        bgGradAngleLabelEl.textContent = String(angle);
        if (state.bgSource === "gradient") drawCanvas(canvas, state);
    });

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
        drawCanvas(canvas, state);
    });

    bgVideoEl.addEventListener("change", async () => {
        // The editor's preview canvas renders the video's THUMBNAIL as a
        // static bg under the text. Live moving-video preview lives in
        // the playlist panel's inline-preview, not here.
        state.bgVideoId = bgVideoEl.value || null;
        state.bgImage = state.bgVideoId
            ? await loadImageForSlide(state.bgVideoId).catch(() => null)
            : null;
        drawCanvas(canvas, state);
    });

    form.addEventListener("keydown", (event) => {
        // Plain Enter inside a single-line <input> would otherwise submit
        // the form (browser default). Suppress unless focus is in a
        // <textarea>, where Enter means "newline".
        if (event.key === "Enter" && event.target?.tagName !== "TEXTAREA") {
            event.preventDefault();
        }
    });

    async function performSave() {
        if (document.fonts?.ready) await document.fonts.ready;
        const png_base64 = rasterizeAtTarget(state);
        const durationSeconds = Number(durationEl.value) || 5;
        const text_layers = state.layers.map((layer) => ({
            text: layer.text,
            name: layer.name || "",
            text_color: (layer.textColor || "#FFFFFF").toUpperCase(),
            font_family: layer.fontFamily,
            font_size_pct: layer.fontSizePct,
            auto_mode: layer.autoMode || null,
            auto_format: layer.autoMode ? layer.autoFormat || null : null,
            motion: layer.motion || "static",
            motion_intensity: layer.motionIntensity ?? 50,
            motion_phase: layer.motionPhase ?? 0,
            visible: layer.visible !== false,
            box: { ...layer.box },
        }));
        const payload = {
            name: state.name || "Untitled",
            background_color: state.backgroundColor.toUpperCase(),
            background_image_slide_id: state.bgSource === "slide" ? state.bgSlideId || null : null,
            background_video_slide_id: state.bgSource === "video" ? state.bgVideoId || null : null,
            background_gradient: state.bgSource === "gradient" ? {
                type: "linear",
                start_color: state.bgGradient.start_color.toUpperCase(),
                end_color: state.bgGradient.end_color.toUpperCase(),
                angle_deg: state.bgGradient.angle_deg,
            } : null,
            duration_ms: Math.round(durationSeconds * 1000),
            text_layers,
            png_base64,
        };
        const wasEdit = Boolean(state.editingId);
        const result = wasEdit && onSaveExisting
            ? await onSaveExisting(state.editingId, payload)
            : await onSave(payload);
        if (!wasEdit && result?.id) {
            state.editingId = String(result.id);
        }
        if (browser && state.editingId) browser.highlight(state.editingId);
    }

    autoSave = attachAutoSave(form, {
        save: performSave,
        status: statusEl,
        // Create-mode requires non-empty text on at least one layer to
        // bother saving (otherwise an empty form auto-creates a junk slide
        // on first focus). Edit mode allows empty text — the operator may
        // be intentionally clearing layers.
        canSave: () =>
            Boolean(state.editingId) ||
            state.layers.some((l) => (l.text || "").trim().length > 0),
        debounceMs: 900,
    });

    async function resetToBlank() {
        state.editingId = null;
        state.bgImage = null;
        state.bgSlideId = null;
        state.bgVideoId = null;
        state.layers = [makeAutoNamedLayer([])];
        state.activeLayerIndex = 0;
        state.expandedLayerIndex = 0;
        renderLayers();
        const colorRadio = container.querySelector(
            '.field-bg-source[value="color"]',
        );
        colorRadio.checked = true;
        bgColorWrapEl.hidden = false;
        bgSlideWrapEl.hidden = true;
        bgVideoWrapEl.hidden = true;
        bgGradWrapEl.hidden = true;
        state.bgSource = "color";
        drawCanvas(canvas, state);
        autoSave.cancel();
        statusEl.textContent = "";
        statusEl.dataset.state = "idle";

        // Async tail: gap-filled default name + browser refresh, both
        // no-ops if loadForEdit took ownership during the await.
        const defaultName = await computeDefaultName();
        if (state.editingId !== null) return;
        nameEl.value = defaultName;
        state.name = defaultName;
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
     * Seeded with a single space as placeholder text so backend's
     * non-empty validation passes — operators replace it with their first
     * keystroke.
     */
    async function createNew() {
        await resetToBlank();
        state.layers[0].text = " ";
        const groupEl = layersListEl.children[0];
        if (groupEl) groupEl.querySelector(".field-text").value = " ";
        drawCanvas(canvas, state);
        try {
            await performSave();
        } catch (err) {
            statusEl.textContent = `Could not create slide: ${err?.message || err}`;
            statusEl.dataset.state = "error";
            return;
        }
        if (browser) await browser.refresh();
    }

    /**
     * Coerce a wire-shape TextLayer into editor-state shape.
     */
    function layerFromWire(wire) {
        return {
            text: wire?.text || "",
            name: wire?.name || "",
            textColor: wire?.text_color || "#FFFFFF",
            fontFamily: wire?.font_family || FONT_FAMILIES[0].value,
            fontSizePct:
                wire?.font_size_pct ??
                (wire?.font_size_px
                    ? (wire.font_size_px / width) * 100
                    : pickFontSizePct()),
            autoMode: wire?.auto_mode || null,
            autoFormat: wire?.auto_format || null,
            motion: wire?.motion || "static",
            motionIntensity: wire?.motion_intensity ?? 50,
            motionPhase: wire?.motion_phase ?? 0,
            visible: wire?.visible !== false,
            box:
                wire?.box && typeof wire.box === "object"
                    ? {
                          x: Number(wire.box.x ?? 0.1),
                          y: Number(wire.box.y ?? 0.1),
                          w: Number(wire.box.w ?? 0.8),
                          h: Number(wire.box.h ?? 0.8),
                      }
                    : { x: 0.1, y: 0.1, w: 0.8, h: 0.8 },
        };
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
        state.name = nameEl.value;
        bgColorEl.value = slide.background_color || "#000000";
        state.backgroundColor = bgColorEl.value;
        durationEl.value = String(Math.max(1, (slide.duration_ms || 5000) / 1000));

        const wireLayers = Array.isArray(slide.text_layers) && slide.text_layers.length
            ? slide.text_layers
            : [{ text: "" }];
        state.layers = wireLayers.map(layerFromWire);
        // Backfill empty-name layers with "Layer N" (qarl 2026-05-01
        // review #1). Pre-76934f2 saves left layer.name="" — without
        // this the saved name stays blank forever and the chip falls
        // through to the visual fallback. Backfilling here surfaces
        // the auto-name in the input AND saves it on next edit, so
        // the slide shape catches up over time.
        const seenNames = state.layers
            .filter((l) => (l.name || "").trim() !== "")
            .map((l) => ({ name: l.name }));
        for (const layer of state.layers) {
            if ((layer.name || "").trim() === "") {
                layer.name = nextLayerName(seenNames);
                seenNames.push({ name: layer.name });
            }
        }
        // Default selection + expansion: top of UI = array tail = the
        // layer drawn last. Operators expect "the topmost layer is open
        // when I click into a slide."
        state.activeLayerIndex = state.layers.length - 1;
        state.expandedLayerIndex = state.layers.length - 1;
        renderLayers();

        if (slide.background_image_slide_id) {
            const slideRadio = container.querySelector(
                '.field-bg-source[value="slide"]',
            );
            slideRadio.checked = true;
            if (fetchItems && !bgSlidePopulated) {
                await populateBgSlideOptions(bgSlideEl, fetchItems, statusEl);
                bgSlidePopulated = true;
            }
            bgColorWrapEl.hidden = true;
            bgSlideWrapEl.hidden = false;
            bgVideoWrapEl.hidden = true;
            bgGradWrapEl.hidden = true;
            bgSlideEl.value = String(slide.background_image_slide_id);
            state.bgSource = "slide";
            state.bgSlideId = String(slide.background_image_slide_id);
            state.bgVideoId = null;
            state.bgImage = await loadImageForSlide(state.bgSlideId).catch(
                () => null,
            );
        } else if (slide.background_video_slide_id) {
            const videoRadio = container.querySelector(
                '.field-bg-source[value="video"]',
            );
            videoRadio.checked = true;
            if (fetchItems && !bgVideoPopulated) {
                await populateBgVideoOptions(bgVideoEl, fetchItems, statusEl);
                bgVideoPopulated = true;
            }
            bgColorWrapEl.hidden = true;
            bgSlideWrapEl.hidden = true;
            bgVideoWrapEl.hidden = false;
            bgGradWrapEl.hidden = true;
            bgVideoEl.value = String(slide.background_video_slide_id);
            state.bgSource = "video";
            state.bgSlideId = null;
            state.bgVideoId = String(slide.background_video_slide_id);
            state.bgImage = await loadImageForSlide(state.bgVideoId).catch(
                () => null,
            );
        } else if (slide.background_gradient) {
            const gradRadio = container.querySelector(
                '.field-bg-source[value="gradient"]',
            );
            gradRadio.checked = true;
            bgColorWrapEl.hidden = true;
            bgSlideWrapEl.hidden = true;
            bgVideoWrapEl.hidden = true;
            bgGradWrapEl.hidden = false;
            state.bgSource = "gradient";
            state.bgSlideId = null;
            state.bgVideoId = null;
            state.bgImage = null;
            state.bgGradient = {
                start_color: slide.background_gradient.start_color,
                end_color: slide.background_gradient.end_color,
                angle_deg: slide.background_gradient.angle_deg,
            };
            bgGradStartEl.value = state.bgGradient.start_color;
            bgGradEndEl.value = state.bgGradient.end_color;
            bgGradAngleEl.value = String(state.bgGradient.angle_deg);
            bgGradAngleLabelEl.textContent = String(state.bgGradient.angle_deg);
        } else {
            const colorRadio = container.querySelector(
                '.field-bg-source[value="color"]',
            );
            colorRadio.checked = true;
            bgColorWrapEl.hidden = false;
            bgSlideWrapEl.hidden = true;
            bgVideoWrapEl.hidden = true;
            bgGradWrapEl.hidden = true;
            state.bgSource = "color";
            state.bgSlideId = null;
            state.bgVideoId = null;
            state.bgImage = null;
        }
        drawCanvas(canvas, state);

        // Bundled @font-face fonts load lazily — wait for any used by
        // any layer to be ready, then re-render so the canvas catches up
        // without the operator having to touch a field.
        const families = new Set(
            state.layers.map((l) => l.fontFamily).filter(Boolean),
        );
        if (families.size > 0 && document.fonts?.load) {
            const loadedAt = state.editingId;
            try {
                await Promise.all(
                    [...families].map((family) => {
                        const weight = FONT_WEIGHT_BY_VALUE.get(family) ?? 700;
                        return document.fonts.load(
                            `${weight} 40px ${cssFontFamily(family)}`,
                        );
                    }),
                );
                if (document.fonts?.ready) await document.fonts.ready;
                await new Promise((resolve) =>
                    requestAnimationFrame(() => resolve()),
                );
            } catch {
                // A failed font-face load shouldn't break the editor —
                // canvas already painted with the fallback face.
                return;
            }
            if (state.editingId === loadedAt) {
                drawCanvas(canvas, state);
            }
        }
        // Loading is not an edit — drop any auto-save scheduled by the
        // field mutations above.
        autoSave.cancel();
    }

    // Initial layer-group render. Mount listeners + box overlay before
    // the slide-browser kicks loadForEdit so a synchronous edit-load
    // doesn't race against the unmounted layers list.
    renderLayers();

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
            drawCanvas(canvas, state);
        }
    })();

    return {
        loadForEdit,
        reset: resetToBlank,
        createNew,
        refreshBrowser: () => browser?.refresh(),
        flushAutoSave: () => autoSave.flush(),
    };
}

export async function populateBgSlideOptions(selectEl, fetchItems, statusEl) {
    try {
        const items = await fetchItems();
        selectEl.innerHTML = '<option value="">(pick a slide)</option>';
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
 * Render only the text layers of a TextSlide onto `canvas`, leaving the
 * canvas's background transparent. Used by the inline-preview to overlay
 * text on top of a live video frame for Text-over-Video slides
 * (Phase 5b — SYSTEM_SPEC §5.10). Iterates `text_layers` in array order
 * (later entries composite over earlier).
 *
 * Accepts the on-the-wire ContentItem shape — not the editor's internal
 * `state` — because the inline-preview consumes ContentItem directly.
 */
export function drawTextOnly(canvas, item, opts) {
    const ctx = canvas.getContext("2d");
    ctx.save();
    try {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        const layers = Array.isArray(item.text_layers) ? item.text_layers : [];
        const elapsed = opts && opts.elapsed_s;
        const slideKey = (item && (item.id || "?")) + "";
        for (let i = 0; i < layers.length; i++) {
            const layer = layers[i];
            const paint = () => paintLayer(ctx, canvas, layer, /* fillBox */ null);
            if (elapsed === undefined || elapsed === null) {
                // Static path — current behavior, no motion.
                paint();
            } else {
                paintLayerWithMotion(ctx, canvas, layer, paint, {
                    elapsed_s: elapsed,
                    layerKey: `${slideKey}:${i}`,
                });
            }
        }
    } finally {
        ctx.restore();
    }
}

/**
 * Paint a single layer's text onto an already-cleared / pre-filled
 * context. `box` defaults to {0.1, 0.1, 0.8, 0.8} when absent. Mirrors
 * `_draw_text_into` on the backend (seed.py).
 */
function paintLayer(ctx, canvas, layer) {
    const text = layer?.text || "";
    if (!text) return;
    const textColor = layer.text_color || layer.textColor || "#FFFFFF";
    const fontFamily = layer.font_family || layer.fontFamily || "sans-serif";
    const box = layer.box || { x: 0.1, y: 0.1, w: 0.8, h: 0.8 };

    const boxX = box.x * canvas.width;
    const boxY = box.y * canvas.height;
    const boxW = Math.max(1, box.w * canvas.width);
    const boxH = Math.max(1, box.h * canvas.height);

    let fontSizePx;
    const pct = layer.font_size_pct ?? layer.fontSizePct;
    const px = layer.font_size_px ?? layer.fontSize;
    if (Number.isFinite(pct) && pct > 0) {
        // §5.10a v3.1.2 (qarl 2026-05-01 review #3): font_size_pct is
        // a percentage of BOX WIDTH (not slide width). Resizing the
        // box visibly resizes the text — operators expected that math
        // and asked for it explicitly. Math: pct% × box.w × canvas.width
        // = pct% × boxW (already in pixels).
        fontSizePx = Math.max(4, Math.round((boxW * pct) / 100));
    } else if (Number.isFinite(px) && px > 0) {
        fontSizePx = px;
    } else {
        fontSizePx = pickFontSize(boxW);
    }
    ctx.fillStyle = textColor;
    const weight = FONT_WEIGHT_BY_VALUE.get(fontFamily) ?? 700;
    ctx.font = `${weight} ${fontSizePx}px ${cssFontFamily(fontFamily)}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    const lines = text.split(/\r?\n/);
    const lineHeight = fontSizePx * 1.1;
    const totalHeight = lineHeight * lines.length;
    const boxCenterX = boxX + boxW / 2;
    const boxCenterY = boxY + boxH / 2;
    const maxWidth = Math.max(1, boxW);
    // Vertical squish (qarl 2026-05-01 ask #1): when total rendered
    // text height exceeds box height, scale-y around the box center
    // so lines stay inside. fillText's maxWidth handles horizontal
    // overflow as before — both axes squish independently.
    const yScale = totalHeight > boxH ? boxH / totalHeight : 1;
    if (yScale === 1) {
        const startY = boxCenterY - totalHeight / 2 + lineHeight / 2;
        for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], boxCenterX, startY + i * lineHeight, maxWidth);
        }
    } else {
        ctx.save();
        ctx.translate(boxCenterX, boxCenterY);
        ctx.scale(1, yScale);
        // Draw centered around (0,0) under the local transform; each
        // line's y-offset is from the centered origin. fillText's
        // maxWidth is in untransformed coords, so it still clamps
        // horizontal width correctly.
        const lineY0 = -totalHeight / 2 + lineHeight / 2;
        for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], 0, lineY0 + i * lineHeight, maxWidth);
        }
        ctx.restore();
    }
}

/**
 * Draw the slide onto `canvas`. Pure: only reads `state` and writes
 * pixels — no DOM wiring, no event handlers.
 *
 * Accepts BOTH editor-state shape (`state.layers` with internal field
 * names) AND a flat single-layer back-compat shape (`state.text`,
 * `state.textColor`, …) for callers that haven't migrated. The two
 * shapes are distinguished by presence of `.layers`.
 */
export function drawCanvas(canvas, state, opts) {
    const ctx = canvas.getContext("2d");
    const {
        backgroundColor = "#000000",
        bgSource = "color",
        bgImage = null,
        bgGradient = null,
    } = state;

    ctx.save();
    try {
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
        } else if (bgSource === "gradient" && bgGradient) {
            // CSS-like angle: 0 = top→bottom, 90 = left→right.
            // Match the backend renderer's projection so editor preview
            // and device output agree visually.
            const rad = (bgGradient.angle_deg * Math.PI) / 180;
            const dx = Math.sin(rad);
            const dy = Math.cos(rad);
            const cx = canvas.width / 2;
            const cy = canvas.height / 2;
            const half = Math.abs(dx) * (canvas.width / 2)
                + Math.abs(dy) * (canvas.height / 2);
            const grad = ctx.createLinearGradient(
                cx - dx * half, cy - dy * half,
                cx + dx * half, cy + dy * half,
            );
            grad.addColorStop(0, bgGradient.start_color);
            grad.addColorStop(1, bgGradient.end_color);
            ctx.fillStyle = grad;
            ctx.fillRect(0, 0, canvas.width, canvas.height);
        } else {
            ctx.fillStyle = backgroundColor;
            ctx.fillRect(0, 0, canvas.width, canvas.height);
        }

        const layers = layersForDraw(state);
        const elapsed = opts && opts.elapsed_s;
        for (let i = 0; i < layers.length; i++) {
            const layer = layers[i];
            // §5.10a v3.1: editor's eye toggle sets visible=false; skip
            // hidden layers entirely so the rasterized PNG matches what
            // the operator sees in preview.
            if (layer?.visible === false) continue;
            const resolved = resolveLayerForDraw(layer);
            const paint = () => paintLayer(ctx, canvas, resolved);
            if (elapsed === undefined || elapsed === null) {
                paint();
            } else {
                // The motion wrapper takes the unresolved layer so it
                // reads .motion / .motion_intensity / .motion_phase
                // off the editor-state shape; paintLayer is called via
                // the closure with the auto-resolved layer.
                paintLayerWithMotion(ctx, canvas, layer, paint, {
                    elapsed_s: elapsed,
                    layerKey: `editor:${i}`,
                });
            }
        }
    } finally {
        ctx.restore();
    }
}

function layersForDraw(state) {
    if (Array.isArray(state.layers) && state.layers.length > 0) {
        return state.layers;
    }
    // Back-compat single-layer shape: pull a synthetic layer off the
    // top-level state fields. This keeps drawCanvas usable from older
    // unit tests that pass `{text, textColor, …}` directly.
    return [
        {
            text: state.text || "",
            textColor: state.textColor,
            fontFamily: state.fontFamily,
            fontSizePct: state.fontSizePct,
            fontSize: state.fontSize,
            autoMode: state.autoMode,
            autoFormat: state.autoFormat,
            box: state.box,
        },
    ];
}

function resolveLayerForDraw(layer) {
    // Auto-mode tokens (time / date / day): the canvas shows the current
    // formatted value so the preview matches what the device renders at
    // playout. Operator's typed text is the fallback.
    const rawText = layer.text || "";
    const mode = layer.auto_mode ?? layer.autoMode ?? null;
    const fmt = layer.auto_format ?? layer.autoFormat ?? null;
    const text = mode
        ? formatAutoText(mode, fmt, new Date()) || rawText
        : rawText;
    return { ...layer, text };
}

/**
 * Heuristic fallback when neither `font_size_pct` nor `font_size_px`
 * is set on a layer. Width-relative per §5.10a v3.1.1 (qarl
 * 2026-05-01 ask #1) — matches the new pct semantic so a slide
 * without explicit sizing reads the same way the editor's "% of
 * width" field would suggest.
 */
export function pickFontSize(panelWidth) {
    return Math.max(12, Math.floor(panelWidth * 0.3));
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
 * its base64 PNG body.
 */
export function rasterizeAtTarget(state) {
    const off = document.createElement("canvas");
    off.width = RASTERIZE_W;
    off.height = RASTERIZE_H;
    drawCanvas(off, state);
    return canvasToBase64(off);
}
