// Compositing editor — the "real" slide designer.
//
// Operators build slides by stacking layers on a canvas:
//   1. A background (solid color, OR the PNG of an existing slide)
//   2. N text layers, each draggable + styled independently
//
// On Save we flatten everything to a PNG at the sign's native resolution
// and POST it as an ImageSlide. No backend changes — the composite slide
// is just another image to the server. The cost is that saved composites
// can't be re-edited layer-by-layer (the PNG is the source of truth);
// operators iterate by cloning and tweaking. Good enough for MVP; a
// future follow-up could persist the layer JSON as metadata.
//
// Rendering is done entirely on the main thread — panel sizes are small
// (SYSTEM_SPEC default 128×96) so a full redraw on every pointer event
// is cheap even without rAF throttling.

const TEMPLATE = `
    <div class="composer">
        <div class="preview-wrap">
            <canvas class="composer-canvas" aria-label="composed slide preview"></canvas>
        </div>
        <form class="controls composer-controls" autocomplete="off">
            <fieldset class="composer-bg">
                <legend>Background</legend>
                <label class="field">
                    <span>Mode</span>
                    <select class="bg-mode">
                        <option value="solid" selected>Solid color</option>
                        <option value="slide">From saved slide</option>
                    </select>
                </label>
                <label class="field field-color bg-color-wrap">
                    <span>Color</span>
                    <input type="color" class="bg-color" value="#000000">
                </label>
                <label class="field bg-slide-wrap" hidden>
                    <span>Saved slide</span>
                    <select class="bg-slide"><option value="">(pick a slide)</option></select>
                </label>
            </fieldset>

            <fieldset class="composer-layers">
                <legend>Text layers</legend>
                <ul class="layers-list" role="list"></ul>
                <button type="button" class="layers-add">+ Add text layer</button>
            </fieldset>

            <div class="row">
                <label class="field">
                    <span>Slide name</span>
                    <input type="text" class="field-name" value="Composite" maxlength="200">
                </label>
                <label class="field field-duration-wrap">
                    <span>Duration (s)</span>
                    <input type="number" class="field-duration" value="5" min="1" max="300" step="1">
                </label>
            </div>
            <button type="submit" class="primary composer-save">Save composite slide</button>
            <p class="composer-status" role="status" aria-live="polite"></p>
        </form>
    </div>
`;

// Inner HTML of a `.layer-card` <li> — the wrapping <li> is set up in code
// so we can attach dataset + listeners without an extra querySelector hop.
const LAYER_INNER = `
    <div class="layer-header">
        <span class="layer-label"></span>
        <button type="button" class="layer-remove danger" aria-label="Remove layer">×</button>
    </div>
    <label class="field">
        <span>Text</span>
        <input type="text" class="layer-text" maxlength="200">
    </label>
    <div class="row">
        <label class="field field-duration-wrap">
            <span>Size</span>
            <input type="number" class="layer-size" min="6" max="200" step="1">
        </label>
        <label class="field field-color">
            <span>Color</span>
            <input type="color" class="layer-color">
        </label>
    </div>
    <div class="row layer-style-row">
        <label class="layer-style-toggle"><input type="checkbox" class="layer-bold"> Bold</label>
        <label class="layer-style-toggle"><input type="checkbox" class="layer-italic"> Italic</label>
        <label class="field">
            <span>Align</span>
            <select class="layer-align">
                <option value="center" selected>Center</option>
                <option value="left">Left</option>
                <option value="right">Right</option>
            </select>
        </label>
    </div>
`;

/**
 * Mount the composer into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {number} options.width
 * @param {number} options.height
 * @param {() => Promise<Array>} options.fetchItems — list of saved slides
 *     (content items) to offer as background candidates. Each item must
 *     have `id` and an asset reachable at /api/content/{id}/asset.
 * @param {(payload: object) => Promise<void>} options.onSave — invoked with
 *     an ImageSlide payload: { name, duration_ms, png_base64 }.
 */
