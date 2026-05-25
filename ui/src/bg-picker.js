// Background-picker dropdown populators. Extracted from editor.js
// (Batch 5.1) so editor.js doesn't carry these stand-alone helpers.
//
// Used by the editor's "background = image slide" / "background =
// video" pickers. Each call clears the <select> and fills it from
// the current content collection. Failure surfaces a message into
// the passed status element so the operator sees the error inline
// rather than the dropdown silently going empty.

// Shared populator for the two type-specialized exports below. The
// public API (populateBgSlideOptions / populateBgVideoOptions) is
// preserved verbatim; this helper just parameterizes the 4 bits that
// differed across the previous two near-identical bodies:
//   - type filter ("image" vs "video")
//   - placeholder label
//   - error-message prefix
//   - per-item display-name fallback chain (image preserves the
//     `item.text` alternative for text-overlay-on-image cases)
async function _populateOptions(selectEl, fetchItems, statusEl, {
    type,
    placeholder,
    errorPrefix,
    nameFor,
}) {
    try {
        const items = await fetchItems();
        selectEl.innerHTML = `<option value="">${placeholder}</option>`;
        for (const item of items) {
            if (item.type !== type) continue;
            const opt = document.createElement("option");
            opt.value = String(item.id);
            opt.textContent = nameFor(item);
            selectEl.appendChild(opt);
        }
    } catch (err) {
        statusEl.textContent = `${errorPrefix}: ${err.message}`;
    }
}

/**
 * Populate `selectEl` with image-type ContentItems from `fetchItems`.
 */
export async function populateBgSlideOptions(selectEl, fetchItems, statusEl) {
    return _populateOptions(selectEl, fetchItems, statusEl, {
        type: "image",
        placeholder: "(pick a slide)",
        errorPrefix: "Could not load slides",
        nameFor: (item) => item.name || item.text || "Untitled",
    });
}

/**
 * Populate `selectEl` with video-type ContentItems from `fetchItems`.
 */
export async function populateBgVideoOptions(selectEl, fetchItems, statusEl) {
    return _populateOptions(selectEl, fetchItems, statusEl, {
        type: "video",
        placeholder: "(pick a video)",
        errorPrefix: "Could not load videos",
        nameFor: (item) => item.name || "Untitled",
    });
}
