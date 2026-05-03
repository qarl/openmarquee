// Curated-palette color picker — qarl 2026-05-03 designer dispatch.
//
// Two-tier widget that wraps a native `<input type="color">`:
//
//   TIER 1 — inline 8-swatch row directly in the form. Click a swatch
//            and the input value updates immediately, no modal.
//   TIER 2 — "More…" button opens a popover containing all 17
//            curated swatches + a custom-hex text input + the native
//            OS color picker as a fallback for arbitrary colors.
//
// Selected state: an orange outline on whichever swatch matches the
// current value. If the current value isn't one of the 17, no inline
// swatch shows selected and the "More" button paints itself in the
// custom color so the operator can see what they have.
//
// Reusable across every color-sample surface in the editor (text
// color, solid bg, pattern Color A / B). Mount with:
//
//   mountColorPicker(inputEl, { onChange });
//
// The function takes ownership of the input — moves it inside a wrapper,
// hides it visually but keeps it in the form's value tree so existing
// listeners on `inputEl` keep firing on swatch click.

// 8 quick-pick swatches shown inline. Order matches the designer's
// row layout. Picked to cover bg-on-fg and fg-on-bg use cases without
// overwhelming the inline row.
export const INLINE_SWATCHES = [
    { name: "Black", hex: "#06070A" },
    { name: "White", hex: "#FFFFFF" },
    { name: "Amber", hex: "#F4B755" },
    { name: "Coral", hex: "#EC666F" },
    { name: "Pink", hex: "#EC6AA5" },
    { name: "Lime", hex: "#C2EE71" },
    { name: "Mint", hex: "#86ED9D" },
    { name: "Sky", hex: "#7FD2FB" },
];

// All 17 curated colors. Layout per designer image:
//   Darks (8):  Ink · Coffee · Espresso · Plum · Navy · Teal · Forest · Burgundy
//   Lights (8): White · Cream · Amber · Orange · Coral · Pink · Lime · Mint
//   Wildcard:   Sky
// The complex picker shows them in this row order.
export const ALL_SWATCHES = [
    // Darks
    { name: "Ink", hex: "#06070A", row: "darks" },
    { name: "Coffee", hex: "#191611", row: "darks" },
    { name: "Espresso", hex: "#260C07", row: "darks" },
    { name: "Plum", hex: "#26061F", row: "darks" },
    { name: "Navy", hex: "#0C1226", row: "darks" },
    { name: "Teal", hex: "#0E2129", row: "darks" },
    { name: "Forest", hex: "#142516", row: "darks" },
    { name: "Burgundy", hex: "#370E15", row: "darks" },
    // Lights
    { name: "White", hex: "#FFFFFF", row: "lights" },
    { name: "Cream", hex: "#F3ECDA", row: "lights" },
    { name: "Amber", hex: "#F4B755", row: "lights" },
    { name: "Orange", hex: "#EE814C", row: "lights" },
    { name: "Coral", hex: "#EC666F", row: "lights" },
    { name: "Pink", hex: "#EC6AA5", row: "lights" },
    { name: "Lime", hex: "#C2EE71", row: "lights" },
    { name: "Mint", hex: "#86ED9D", row: "lights" },
    // Wildcard
    { name: "Sky", hex: "#7FD2FB", row: "wildcard" },
];

// Normalize a hex string for comparison: lowercase, leading '#'.
// Browsers' native color input always returns lowercase #rrggbb;
// Pydantic-stored colors come back uppercase. Normalize both sides
// before matching.
function normHex(s) {
    if (typeof s !== "string") return "";
    let v = s.trim().toLowerCase();
    if (!v.startsWith("#")) v = "#" + v;
    return v;
}

function matchesSwatch(currentHex, swatch) {
    return normHex(currentHex) === normHex(swatch.hex);
}