export function mountComposer(container, { width, height, fetchItems, onSave }) {
    container.innerHTML = TEMPLATE;

    const canvas = container.querySelector(".composer-canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = `${width} / ${height}`;

    const bgModeEl = container.querySelector(".bg-mode");
    const bgColorEl = container.querySelector(".bg-color");
    const bgColorWrap = container.querySelector(".bg-color-wrap");
    const bgSlideEl = container.querySelector(".bg-slide");
    const bgSlideWrap = container.querySelector(".bg-slide-wrap");
    const layersListEl = container.querySelector(".layers-list");
    const addLayerBtn = container.querySelector(".layers-add");
    const nameEl = container.querySelector(".field-name");
    const durationEl = container.querySelector(".field-duration");
    const saveBtn = container.querySelector(".composer-save");
    const statusEl = container.querySelector(".composer-status");
    const form = container.querySelector(".controls");

    /** @type {object[]} — render state, the canonical source for redraws. */
    const layers = [];
    // Cached HTMLImageElement for the background slide (decoded PNG).
    let bgImage = null;
    // True between picking a bg slide from the dropdown and the <img>'s load
    // or error event firing — avoids saving a PNG that's out of sync with
    // what the operator sees.
    let bgLoading = false;
    let nextLayerId = 1;

    function syncSaveEnabled() {
        if (bgLoading) {
            saveBtn.disabled = true;
            saveBtn.dataset.reason = "bgLoading";
            statusEl.textContent = "Waiting for background…";
        } else if (saveBtn.dataset.reason === "bgLoading") {
            saveBtn.disabled = false;
            delete saveBtn.dataset.reason;
            if (statusEl.textContent === "Waiting for background…") {
                statusEl.textContent = "";
            }
        }
    }

    function addLayer(initial = {}) {
        const id = `L${nextLayerId++}`;
        const layer = {
            id,
            text: initial.text ?? "HELLO",
            x: initial.x ?? width / 2,
            y: initial.y ?? height / 2,
            fontSize: initial.fontSize ?? Math.max(12, Math.floor(height * 0.35)),
            color: initial.color ?? "#FFFFFF",
            bold: initial.bold ?? true,
            italic: initial.italic ?? false,
            align: initial.align ?? "center",
        };
        layers.push(layer);
        renderLayersList();
        redraw();
    }

    function removeLayer(id) {
        const idx = layers.findIndex((l) => l.id === id);
        if (idx >= 0) {
            layers.splice(idx, 1);
            renderLayersList();
            redraw();
        }
    }

    function renderLayersList() {
        layersListEl.innerHTML = "";
        layers.forEach((layer, i) => {
            const li = document.createElement("li");
            li.className = "layer-card";
            li.dataset.id = layer.id;
            li.innerHTML = LAYER_INNER;

            li.querySelector(".layer-label").textContent = `Layer ${i + 1}`;
            const textEl = li.querySelector(".layer-text");
            const sizeEl = li.querySelector(".layer-size");
            const colorEl = li.querySelector(".layer-color");
            const boldEl = li.querySelector(".layer-bold");
            const italicEl = li.querySelector(".layer-italic");
            const alignEl = li.querySelector(".layer-align");
            textEl.value = layer.text;
            sizeEl.value = String(layer.fontSize);
            colorEl.value = layer.color;
            boldEl.checked = layer.bold;
            italicEl.checked = layer.italic;
            alignEl.value = layer.align;

            textEl.addEventListener("input", () => { layer.text = textEl.value; redraw(); });
            sizeEl.addEventListener("input", () => {
                const n = Number(sizeEl.value);
                if (Number.isFinite(n) && n > 0) layer.fontSize = n;
                redraw();
            });
            colorEl.addEventListener("input", () => { layer.color = colorEl.value; redraw(); });
            boldEl.addEventListener("input", () => { layer.bold = boldEl.checked; redraw(); });
            italicEl.addEventListener("input", () => { layer.italic = italicEl.checked; redraw(); });
            alignEl.addEventListener("change", () => { layer.align = alignEl.value; redraw(); });
            li.querySelector(".layer-remove").addEventListener("click", () => removeLayer(layer.id));

            layersListEl.appendChild(li);
        });
    }

    // --- background wiring ---

    bgModeEl.addEventListener("change", () => {
        if (bgModeEl.value === "slide") {
            bgSlideWrap.hidden = false;
            bgColorWrap.hidden = true;
            // Lazy-populate on first switch.
            if (bgSlideEl.options.length <= 1) populateBgSlideDropdown();
        } else {
            bgSlideWrap.hidden = true;
            bgColorWrap.hidden = false;
            bgImage = null;
            redraw();
        }
    });
    bgColorEl.addEventListener("input", redraw);
    bgSlideEl.addEventListener("change", loadBgSlide);

    async function populateBgSlideDropdown() {
        try {
            const items = await fetchItems();
            bgSlideEl.innerHTML = '<option value="">(pick a slide)</option>';
            for (const item of items) {
                const opt = document.createElement("option");
                opt.value = String(item.id);
                opt.textContent = item.name || item.text || "Untitled";
                bgSlideEl.appendChild(opt);
            }
        } catch (err) {
            statusEl.textContent = `Could not load slides: ${err.message}`;
        }
    }

    function loadBgSlide() {
        const id = bgSlideEl.value;
        if (!id) {
            bgImage = null;
            bgLoading = false;
            syncSaveEnabled();
            redraw();
            return;
        }
        bgLoading = true;
        syncSaveEnabled();
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.onload = () => {
            bgImage = img;
            bgLoading = false;
            syncSaveEnabled();
            redraw();
        };
        img.onerror = () => {
            bgImage = null;
            bgLoading = false;
            syncSaveEnabled();
            statusEl.textContent = "Could not load background image.";
            redraw();
        };
        img.src = `/api/content/${id}/asset`;
    }

    // --- drag to position ---

    let drag = null;
    canvas.addEventListener("pointerdown", (event) => {
        const { logicalX, logicalY } = toLogical(canvas, event, width, height);
        // Hit-test topmost first so clicking on an overlap grabs the front layer.
        for (let i = layers.length - 1; i >= 0; i--) {
            const layer = layers[i];
            if (hitTestLayer(canvas, layer, logicalX, logicalY)) {
                drag = {
                    layerId: layer.id,
                    offsetX: logicalX - layer.x,
                    offsetY: logicalY - layer.y,
                };
                canvas.setPointerCapture(event.pointerId);
                event.preventDefault();
                return;
            }
        }
    });
    canvas.addEventListener("pointermove", (event) => {
        if (!drag) return;
        const { logicalX, logicalY } = toLogical(canvas, event, width, height);
        const layer = layers.find((l) => l.id === drag.layerId);
        if (!layer) return;
        // Clamp to the text anchor, not the text bbox. With a non-center
        // `align` or a large fontSize, rendered text can extend past the
        // canvas edge — operators see the clipping live and can adjust.
        layer.x = clamp(logicalX - drag.offsetX, 0, width);
        layer.y = clamp(logicalY - drag.offsetY, 0, height);
        redraw();
    });
    canvas.addEventListener("pointerup", (event) => {
        if (drag) {
            canvas.releasePointerCapture(event.pointerId);
            drag = null;
        }
    });

    // --- save ---

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (saveBtn.disabled) return;
        saveBtn.disabled = true;
        statusEl.textContent = "Saving…";
        try {
            const png_base64 = canvasToBase64(canvas);
            const durationSeconds = Number(durationEl.value) || 5;
            await onSave({
                name: nameEl.value || "Composite",
                duration_ms: Math.round(durationSeconds * 1000),
                png_base64,
            });
            statusEl.textContent = "Saved.";
        } catch (err) {
            statusEl.textContent = `Save failed: ${err.message}`;
        } finally {
            saveBtn.disabled = false;
        }
    });

    // --- rendering ---

    function redraw() {
        drawComposite(canvas, {
            width,
            height,
            bgMode: bgModeEl.value,
            bgColor: bgColorEl.value,
            bgImage,
            layers,
        });
    }

    addLayerBtn.addEventListener("click", () => addLayer());

    // Seed with one layer so the simple case (type + save) still works
    // without clicking Add.
    addLayer();
}

