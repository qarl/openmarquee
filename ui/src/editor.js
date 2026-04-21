// Text-slide editor: form controls on one side, live canvas preview on the
// other. Canvas is at the sign's native resolution; the browser scales it up
// for display via CSS (image-rendering: pixelated) so what you see is what
// the sign will show.

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
            <label class="field">
                <span>Slide name</span>
                <input type="text" class="field-name" value="Untitled" maxlength="200">
            </label>
            <button type="submit" class="primary field-save">Save slide</button>
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
    const form = container.querySelector(".controls");
    const statusEl = container.querySelector(".editor-status");
    const saveBtn = container.querySelector(".field-save");

    const state = {
        name: nameEl.value,
        text: "",
        textColor: textColorEl.value,
        backgroundColor: bgColorEl.value,
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
        drawCanvas(canvas, state);
        updateSaveEnabled();
    }

    for (const el of [textEl, textColorEl, bgColorEl, nameEl]) {
        el.addEventListener("input", syncAndRender);
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (saveBtn.disabled) return;

        saveBtn.dataset.inFlight = "1";
        updateSaveEnabled();
        statusEl.textContent = "Saving…";
        try {
            const png_base64 = canvasToBase64(canvas);
            await onSave({
                name: state.name || "Untitled",
                text: state.text,
                text_color: state.textColor.toUpperCase(),
                background_color: state.backgroundColor.toUpperCase(),
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
    const { text = "", textColor = "#FFFFFF", backgroundColor = "#000000" } = state;

    ctx.save();
    try {
        ctx.fillStyle = backgroundColor;
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        if (!text) return;

        const fontSizePx = pickFontSize(canvas.height);
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
