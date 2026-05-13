// Wire-shape ContentItem → drawCanvas state-shape converter.
//
// drawCanvas (rasterize.js) accepts the editor's internal `state`
// object: { backgroundColor, bgSource, bgPattern, bgImage, layers }.
// The on-the-wire ContentItem produced by the backend uses different
// field names (background_color, background_pattern, text_layers) and
// signals bg source by which of background_color / background_pattern
// / background_image_slide_id is populated, not by a single bgSource
// enum. This helper bridges the two so any surface that draws a
// wire-shape item (parity harness, inline preview during playback)
// can route through drawCanvas instead of re-implementing bg
// resolution + text layout per-call.
//
// Originally inlined in parity-harness.html; extracted (qarl
// 2026-05-13) so inline-preview.drawTextSlideAnimated can reuse it,
// closing the bug where the animated preview silently dropped
// background_pattern.
//
// `resolvedBgImage` is an optional HTMLImageElement the caller
// resolved from background_image_slide_id. Pass null when no bg
// slide is referenced (or when the bg slide image isn't loaded yet
// — drawCanvas falls back through bgSource priority anyway).

export function stateFromItem(item, resolvedBgImage = null) {
    const state = {
        backgroundColor: item.background_color || "#000000",
        bgSource: "color",
        bgPattern: null,
        bgImage: null,
        layers: Array.isArray(item.text_layers) ? item.text_layers : [],
    };
    if (item.background_image_slide_id && resolvedBgImage) {
        state.bgSource = "slide";
        state.bgImage = resolvedBgImage;
    } else if (item.background_pattern) {
        state.bgSource = "pattern";
        state.bgPattern = {
            pattern: item.background_pattern.pattern,
            color_a: item.background_pattern.color_a,
            color_b: item.background_pattern.color_b,
            density: item.background_pattern.density,
        };
    }
    return state;
}