function toLogical(canvas, event, width, height) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = width / rect.width;
    const scaleY = height / rect.height;
    return {
        logicalX: (event.clientX - rect.left) * scaleX,
        logicalY: (event.clientY - rect.top) * scaleY,
    };
}

function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
}

// Approximate the layer's bbox by measuring the text once and padding a bit,
// so the user gets a generous hit target on small panel sizes.
function hitTestLayer(canvas, layer, x, y) {
    const ctx = canvas.getContext("2d");
    applyLayerFont(ctx, layer);
    const m = ctx.measureText(layer.text || " ");
    // On most browsers measureText gives width + rough ascents; fall back to
    // fontSize if the metric is missing (older Safari).
    const w = Math.max(m.width, layer.fontSize);
    const h = (m.actualBoundingBoxAscent || layer.fontSize * 0.8)
        + (m.actualBoundingBoxDescent || layer.fontSize * 0.2);
    const pad = Math.max(4, layer.fontSize * 0.2);

    let left = layer.x;
    if (layer.align === "center") left = layer.x - w / 2;
    else if (layer.align === "right") left = layer.x - w;

    return (
        x >= left - pad
        && x <= left + w + pad
        && y >= layer.y - h / 2 - pad
        && y <= layer.y + h / 2 + pad
    );
}

function applyLayerFont(ctx, layer) {
    const weight = layer.bold ? "bold" : "normal";
    const style = layer.italic ? "italic" : "normal";
    ctx.font = `${style} ${weight} ${layer.fontSize}px sans-serif`;
    ctx.textBaseline = "middle";
    ctx.textAlign = layer.align || "center";
}

/**
 * Pure-ish rendering: draws background (solid or image) then each text
 * layer onto the canvas. Exported for unit-test convenience.
 */
export function drawComposite(canvas, { width, height, bgMode, bgColor, bgImage, layers }) {
    const ctx = canvas.getContext("2d");
    ctx.save();
    try {
        if (bgMode === "slide" && bgImage) {
            // Draw the slide PNG scaled to the target panel — matches how
            // the device's renderer would see it.
            ctx.drawImage(bgImage, 0, 0, width, height);
        } else {
            ctx.fillStyle = bgColor || "#000000";
            ctx.fillRect(0, 0, width, height);
        }

        for (const layer of layers) {
            if (!layer.text) continue;
            applyLayerFont(ctx, layer);
            ctx.fillStyle = layer.color;
            ctx.fillText(layer.text, layer.x, layer.y, width);
        }
    } finally {
        ctx.restore();
    }
}

/** Base64-encode the canvas's current pixels as a PNG body (no data: prefix). */
export function canvasToBase64(canvas) {
    const dataUrl = canvas.toDataURL("image/png");
    return dataUrl.split(",")[1];
}
