// Text-slide editor: form controls on one side, live canvas preview on the
// other. Canvas is at the sign's native resolution; the browser scales it up
// for display via CSS (image-rendering: pixelated) so what you see is what
// the sign will show.

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
        <div class="preview-wrap">
            <canvas class="editor-canvas" aria-label="slide preview"></canvas>
        </div>
        <form class="controls" autocomplete="off">
            <label class="field">
                <span>Text</span>
                <textarea class="field-text" rows="3" placeholder="GRAND OPENING"></textarea>
            </label>
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
                    <span>Background</span>
                    <input type="color" class="field-bg-color" value="#000000">
                </label>
            </div>
            <div class="row">
                <label class="field">
                    <span>Slide name</span>
                    <input type="text" class="field-name" value="Untitled" maxlength="200">
                </label>
                <label class="field field-duration-wrap">
                    <span>Duration (s)</span>
                    <input type="number" class="field-duration" value="5" min="1" max="300" step="1">
                </label>
                <label class="field field-duration-wrap">
                    <span>Font size (px)</span>
                    <input type="number" class="field-font-size" min="4" max="2048" step="1">
                </label>
            </div>
            <div class="row">
                <label class="field">
                    <span>Transition into next</span>
                    <select class="field-transition">
                        <option value="cut" selected>Cut (instant)</option>
                        <option value="fade">Fade</option>
                    </select>
                </label>
                <label class="field field-duration-wrap">
                    <span>Fade time (ms)</span>
                    <input type="number" class="field-transition-ms" value="500" min="0" max="5000" step="50">
                </label>
            </div>
            <label class="field">
                <span>Dynamic content (rendered at playback)</span>
                <select class="field-auto-mode">
                    <option value="" selected>Off — use the typed text</option>
                    <option value="time">Current time</option>
                    <option value="date">Today's date</option>
                    <option value="day">Day of week</option>
                </select>
            </label>
            <p class="field-hint field-auto-mode-hint" hidden>
                When dynamic content is set, the typed text is a preview-only
                fallback — the device re-renders at playback time with the
                current value. (Playback-time rendering is a follow-up; today
                only the metadata persists.)
            </p>
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
 * Mount the text-slide editor into `container`.
 *
 * @param {HTMLElement} container — parent element (emptied and replaced).
 * @param {object} options
 * @param {number} options.width — target sign width in pixels.
 * @param {number} options.height — target sign height in pixels.
 * @param {(payload: object) => Promise<void>} options.onSave — called with
 *     the serialized slide payload when the user hits Save.
 */
