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
import {
    PATTERN_NAMES,
    PATTERN_LABELS,
    buildBg as buildPatternBg,
    patternUsesColorB,
    patternUsesDensity,
    densityLabelFor,
} from "./bg-system.js";
import { populateBgSlideOptions, populateBgVideoOptions } from "./bg-picker.js";
import { mountColorPicker } from "./color-picker.js";
import {
    anyLayerAnimated,
} from "./canvas-motion.js";
import {
    FONT_FAMILIES,
    FONT_WEIGHT_BY_VALUE,
    cssFontFamily,
    setupFontPicker,
} from "./font-picker.js";
import { makeAutoNamedLayer, nextLayerName } from "./layer-defaults.js";
import {
    drawCanvas,
    drawTextOnly,
    pickFontSize,
    pickFontSizePct,
    rasterizeAtTarget,
} from "./rasterize.js";
import { mountSlideBrowser, nextAutoName } from "./slide-browser.js";

// Re-export the public surface so existing importers
// (main.js, editor.test.js, bg-picker.test.js, inline-preview.js)
// don't churn on the split.
export {
    FONT_FAMILIES,
    populateBgSlideOptions,
    populateBgVideoOptions,
    drawCanvas,
    drawTextOnly,
    pickFontSize,
    pickFontSizePct,
    rasterizeAtTarget,
};

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
                        <input type="radio" name="editor-bg-source" class="field-bg-source" value="pattern" checked>
                        <span>Pattern</span>
                    </label>
                    <div class="editor-bg-pattern-wrap" hidden>
                        <div class="editor-bg-pattern-grid" role="radiogroup"
                             aria-label="Pattern picker"></div>
                        <div class="editor-bg-pattern-tweaks">
                            <div class="om-row editor-bg-pattern-tweak" style="gap: 10px; align-items: center;">
                                <span class="editor-bg-pattern-label">Color A</span>
                                <input type="color" class="field-bg-pat-color-a" value="#000000" style="flex: 1; height: 36px; border-radius: 9px; border: 1px solid var(--om-line); background: var(--om-surface-2);">
                            </div>
                            <div class="om-row editor-bg-pattern-tweak editor-bg-pattern-tweak-b" style="gap: 10px; align-items: center;">
                                <span class="editor-bg-pattern-label">Color B</span>
                                <input type="color" class="field-bg-pat-color-b" value="#FFB43C" style="flex: 1; height: 36px; border-radius: 9px; border: 1px solid var(--om-line); background: var(--om-surface-2);">
                            </div>
                            <div class="om-row editor-bg-pattern-tweak editor-bg-pattern-tweak-d" style="gap: 10px; align-items: center;">
                                <span class="editor-bg-pattern-label field-bg-pat-density-label">Density</span>
                                <input type="range" class="field-bg-pat-density" min="0" max="100" step="1" value="50" style="flex: 1;">
                                <span class="field-bg-pat-density-val" style="font-family: var(--om-mono); font-size: 11px; color: var(--om-text-fade); min-width: 28px; text-align: right;">50</span>
                            </div>
                        </div>
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
        <div class="om-row" style="gap: 10px; align-items: end;">
            <label class="om-field" style="flex: 1;">
                <span>Blend</span>
                <select class="om-select field-blend" aria-label="layer blend mode">
                    <option value="normal">Normal</option>
                    <option value="multiply">Multiply</option>
                    <option value="screen">Screen</option>
                    <option value="overlay">Overlay</option>
                </select>
            </label>
            <label class="om-field" style="flex: 1;">
                <span>Opacity <span class="field-opacity-display"></span></span>
                <input type="range" class="om-range field-opacity" min="0" max="100" step="1" value="100">
            </label>
        </div>
        <label class="om-field">
            <span>Layer name</span>
            <input type="text" class="om-input field-layer-name" placeholder="Headline" maxlength="200">
        </label>
        <label class="om-field">
            <span>Text</span>
            <textarea class="om-textarea field-text" rows="2" placeholder="(enter text here)"></textarea>
        </label>
        <div class="om-field">
            <span>Align</span>
            <div class="editor-segmented field-text-align-segmented" role="group" aria-label="text alignment">
                <button type="button" data-value="left" aria-pressed="false">Left</button>
                <button type="button" data-value="center" aria-pressed="true">Center</button>
                <button type="button" data-value="right" aria-pressed="false">Right</button>
            </div>
            <input type="hidden" class="field-text-align" value="center">
        </div>
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
            <input type="color" class="field-text-color" value="#FFFFFF">
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
                    <option value="ticker">Ticker</option>
                    <option value="breathe">Breathe</option>
                    <option value="pulse">Pulse</option>
                    <option value="bounce">Bounce</option>
                    <option value="shake">Shake</option>
                    <option value="blink">Blink</option>
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
                <span>Speed <span class="field-motion-speed-display"></span></span>
                <input type="range" class="om-range field-motion-speed" min="0" max="200" step="5" value="100">
            </label>
            <label class="om-field" style="flex: 1;">
                <span>Phase <span class="field-motion-phase-display"></span></span>
                <input type="range" class="om-range field-motion-phase" min="0" max="1" step="0.01" value="0">
            </label>
        </div>
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

    const bgSlideEl = container.querySelector(".field-bg-slide");
    const bgSlideWrapEl = container.querySelector(".editor-bg-slide-wrap");
    const bgVideoEl = container.querySelector(".field-bg-video");
    const bgVideoWrapEl = container.querySelector(".editor-bg-video-wrap");
    const bgPatWrapEl = container.querySelector(".editor-bg-pattern-wrap");
    const bgPatGridEl = container.querySelector(".editor-bg-pattern-grid");
    const bgPatColorAEl = container.querySelector(".field-bg-pat-color-a");
    const bgPatColorBEl = container.querySelector(".field-bg-pat-color-b");
    const bgPatColorBRowEl = container.querySelector(".editor-bg-pattern-tweak-b");
    const bgPatDensityEl = container.querySelector(".field-bg-pat-density");
    const bgPatDensityValEl = container.querySelector(".field-bg-pat-density-val");
    const bgPatDensityLabelEl = container.querySelector(".field-bg-pat-density-label");
    const bgPatDensityRowEl = container.querySelector(".editor-bg-pattern-tweak-d");

    // Wrap the pattern color inputs with the curated palette picker
    // (qarl 2026-05-03 designer dispatch). The per-layer text color
    // is mounted later, inside the layer-card render loop, so each
    // new layer's color input gets the same affordance. Capture the
    // handles so loadForEdit can call .refresh() to update selected
    // indicators after externally-set values — without that, the
    // picker's internal selected state goes stale on slide load.
    // (Refresh-not-input-dispatch because dispatching input during
    // load would trigger the debounced autosave to re-save the slide
    // as-is on every load — 900 ms later, server gets a no-op write.)
    const bgPatColorAPicker = mountColorPicker(bgPatColorAEl);
    const bgPatColorBPicker = mountColorPicker(bgPatColorBEl);

    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const form = container.querySelector(".controls");
    const statusEl = container.querySelector(".editor-status");
    const layersListEl = container.querySelector(".editor-layers-list");
    const addLayerBtn = container.querySelector(".editor-add-layer");

    const state = {
        name: nameEl.value,
        backgroundColor: bgPatColorAEl.value,
        bgSource: "pattern",
        bgSlideId: null,
        bgVideoId: null,
        bgImage: null,
        // Pattern-source state (qarl 2026-05-03 designer handoff —
        // replaces the prior gradient state). pattern is one of the
        // 11 names in PATTERN_NAMES; color_a / color_b stay synced
        // with the color inputs; density (0-1 normalized) comes from
        // the range slider scaled by 100. Mutex with the other bg
        // sources — bgSource = "pattern" activates this object,
        // otherwise it's ignored at save time. Default pattern is
        // "solid" (B14, 2026-05-05) — replaces the removed top-level
        // "Solid color" radio; solid pattern uses color_a only.
        bgPattern: {
            pattern: "solid",
            color_a: bgPatColorAEl.value,
            color_b: bgPatColorBEl.value,
            density: (parseInt(bgPatDensityEl.value, 10) || 50) / 100,
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
        // backgroundColor mirrors the solid pattern's color_a so the
        // payload's background_color field stays in sync with what the
        // user effectively sees (pattern-solid replaced the dropped
        // top-level "Solid color" mode in B14).
        if (state.bgSource === "pattern" && state.bgPattern.pattern === "solid") {
            state.backgroundColor = state.bgPattern.color_a;
        }
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
        const textAlignEl = groupEl.querySelector(".field-text-align");
        layer.textAlign = textAlignEl ? textAlignEl.value || "center" : "center";
        const motionEl = groupEl.querySelector(".field-motion");
        layer.motion = motionEl ? motionEl.value || "static" : "static";
        const blendEl = groupEl.querySelector(".field-blend");
        layer.blend = blendEl ? blendEl.value || "normal" : "normal";
        const opacityEl = groupEl.querySelector(".field-opacity");
        const parsedOpacity = Number(opacityEl?.value);
        if (Number.isFinite(parsedOpacity)) {
            layer.opacity = Math.max(0, Math.min(1, parsedOpacity / 100));
        }
        const opacityDisplayEl = groupEl.querySelector(".field-opacity-display");
        if (opacityDisplayEl) {
            opacityDisplayEl.textContent = `(${Math.round((layer.opacity ?? 1) * 100)}%)`;
        }
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
        const speedEl = groupEl.querySelector(".field-motion-speed");
        const parsedSpeed = Number(speedEl?.value);
        if (Number.isFinite(parsedSpeed)) {
            layer.motionSpeed = Math.max(0, Math.min(2, parsedSpeed / 100));
        }
        // Update the inline numeric readouts next to the slider labels.
        const intensityDisplayEl = groupEl.querySelector(".field-motion-intensity-display");
        if (intensityDisplayEl) {
            intensityDisplayEl.textContent = `(${layer.motionIntensity ?? 50})`;
        }
        const speedDisplayEl = groupEl.querySelector(".field-motion-speed-display");
        if (speedDisplayEl) {
            speedDisplayEl.textContent = `(${Math.round((layer.motionSpeed ?? 1) * 100)}%)`;
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
        // Curated-palette picker on the per-layer text color input.
        // Each layer card gets its own picker instance (independent
        // selected state). Mount before the input-event listeners
        // below since mountColorPicker dispatches 'input' on swatch
        // click — which would otherwise create a setup-time spurious
        // syncLayerFromForm call. Mounting first means the listeners
        // bind AFTER the widget's wrap-and-hide DOM rearrangement.
        mountColorPicker(textColorEl);
        const fontSizeEl = groupEl.querySelector(".field-font-size");
        const autoModeEl = groupEl.querySelector(".field-auto-mode");
        const autoModeSegEl = groupEl.querySelector(".field-auto-mode-segmented");
        const autoFormatEl = groupEl.querySelector(".field-auto-format");
        const autoModeHintEl = groupEl.querySelector(".field-auto-mode-hint");
        const motionEl = groupEl.querySelector(".field-motion");
        const motionIntensityEl = groupEl.querySelector(".field-motion-intensity");
        const motionPhaseEl = groupEl.querySelector(".field-motion-phase");
        const motionSpeedEl = groupEl.querySelector(".field-motion-speed");
        const blendEl = groupEl.querySelector(".field-blend");
        const opacityEl = groupEl.querySelector(".field-opacity");

        for (const el of [
            textEl, layerNameEl, textColorEl, fontSizeEl, fontFamilyEl,
            motionEl, motionIntensityEl, motionPhaseEl, motionSpeedEl,
            blendEl, opacityEl,
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

        // Text-align segmented control (B4, 2026-05-05). Mirrors the
        // dynamic-text segmented pattern: buttons drive a hidden
        // .field-text-align input that the form-state sync reads from.
        const textAlignSegEl = groupEl.querySelector(".field-text-align-segmented");
        const textAlignEl = groupEl.querySelector(".field-text-align");
        if (textAlignSegEl && textAlignEl) {
            textAlignSegEl.querySelectorAll("button").forEach((btn) => {
                btn.addEventListener("click", () => {
                    const value = btn.dataset.value;
                    if (textAlignEl.value === value) return;
                    textAlignEl.value = value;
                    refreshSegmentedPressed(textAlignSegEl, value);
                    syncLayerFromForm(groupEl);
                    autoSave?.kick();
                });
            });
        }

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
        const textAlignVal = layer.textAlign || "center";
        const textAlignEl = groupEl.querySelector(".field-text-align");
        if (textAlignEl) textAlignEl.value = textAlignVal;
        refreshSegmentedPressed(
            groupEl.querySelector(".field-text-align-segmented"),
            textAlignVal,
        );
        motionEl.value = layer.motion || "static";
        const blendEl = groupEl.querySelector(".field-blend");
        if (blendEl) blendEl.value = layer.blend || "normal";
        const opacityEl = groupEl.querySelector(".field-opacity");
        const opacityVal = Math.round((layer.opacity ?? 1) * 100);
        if (opacityEl) opacityEl.value = String(opacityVal);
        const opacityDisplayEl = groupEl.querySelector(".field-opacity-display");
        if (opacityDisplayEl) opacityDisplayEl.textContent = `(${opacityVal}%)`;
        const intensityEl = groupEl.querySelector(".field-motion-intensity");
        const phaseEl = groupEl.querySelector(".field-motion-phase");
        const speedEl = groupEl.querySelector(".field-motion-speed");
        const intensityVal = layer.motionIntensity ?? 50;
        const phaseVal = layer.motionPhase ?? 0;
        const speedVal = layer.motionSpeed ?? 1.0;
        intensityEl.value = String(intensityVal);
        phaseEl.value = String(phaseVal);
        if (speedEl) speedEl.value = String(Math.round(speedVal * 100));
        groupEl.querySelector(".field-motion-intensity-display").textContent =
            `(${intensityVal})`;
        groupEl.querySelector(".field-motion-phase-display").textContent =
            `(${Number(phaseVal).toFixed(2)})`;
        const speedDisplayEl = groupEl.querySelector(".field-motion-speed-display");
        if (speedDisplayEl) {
            speedDisplayEl.textContent = `(${Math.round(speedVal * 100)}%)`;
        }
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

    // Background-source radios toggle the slide picker. When "slide" or
    // "video" is selected, populate the dropdown lazily (first time only)
    // via fetchItems so a first-mount doesn't burn a fetch on an operator
    // who's going to stick with the default pattern anyway.
    const bgGenerateWrap = container.querySelector(".editor-bg-generate");
    let bgSlidePopulated = false;
    let bgVideoPopulated = false;
    for (const radio of container.querySelectorAll(".field-bg-source")) {
        radio.addEventListener("change", async () => {
            state.bgSource = radio.value;
            bgSlideWrapEl.hidden = state.bgSource !== "slide";
            bgVideoWrapEl.hidden = state.bgSource !== "video";
            bgPatWrapEl.hidden = state.bgSource !== "pattern";
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
            if (state.bgSource === "slide") {
                state.bgVideoId = null;
            } else if (state.bgSource === "video") {
                state.bgSlideId = null;
            } else if (state.bgSource === "pattern") {
                state.bgImage = null;
                state.bgSlideId = null;
                state.bgVideoId = null;
            }
            drawCanvas(canvas, state);
        });
    }

    // Pattern grid: render an 11-tile grid where each tile shows the
    // pattern at the current color_a / color_b. Click sets the active
    // pattern. Re-rendered any time color_a / color_b change so each
    // thumbnail previews how the operator's chosen palette would look
    // in that pattern.
    function renderPatternGrid() {
        bgPatGridEl.innerHTML = "";
        for (const name of PATTERN_NAMES) {
            const tile = document.createElement("button");
            tile.type = "button";
            tile.role = "radio";
            tile.className = "editor-bg-pattern-tile";
            tile.dataset.pattern = name;
            tile.title = PATTERN_LABELS[name];
            const isSelected = state.bgPattern.pattern === name;
            tile.setAttribute("aria-checked", String(isSelected));
            if (isSelected) tile.classList.add("is-selected");
            const swatch = document.createElement("span");
            swatch.className = "editor-bg-pattern-tile-swatch";
            swatch.style.background = buildPatternBg(
                name, state.bgPattern.color_a, state.bgPattern.color_b,
                state.bgPattern.density,
            );
            const label = document.createElement("span");
            label.className = "editor-bg-pattern-tile-label";
            label.textContent = PATTERN_LABELS[name];
            tile.append(swatch, label);
            tile.addEventListener("click", () => {
                state.bgPattern.pattern = name;
                applyPatternTweakRowVisibility();
                renderPatternGrid();
                if (state.bgSource === "pattern") drawCanvas(canvas, state);
                syncSlideFromForm();
            });
            bgPatGridEl.appendChild(tile);
        }
    }

    function applyPatternTweakRowVisibility() {
        const p = state.bgPattern.pattern;
        // Color B greyed out (not hidden — keep layout stable) for
        // patterns that don't use it.
        bgPatColorBRowEl.classList.toggle("is-disabled", !patternUsesColorB(p));
        bgPatColorBEl.disabled = !patternUsesColorB(p);
        // Density row: relabel for gradient ("Angle" instead of
        // "Density"); grey out for solid (which ignores it).
        bgPatDensityLabelEl.textContent = densityLabelFor(p);
        bgPatDensityRowEl.classList.toggle("is-disabled", !patternUsesDensity(p));
        bgPatDensityEl.disabled = !patternUsesDensity(p);
    }

    bgPatColorAEl.addEventListener("input", () => {
        state.bgPattern.color_a = bgPatColorAEl.value;
        renderPatternGrid();
        if (state.bgSource === "pattern") drawCanvas(canvas, state);
        syncSlideFromForm();
    });
    bgPatColorBEl.addEventListener("input", () => {
        state.bgPattern.color_b = bgPatColorBEl.value;
        renderPatternGrid();
        if (state.bgSource === "pattern") drawCanvas(canvas, state);
        syncSlideFromForm();
    });
    bgPatDensityEl.addEventListener("input", () => {
        const v = parseInt(bgPatDensityEl.value, 10) || 0;
        state.bgPattern.density = v / 100;
        bgPatDensityValEl.textContent = String(v);
        renderPatternGrid();
        if (state.bgSource === "pattern") drawCanvas(canvas, state);
        syncSlideFromForm();
    });

    renderPatternGrid();
    applyPatternTweakRowVisibility();

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
            text_align: layer.textAlign || "center",
            motion: layer.motion || "static",
            motion_intensity: layer.motionIntensity ?? 50,
            motion_phase: layer.motionPhase ?? 0,
            motion_speed: layer.motionSpeed ?? 1.0,
            blend: layer.blend || "normal",
            opacity: layer.opacity ?? 1.0,
            visible: layer.visible !== false,
            box: { ...layer.box },
        }));
        const payload = {
            name: state.name || "Untitled",
            background_color: state.backgroundColor.toUpperCase(),
            background_image_slide_id: state.bgSource === "slide" ? state.bgSlideId || null : null,
            background_video_slide_id: state.bgSource === "video" ? state.bgVideoId || null : null,
            background_pattern: state.bgSource === "pattern" ? {
                pattern: state.bgPattern.pattern,
                color_a: state.bgPattern.color_a.toUpperCase(),
                color_b: state.bgPattern.color_b.toUpperCase(),
                density: state.bgPattern.density,
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
        // Default to pattern-solid (replaces the dropped top-level
        // "Solid color" mode in B14 — solid is now reachable as a
        // pattern type).
        const patternRadio = container.querySelector(
            '.field-bg-source[value="pattern"]',
        );
        patternRadio.checked = true;
        bgSlideWrapEl.hidden = true;
        bgVideoWrapEl.hidden = true;
        bgPatWrapEl.hidden = false;
        state.bgSource = "pattern";
        state.bgPattern.pattern = "solid";
        renderPatternGrid();
        applyPatternTweakRowVisibility();
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
            textAlign: wire?.text_align || "center",
            motion: wire?.motion || "static",
            motionIntensity: wire?.motion_intensity ?? 50,
            motionPhase: wire?.motion_phase ?? 0,
            motionSpeed: typeof wire?.motion_speed === "number" ? wire.motion_speed : 1.0,
            blend: wire?.blend || "normal",
            opacity: typeof wire?.opacity === "number" ? wire.opacity : 1.0,
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
        state.backgroundColor = slide.background_color || "#000000";
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
            bgSlideWrapEl.hidden = false;
            bgVideoWrapEl.hidden = true;
            bgPatWrapEl.hidden = true;
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
            bgSlideWrapEl.hidden = true;
            bgVideoWrapEl.hidden = false;
            bgPatWrapEl.hidden = true;
            bgVideoEl.value = String(slide.background_video_slide_id);
            state.bgSource = "video";
            state.bgSlideId = null;
            state.bgVideoId = String(slide.background_video_slide_id);
            state.bgImage = await loadImageForSlide(state.bgVideoId).catch(
                () => null,
            );
        } else {
            // Pattern path -- handles both new pattern-bearing slides
            // and legacy color-only slides (B14 migration: bg_color
            // becomes pattern.color_a with pattern="solid"). Save
            // will persist the migration the next time the user edits.
            const patRadio = container.querySelector(
                '.field-bg-source[value="pattern"]',
            );
            patRadio.checked = true;
            bgSlideWrapEl.hidden = true;
            bgVideoWrapEl.hidden = true;
            bgPatWrapEl.hidden = false;
            state.bgSource = "pattern";
            state.bgSlideId = null;
            state.bgVideoId = null;
            state.bgImage = null;
            const pat = slide.background_pattern;
            state.bgPattern = pat ? {
                pattern: pat.pattern,
                color_a: pat.color_a,
                color_b: pat.color_b,
                density: pat.density,
            } : {
                pattern: "solid",
                color_a: slide.background_color || "#000000",
                color_b: state.bgPattern.color_b,
                density: 0.5,
            };
            bgPatColorAEl.value = state.bgPattern.color_a;
            bgPatColorBEl.value = state.bgPattern.color_b;
            // Refresh the curated-palette pickers so the inline-row +
            // More button paint matches the loaded value. Direct
            // .refresh() instead of dispatching input -- input dispatch
            // would trigger the form-level debounced autosave to re-save
            // the slide as-is 900 ms later.
            bgPatColorAPicker.refresh();
            bgPatColorBPicker.refresh();
            const densityPct = Math.round(state.bgPattern.density * 100);
            bgPatDensityEl.value = String(densityPct);
            bgPatDensityValEl.textContent = String(densityPct);
            applyPatternTweakRowVisibility();
            renderPatternGrid();
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


function loadImageForSlide(slideId) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error("could not load slide image"));
        img.src = `/api/content/${slideId}/asset`;
    });
}
