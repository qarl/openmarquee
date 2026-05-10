// Background-picker dropdown populators. Extracted from editor.js
// (Batch 5.1) so editor.js doesn't carry these stand-alone helpers.
//
// Used by the editor's "background = image slide" / "background =
// video" pickers. Each call clears the <select> and fills it from
// the current content collection. Failure surfaces a message into
// the passed status element so the operator sees the error inline
// rather than the dropdown silently going empty.

/**
 * Populate `selectEl` with image-type ContentItems from `fetchItems`.
 */
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

/**
 * Populate `selectEl` with video-type ContentItems from `fetchItems`.
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