export function mountEditor(container, { width, height, onSave }) {
    container.innerHTML = EDITOR_TEMPLATE;

    const canvas = container.querySelector(".editor-canvas");
    canvas.width = width;
    canvas.height = height;
    // Pin the visible aspect ratio to the actual canvas dimensions so a
    // 64x32 panel doesn't get displayed as 4:3.
    canvas.style.aspectRatio = `${width} / ${height}`;

    const textEl = container.querySelector(".field-text");
    const textColorEl = container.querySelector(".field-text-color");
    const bgColorEl = container.querySelector(".field-bg-color");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const fontSizeEl = container.querySelector(".field-font-size");
    const autoModeEl = container.querySelector(".field-auto-mode");
    const autoModeHintEl = container.querySelector(".field-auto-mode-hint");
    const transitionEl = container.querySelector(".field-transition");
    const transitionMsEl = container.querySelector(".field-transition-ms");
    const form = container.querySelector(".controls");
    const statusEl = container.querySelector(".editor-status");
    const saveBtn = container.querySelector(".field-save");

    // Seed font-size from the existing heuristic so a first-time user
    // sees reasonable output without adjusting anything; they can override.
    fontSizeEl.value = String(pickFontSize(height));

    const state = {
        name: nameEl.value,
        text: "",
        textColor: textColorEl.value,
        backgroundColor: bgColorEl.value,
        fontSize: Number(fontSizeEl.value),
    };

    function updateSaveEnabled() {
        // Empty text isn't worth a slide; in-flight saves shouldn't double-fire.
        saveBtn.disabled = !state.text.trim() || saveBtn.dataset.inFlight === "1";
    }

    function syncAndRender() {
        state.name = nameEl.value;
        state.text = textEl.value;
        state.textColor = textColorEl.value;
        state.backgroundColor = bgColorEl.value;
        const parsedSize = Number(fontSizeEl.value);
        if (Number.isFinite(parsedSize) && parsedSize > 0) {
            state.fontSize = parsedSize;
        }
        drawCanvas(canvas, state);
        updateSaveEnabled();
    }

    for (const el of [textEl, textColorEl, bgColorEl, nameEl, fontSizeEl]) {
        el.addEventListener("input", syncAndRender);
    }

    // Auto-mode: show/hide the hint and let the user know their typed
    // text is a fallback when dynamic content is selected. No live render
    // in the preview — we don't want the canvas to tick with the clock
    // while the operator is still editing styling.
    autoModeEl.addEventListener("change", () => {
        autoModeHintEl.hidden = !autoModeEl.value;
    });

    // Keyboard shortcuts: Cmd/Ctrl+Enter to save from anywhere in the form.
    form.addEventListener("keydown", (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            if (!saveBtn.disabled) form.requestSubmit();
        }
    });

    // Escape in the text area clears it (but keeps the styling + name so the
    // user can iterate quickly on the same slide settings).
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
            // Dispatch synthetic input events so any future listener
            // (validation, dirty-state, undo) sees preset clicks the same as
            // user edits.
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
            const png_base64 = canvasToBase64(canvas);
            // Seconds → ms; clamp lightly so an empty input becomes the default.
            const durationSeconds = Number(durationEl.value) || 5;
            const transitionMs = Number(transitionMsEl.value);
            await onSave({
                name: state.name || "Untitled",
                text: state.text,
                text_color: state.textColor.toUpperCase(),
                background_color: state.backgroundColor.toUpperCase(),
                font_size_px: Math.round(state.fontSize),
                auto_mode: autoModeEl.value || null,
                duration_ms: Math.round(durationSeconds * 1000),
                transition: transitionEl.value,
                transition_ms: Number.isFinite(transitionMs) ? transitionMs : 500,
                png_base64,
            });
            statusEl.textContent = "Saved.";
            textEl.value = "";
            state.text = "";
            syncAndRender();
        } catch (err) {
            statusEl.textContent = `Save failed: ${err.message}`;
        } finally {
            delete saveBtn.dataset.inFlight;
            updateSaveEnabled();
        }
    });

    syncAndRender();
}

/**
 * Draw the slide onto `canvas`. Pure in the sense that it only reads
 * `state` and writes pixels — no DOM wiring, no event handlers. Wraps
 * the body in save()/restore() so callers (e.g. list thumbnails sharing
 * an offscreen canvas) don't see leaked context state.
 */
export function drawCanvas(canvas, state) {
    const ctx = canvas.getContext("2d");
    const {
        text = "",
        textColor = "#FFFFFF",
        backgroundColor = "#000000",
        fontSize,
    } = state;

    ctx.save();
    try {
        ctx.fillStyle = backgroundColor;
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        if (!text) return;

        // Operator-chosen font size when present; fall back to the old
        // ~40% heuristic so render-only callers (list thumbnails) keep
        // working without threading a size through.
        const fontSizePx =
            Number.isFinite(fontSize) && fontSize > 0
                ? fontSize
                : pickFontSize(canvas.height);
        ctx.fillStyle = textColor;
        ctx.font = `bold ${fontSizePx}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";

        // Multiline support — handle \r\n from iOS paste too.
        const lines = text.split(/\r?\n/);
        const lineHeight = fontSizePx * 1.1;
        const totalHeight = lineHeight * lines.length;
        const startY = canvas.height / 2 - totalHeight / 2 + lineHeight / 2;
        // maxWidth shrinks long lines horizontally instead of overflowing.
        const maxWidth = Math.max(1, canvas.width - 4);
        for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], canvas.width / 2, startY + i * lineHeight, maxWidth);
        }
    } finally {
        ctx.restore();
    }
}

/**
 * Pick a font size that's roughly readable at the target panel height.
 * Simple heuristic for now; Phase 3 polish can replace this with real
 * auto-fit once we see what real slides need.
 */
export function pickFontSize(panelHeight) {
    // ~40% of the panel height per line feels right for a single short line
    // at small panel sizes, scales up sensibly for HDMI resolutions.
    return Math.max(12, Math.floor(panelHeight * 0.4));
}

/**
 * Serialize the canvas's current pixels to a base64 PNG body (no data: prefix).
 */
export function canvasToBase64(canvas) {
    const dataUrl = canvas.toDataURL("image/png");
    return dataUrl.split(",")[1];
}