// Mount the widget. Takes the existing `<input type="color">` and
// replaces its visible affordance with the inline swatch row + More
// button. The input itself stays in the DOM (hidden) so existing
// `inputEl.addEventListener("input", ...)` listeners — which the
// editor uses for autosave + canvas redraw — keep firing as before.
export function mountColorPicker(inputEl, opts = {}) {
    const { onChange } = opts;

    // Wrapper holding the inline row + the (hidden) original input.
    const wrap = document.createElement("div");
    wrap.className = "om-color-picker";
    inputEl.parentNode.insertBefore(wrap, inputEl);
    // Move the original input INTO the wrapper, then hide it. We keep
    // it for two reasons:
    //   (1) existing form-data collection still finds it
    //   (2) the More popover's "open native picker" button can click
    //       it programmatically to surface the OS color dialog
    wrap.appendChild(inputEl);
    inputEl.classList.add("om-color-picker-native");
    // tabindex=-1 keeps it out of keyboard focus order; the visible
    // swatches are the focusable controls.
    inputEl.tabIndex = -1;

    // Inline 8-swatch row.
    const inlineRow = document.createElement("div");
    inlineRow.className = "om-color-picker-inline";
    inlineRow.setAttribute("role", "radiogroup");
    inlineRow.setAttribute("aria-label", "Color");
    wrap.appendChild(inlineRow);

    const swatchEls = [];
    for (const sw of INLINE_SWATCHES) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "om-color-picker-swatch";
        btn.title = `${sw.name} ${sw.hex}`;
        btn.setAttribute("role", "radio");
        btn.dataset.hex = sw.hex;
        btn.style.setProperty("--om-swatch", sw.hex);
        btn.addEventListener("click", () => setValue(sw.hex));
        inlineRow.appendChild(btn);
        swatchEls.push(btn);
    }

    // "More…" button — paints itself in the current color when the
    // current value isn't one of the inline 8, so the operator always
    // sees their actual color somewhere in the row.
    const moreBtn = document.createElement("button");
    moreBtn.type = "button";
    moreBtn.className = "om-color-picker-more";
    moreBtn.title = "More colors";
    moreBtn.textContent = "···";
    moreBtn.addEventListener("click", openComplexPicker);
    wrap.appendChild(moreBtn);

    function refreshSelection() {
        const cur = inputEl.value;
        let matchedInline = false;
        for (const btn of swatchEls) {
            const match = matchesSwatch(cur, { hex: btn.dataset.hex });
            btn.classList.toggle("is-selected", match);
            btn.setAttribute("aria-checked", String(match));
            if (match) matchedInline = true;
        }
        // If the current value isn't in the inline 8, paint the More
        // button so the operator sees their color (state-coupling rule
        // in the spec).
        if (!matchedInline) {
            moreBtn.style.setProperty("--om-swatch", cur);
            moreBtn.classList.add("has-custom-preview");
        } else {
            moreBtn.classList.remove("has-custom-preview");
            moreBtn.style.removeProperty("--om-swatch");
        }
    }

    function setValue(hex) {
        inputEl.value = hex.toLowerCase();
        // Fire a real input event so existing listeners pick it up.
        inputEl.dispatchEvent(new Event("input", { bubbles: true }));
        refreshSelection();
        if (typeof onChange === "function") onChange(inputEl.value);
    }

    // Listen back so external value changes (loadForEdit, swatch
    // click in another widget that shares state, etc.) update the
    // selected indicator.
    inputEl.addEventListener("input", refreshSelection);
    refreshSelection();

    function openComplexPicker() {
        const dlg = renderComplexPicker(inputEl.value, (hex) => {
            setValue(hex);
        });
        document.body.appendChild(dlg);
        // Position the dialog near the More button.
        const rect = moreBtn.getBoundingClientRect();
        dlg.style.position = "absolute";
        dlg.style.left = `${rect.left + window.scrollX}px`;
        dlg.style.top = `${rect.bottom + window.scrollY + 4}px`;
        // Click-outside dismiss.
        const onDocClick = (e) => {
            if (!dlg.contains(e.target) && e.target !== moreBtn) {
                close();
            }
        };
        const onKey = (e) => {
            if (e.key === "Escape") close();
        };
        function close() {
            dlg.remove();
            document.removeEventListener("click", onDocClick);
            document.removeEventListener("keydown", onKey);
        }
        // Defer so the click that opened the dialog doesn't immediately
        // close it via the bubble.
        setTimeout(() => {
            document.addEventListener("click", onDocClick);
            document.addEventListener("keydown", onKey);
        }, 0);
        dlg.dataset.close = "1";
        dlg.addEventListener("om-color-picker-close", close);
    }

    return {
        refresh: refreshSelection,
        // Useful for tests.
        wrap, inlineRow, moreBtn,
    };
}

