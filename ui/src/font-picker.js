// Font-picker UI + canvas-side helpers. Extracted from editor.js
// (Batch 5.1) so the bundled-family list + the click-to-pick
// popover live in one focused module. Sites that need the canonical
// list (editor, rasterize, anywhere wanting font_family resolution)
// import from here.
//
// FONT_FAMILIES.weight = numeric CSS font-weight to request in
// `ctx.font`. Using each face's *native* weight avoids browser-
// synthesized fake bold on single-weight display fonts (Pacifico,
// Bebas Neue, etc.).

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

export const FONT_WEIGHT_BY_VALUE = new Map(
    FONT_FAMILIES.map((f) => [f.value, f.weight]),
);

/**
 * Return a CSS font-family string safe for use in `ctx.font` /
 * `style.font`. Generic keywords (sans-serif, serif, monospace) must
 * NOT be quoted; named families with spaces must be. Canvas will
 * silently drop an unquoted "Bebas Neue" otherwise.
 *
 * Appends "Noto Color Emoji" as the canvas-side fallback so the
 * browser picks emoji glyphs from the bundled color-emoji TTF
 * (loaded via @font-face in styles.css, populated at build time by
 * scripts/download-emoji-font.sh) rather than the system's default
 * emoji font. Pairs with the device-side codepoint segmentation in
 * backend/openmarquee/seed.py:_draw_text_runs so editor preview and
 * device output use the same color glyphs.
 */
export function cssFontFamily(value) {
    const GENERICS = new Set([
        "sans-serif", "serif", "monospace", "cursive", "fantasy",
        "system-ui", "ui-sans-serif", "ui-serif", "ui-monospace",
    ]);
    const primary = GENERICS.has(value) ? value : `"${value}"`;
    return `${primary}, "Noto Color Emoji"`;
}

/**
 * Wire up the visual font picker around a layer's hidden <select> +
 * trigger button + popover. Idempotent per `layerEl`: each layer
 * gets its own popover wired once at insert time.
 */
export function setupFontPicker(layerEl) {
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
