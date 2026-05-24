// Slide-event handler helpers extracted from main.js's boot() flow.
//
// main.js wires three `openmarquee:*` custom events (edit-slide,
// delete-slide, settings-updated). The handlers themselves stay inside
// boot() so they close over the boot-scoped panel handles. This module
// holds the two pieces of logic that are pure enough to test in
// isolation:
//
//   - `buildEditRoutes(getHandles)` — returns the slide-type → route
//     table the edit-slide handler dispatches against.
//   - `runDeleteCascade(targets)` — the Promise.all over every panel
//     that lists slides; called by the delete-slide handler after
//     `deleteContent` resolves.
//
// `wireSlideEventHandlers` is intentionally NOT extracted: doing so
// would require either a `getHandles`-style accessor for every closure
// dep (heavy) or freezing the handles at boot time (would break the
// settings-updated re-mount that reassigns `editor` / `imageUploader`
// / etc.). The handler bodies in main.js boot() are 25 LOC each + a
// 5-second code review.
//
// `getHandles` indirection: panel handles are reassigned each time
// `mountDimensionedPanels` runs (boot + every settings-updated). The
// route-table closures must read the CURRENT handle at dispatch time,
// not a snapshot at boot. The accessor pattern preserves that.

/**
 * Build the edit-route table. Each route has:
 *   - `section` — URL hash slug (e.g. "slides/image") for navigation
 *   - `load(slide)` — invokes the matching uploader's loadForEdit
 *
 * @param {() => object} getHandles — accessor returning current panel
 *   handles ({editor, imageUploader, videoUploader, streamUploader,
 *   webSlideEditor}). Read at load-call time so re-mount swaps are
 *   transparent. Each handle's `loadForEdit` is invoked with the slide.
 * @returns {Record<string, { section: string, load: (slide) => unknown }>}
 */
export function buildEditRoutes(getHandles) {
    return {
        text_slide: {
            section: "slides/text",
            load: (slide) => getHandles().editor?.loadForEdit?.(slide),
        },
        image: {
            section: "slides/image",
            load: (slide) => getHandles().imageUploader?.loadForEdit?.(slide),
        },
        video: {
            section: "slides/video",
            load: (slide) => getHandles().videoUploader?.loadForEdit?.(slide),
        },
        stream: {
            section: "slides/stream",
            load: (slide) => getHandles().streamUploader?.loadForEdit?.(slide),
        },
        web: {
            section: "slides/web",
            load: (slide) => getHandles().webSlideEditor?.loadForEdit?.(slide),
        },
    };
}

/**
 * After a slide is deleted, every surface that lists slides needs to
 * refresh so the deleted tile vanishes without a page reload. The 9
 * targets are: the playlist track, the 5 uploaders' browsers, the
 * slides-shell counts, the sidebar counts, and the inline preview.
 *
 * Parallel via Promise.all — operator-visible latency is the slowest
 * single refresh, not the sum. Each target is optional-chained so a
 * panel that hasn't mounted yet (or doesn't expose the expected
 * method) is a no-op rather than a throw.
 *
 * Rejection behavior: if ANY refresh rejects, the cascade rejects.
 * That's Promise.all's default — caller (main.js's delete handler) is
 * responsible for what to do with it. The current handler at main.js
 * lets the rejection propagate as an unhandled promise rejection; a
 * future cascade-with-tolerance refactor would change this contract.
 *
 * @param {object} targets — panel-handle map. Each property optional.
 */
export async function runDeleteCascade(targets) {
    await Promise.all([
        targets.playlistTrack?.refresh?.(),
        targets.editor?.refreshBrowser?.(),
        targets.imageUploader?.refreshBrowser?.(),
        targets.videoUploader?.refreshBrowser?.(),
        targets.streamUploader?.refreshBrowser?.(),
        targets.webSlideEditor?.refreshBrowser?.(),
        targets.slidesShell?.refreshCounts?.(),
        targets.refreshSidebarCounts?.(),
        targets.inlinePreviewHandle?.refresh?.(),
    ]);
}