// The complex picker popover. Opens with all 17 swatches in 3 rows
// (8 darks, 8 lights, 1 wildcard) plus a custom-hex input + a button
// that triggers the OS native color picker. Selected outline matches
// the current color when it's one of the 17.
function renderComplexPicker(currentHex, onPick) {
    const dlg = document.createElement("div");
    dlg.className = "om-color-picker-popover";
    dlg.setAttribute("role", "dialog");
    dlg.setAttribute("aria-label", "All colors");

    // Group swatches by row.
    const groups = { darks: [], lights: [], wildcard: [] };
    for (const sw of ALL_SWATCHES) groups[sw.row].push(sw);

    for (const rowName of ["darks", "lights", "wildcard"]) {
        const row = document.createElement("div");
        row.className = `om-color-picker-popover-row om-color-picker-popover-${rowName}`;
        for (const sw of groups[rowName]) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "om-color-picker-swatch";
            btn.title = `${sw.name} ${sw.hex}`;
            btn.dataset.hex = sw.hex;
            btn.style.setProperty("--om-swatch", sw.hex);
            if (matchesSwatch(currentHex, sw)) btn.classList.add("is-selected");
            btn.addEventListener("click", () => {
                onPick(sw.hex);
                dlg.dispatchEvent(new CustomEvent("om-color-picker-close"));
            });
            row.appendChild(btn);
        }
        dlg.appendChild(row);
    }

    // Custom-hex input + native OS picker — escape hatches for colors
    // not in the curated 17. The hex input commits on Enter / blur;
    // the native button fires the standard `<input type="color">`.
    const escape = document.createElement("div");
    escape.className = "om-color-picker-popover-escape";

    const hexLabel = document.createElement("label");
    hexLabel.className = "om-color-picker-popover-hex";
    hexLabel.innerHTML = `<span>Custom hex</span>`;
    const hexInput = document.createElement("input");
    hexInput.type = "text";
    hexInput.value = currentHex;
    hexInput.maxLength = 7;
    hexInput.placeholder = "#RRGGBB";
    hexLabel.appendChild(hexInput);
    function commitHex() {
        const v = normHex(hexInput.value);
        if (/^#[0-9a-f]{6}$/i.test(v)) {
            onPick(v);
            dlg.dispatchEvent(new CustomEvent("om-color-picker-close"));
        }
    }
    hexInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            commitHex();
        }
    });
    hexInput.addEventListener("blur", commitHex);

    const nativeBtn = document.createElement("button");
    nativeBtn.type = "button";
    nativeBtn.className = "om-color-picker-popover-native";
    nativeBtn.textContent = "OS picker…";
    const nativeInput = document.createElement("input");
    nativeInput.type = "color";
    nativeInput.value = currentHex;
    nativeInput.style.position = "absolute";
    nativeInput.style.opacity = "0";
    nativeInput.style.pointerEvents = "none";
    nativeBtn.appendChild(nativeInput);
    nativeBtn.addEventListener("click", () => nativeInput.click());
    nativeInput.addEventListener("input", () => {
        onPick(nativeInput.value);
        dlg.dispatchEvent(new CustomEvent("om-color-picker-close"));
    });

    escape.appendChild(hexLabel);
    escape.appendChild(nativeBtn);
    dlg.appendChild(escape);

    return dlg;
}
