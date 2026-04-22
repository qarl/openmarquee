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
const FONT_FAMILIES = [
    { value: "sans-serif", label: "Sans-serif (default)" },
    { value: "serif", label: "Serif" },
    { value: "monospace", label: "Monospace" },
];

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
        <h2 class="subpage-title">Text Slides</h2>
        <div class="slide-browser-slot"></div>
        <div class="preview-wrap">
            <canvas class="editor-canvas" aria-label="slide preview"></canvas>
        </div>
        <form class="controls" autocomplete="off">
            <div class="row">
                <label class="field">
                    <span>Slide name</span>
                    <input type="text" class="field-name" value="Untitled" maxlength="200">
                </label>
                <label class="field field-duration-wrap">
                    <span>Duration (s)</span>
                    <input type="number" class="field-duration" value="5" min="1" max="300" step="1">
                </label>
            </div>
            <label class="field">
                <span>Text</span>
                <textarea class="field-text" rows="3" placeholder="(enter text here)"></textarea>
            </label>
            <label class="field">
                <span>Dynamic Text</span>
                <select class="field-auto-mode">
                    <option value="" selected>Off</option>
                    <option value="time">Current time</option>
                    <option value="date">Today's date</option>
                    <option value="day">Day of week</option>
                </select>
            </label>
            <label class="field field-auto-format-wrap" hidden>
                <span>Format</span>
                <select class="field-auto-format"></select>
            </label>
            <p class="field-hint field-auto-mode-hint" hidden>
                When Dynamic Text is set, the typed text is a preview-only
                fallback — the device re-renders each second at playback
                time using the configured timezone.
            </p>
            <div class="field">
                <span>Quick colors</span>
                <div class="presets">${presetButtonsHtml()}</div>
            </div>
            <div class="row">
                <label class="field field-color">
                    <span>Text color</span>
                    <input type="color" class="field-text-color" value="#FFFFFF">
                </label>
                <label class="field field-color">
                    <span>Solid background</span>
                    <input type="color" class="field-bg-color" value="#000000">
                </label>
            </div>
            <div class="row">
                <label class="field">
                    <span>Font</span>
                    <select class="field-font-family"></select>
                </label>
                <label class="field field-duration-wrap">
                    <span>Font size (% of width)</span>
                    <input type="number" class="field-font-size" min="1" max="100" step="0.5">
                </label>
            </div>
            <fieldset class="editor-bg-picker">
                <legend>Background source</legend>
                <label class="field-inline">
                    <input type="radio" name="editor-bg-source" class="field-bg-source" value="color" checked>
                    Solid color (above)
                </label>
                <label class="field-inline">
                    <input type="radio" name="editor-bg-source" class="field-bg-source" value="slide">
                    Existing slide
                </label>
                <label class="field editor-bg-slide-wrap" hidden>
                    <span>Saved slide</span>
                    <select class="field-bg-slide"><option value="">(pick a slide)</option></select>
                </label>
                <div class="editor-bg-generate" hidden>
                    <label class="field">
                        <span>Generate a new background (free, via pollinations.ai — 10-30s)</span>
                        <input type="text" class="field-bg-generate-prompt"
                               placeholder="abstract gradient, minimal, signage-friendly"
                               maxlength="4000">
                    </label>
                    <button type="button" class="bg-generate-btn">Generate…</button>
                    <p class="bg-generate-status field-hint" role="status" aria-live="polite"></p>
                </div>
            </fieldset>
            <button type="submit" class="primary field-save">Save slide</button>
            <p class="field-hint">
                <kbd>⌘</kbd> or <kbd>Ctrl</kbd> + <kbd>Enter</kbd> to save.
                <kbd>Esc</kbd> to clear.
            </p>
            <p class="editor-status" role="status" aria-live="polite"></p>
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
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const fontSizeEl = container.querySelector(".field-font-size");
    const autoModeEl = container.querySelector(".field-auto-mode");
    const autoModeHintEl = container.querySelector(".field-auto-mode-hint");
    const autoFormatEl = container.querySelector(".field-auto-format");
    const autoFormatWrapEl = container.querySelector(".field-auto-format-wrap");
    const form = container.querySelector(".controls");
    const statusEl = container.querySelector(".editor-status");
    const saveBtn = container.querySelector(".field-save");

    for (const f of FONT_FAMILIES) {
        const opt = document.createElement("option");
        opt.value = f.value;
        opt.textContent = f.label;
        fontFamilyEl.appendChild(opt);
    }
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
        bgImage: null, // decoded <img> for "slide" mode
        // Edit-mode tracking: when non-null, Save dispatches to
        // onSaveExisting(editingId, payload) instead of onSave.
        editingId: null,
    };

    function updateSaveEnabled() {
        saveBtn.disabled = !state.text.trim() || saveBtn.dataset.inFlight === "1";
    }

    function syncAndRender() {
        state.name = nameEl.value;
        state.text = textEl.value;
        state.textColor = textColorEl.value;
        state.backgroundColor = bgColorEl.value;
        state.fontFamily = fontFamilyEl.value;
        const parsedSize = Number(fontSizeEl.value);
        if (Number.isFinite(parsedSize) && parsedSize > 0) {
            state.fontSizePct = parsedSize;
        }
        drawCanvas(canvas, state);
        updateSaveEnabled();
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
    });

    // Background-source radios toggle the slide picker. When "slide" is
    // selected, populate the dropdown lazily (first time only) via
    // fetchItems so a first-mount doesn't burn a fetch on an operator
    // who's going to stick with solid-color anyway.
    const bgGenerateWrap = container.querySelector(".editor-bg-generate");
    let bgSlidePopulated = false;
    for (const radio of container.querySelectorAll(".field-bg-source")) {
        radio.addEventListener("change", async () => {
            state.bgSource = radio.value;
            bgSlideWrapEl.hidden = state.bgSource !== "slide";
            bgGenerateWrap.hidden =
                state.bgSource !== "slide" || !onGenerateBackground;
            if (state.bgSource === "slide" && fetchItems && !bgSlidePopulated) {
                await populateBgSlideOptions(bgSlideEl, fetchItems, statusEl);
                bgSlidePopulated = true;
            }
            if (state.bgSource === "color") {
                state.bgImage = null;
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

    form.addEventListener("keydown", (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            if (!saveBtn.disabled) form.requestSubmit();
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

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (saveBtn.disabled) return;

        saveBtn.dataset.inFlight = "1";
        updateSaveEnabled();
        statusEl.textContent = "Saving…";
        try {
            // Rasterize the asset at a fixed 4K target so the stored PNG
            // is resolution-independent — playback cover-fits down to the
            // current panel dims at slide entry. drawCanvas reads the
            // canvas's own width/height, so the same scene draws cleanly
            // at any size (font_size_pct is a fraction of canvas.width).
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
                auto_mode: autoModeEl.value || null,
                auto_format: autoModeEl.value ? autoFormatEl.value || null : null,
                duration_ms: Math.round(durationSeconds * 1000),
                png_base64,
            };
            const result = state.editingId && onSaveExisting
                ? await onSaveExisting(state.editingId, payload)
                : await onSave(payload);
            statusEl.textContent = state.editingId
                ? "Updated."
                : "Saved.";
            // After a save we reset to a blank slate — operator's flow
            // is save → tweak next one, not re-save → identical twin.
            resetToBlank();
            return result;
        } catch (err) {
            statusEl.textContent = `Save failed: ${err.message}`;
        } finally {
            delete saveBtn.dataset.inFlight;
            updateSaveEnabled();
        }
    });

    async function resetToBlank() {
        // Sync blank-state setup. Anything here can be safely
        // overridden by a loadForEdit that interleaves later.
        state.editingId = null;
        state.bgImage = null;
        state.bgSlideId = null;
        textEl.value = "";
        autoModeEl.value = "";
        autoModeHintEl.hidden = true;
        populateAutoFormatOptions("");
        const colorRadio = container.querySelector(
            '.field-bg-source[value="color"]',
        );
        colorRadio.checked = true;
        bgSlideWrapEl.hidden = true;
        state.bgSource = "color";
        syncAndRender();

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
            bgSlideEl.value = String(slide.background_image_slide_id);
            state.bgSource = "slide";
            state.bgSlideId = String(slide.background_image_slide_id);
            state.bgImage = await loadImageForSlide(state.bgSlideId).catch(
                () => null,
            );
        } else {
            const colorRadio = container.querySelector(
                '.field-bg-source[value="color"]',
            );
            colorRadio.checked = true;
            bgSlideWrapEl.hidden = true;
            state.bgSource = "color";
            state.bgSlideId = null;
            state.bgImage = null;
        }
        syncAndRender();
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

    // Initial blank state picks up the first default name.
    resetToBlank();
    syncAndRender();

    return {
        loadForEdit,
        reset: resetToBlank,
        refreshBrowser: () => browser?.refresh(),
    };
}

async function populateBgSlideOptions(selectEl, fetchItems, statusEl) {
    try {
        const items = await fetchItems();
        selectEl.innerHTML = '<option value="">(pick a slide)</option>';
        for (const item of items) {
            const opt = document.createElement("option");
            opt.value = String(item.id);
            opt.textContent = item.name || item.text || "Untitled";
            selectEl.appendChild(opt);
        }
    } catch (err) {
        statusEl.textContent = `Could not load slides: ${err.message}`;
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
export function drawCanvas(canvas, state) {
    const ctx = canvas.getContext("2d");
    const {
        text = "",
        textColor = "#FFFFFF",
        backgroundColor = "#000000",
        fontSize,
        fontSizePct,
        fontFamily = "sans-serif",
        bgSource = "color",
        bgImage = null,
    } = state;

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
            fontSizePx = Math.max(4, Math.round((canvas.width * fontSizePct) / 100));
        } else if (Number.isFinite(fontSize) && fontSize > 0) {
            fontSizePx = fontSize;
        } else {
            fontSizePx = pickFontSize(canvas.height);
        }
        ctx.fillStyle = textColor;
        ctx.font = `bold ${fontSizePx}px ${fontFamily}`;
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
