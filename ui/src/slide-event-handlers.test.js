// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import {
    buildEditRoutes,
    runDeleteCascade,
} from "./slide-event-handlers.js";

describe("buildEditRoutes", () => {
    it("returns a route entry for every supported slide type", () => {
        const routes = buildEditRoutes(() => ({}));
        // Five known types as of 2026-05-24: text_slide / image / video /
        // stream / web. A new type added to the operator UI would need to
        // grow this list -- the test failure tells the next author "add
        // your type here too".
        expect(Object.keys(routes).sort()).toEqual([
            "image",
            "stream",
            "text_slide",
            "video",
            "web",
        ]);
    });

    it("each route carries the `slides/<key>` URL section + a load function", () => {
        const routes = buildEditRoutes(() => ({}));
        // The section format is the contract with nav.js (which prefix-
        // walks "slides/*" to the parent "slides" section) AND with
        // slides.js (whose hashchange listener tabs to the matching
        // child). A divergent section format silently breaks both.
        expect(routes.text_slide.section).toBe("slides/text");
        expect(routes.image.section).toBe("slides/image");
        expect(routes.video.section).toBe("slides/video");
        expect(routes.stream.section).toBe("slides/stream");
        expect(routes.web.section).toBe("slides/web");
        for (const route of Object.values(routes)) {
            expect(typeof route.load).toBe("function");
        }
    });

    it("load(slide) routes to the matching handle's loadForEdit", () => {
        const editor = { loadForEdit: vi.fn() };
        const imageUploader = { loadForEdit: vi.fn() };
        const videoUploader = { loadForEdit: vi.fn() };
        const streamUploader = { loadForEdit: vi.fn() };
        const webSlideEditor = { loadForEdit: vi.fn() };
        const routes = buildEditRoutes(() => ({
            editor,
            imageUploader,
            videoUploader,
            streamUploader,
            webSlideEditor,
        }));
        const textSlide = { id: "t1", type: "text_slide" };
        const imageSlide = { id: "i1", type: "image" };
        const videoSlide = { id: "v1", type: "video" };
        const streamSlide = { id: "s1", type: "stream" };
        const webSlide = { id: "w1", type: "web" };

        routes.text_slide.load(textSlide);
        routes.image.load(imageSlide);
        routes.video.load(videoSlide);
        routes.stream.load(streamSlide);
        routes.web.load(webSlide);

        expect(editor.loadForEdit).toHaveBeenCalledWith(textSlide);
        expect(imageUploader.loadForEdit).toHaveBeenCalledWith(imageSlide);
        expect(videoUploader.loadForEdit).toHaveBeenCalledWith(videoSlide);
        expect(streamUploader.loadForEdit).toHaveBeenCalledWith(streamSlide);
        expect(webSlideEditor.loadForEdit).toHaveBeenCalledWith(webSlide);
        // No cross-talk: each spy fired exactly once.
        for (const spy of [
            editor.loadForEdit,
            imageUploader.loadForEdit,
            videoUploader.loadForEdit,
            streamUploader.loadForEdit,
            webSlideEditor.loadForEdit,
        ]) {
            expect(spy).toHaveBeenCalledTimes(1);
        }
    });

    it("re-reads handles via getHandles on every load() (supports settings-updated re-mount)", () => {
        // boot-time mountDimensionedPanels assigns `editor`. settings-
        // updated re-runs mountDimensionedPanels and REASSIGNS `editor`
        // to a fresh instance. The route's load must invoke the CURRENT
        // editor, not the boot-time snapshot. The getHandles accessor
        // preserves that; a naive destructure-at-build-time would silently
        // dispatch every future edit to the dead first-mount editor.
        let editor = { loadForEdit: vi.fn() };
        const routes = buildEditRoutes(() => ({ editor }));
        const slide = { id: "x" };

        routes.text_slide.load(slide);
        expect(editor.loadForEdit).toHaveBeenCalledTimes(1);

        // Simulate a re-mount: the outer `editor` rebinds to a new object.
        const firstEditor = editor;
        editor = { loadForEdit: vi.fn() };

        routes.text_slide.load(slide);
        // The OLD editor's spy did NOT get a second call.
        expect(firstEditor.loadForEdit).toHaveBeenCalledTimes(1);
        // The NEW editor's spy was reached via the getter.
        expect(editor.loadForEdit).toHaveBeenCalledTimes(1);
    });

    it("load() is a no-op (no throw) when the handle is missing or not yet mounted", () => {
        // Production reality: an edit-slide event can race against boot
        // (the user clicks ✎ before mountDimensionedPanels resolves).
        // The ?. chain in buildEditRoutes turns this into a silent no-op
        // rather than a thrown TypeError that would surface as an
        // unhandled rejection in the operator's console.
        const routes = buildEditRoutes(() => ({}));
        expect(() => routes.text_slide.load({ id: "x" })).not.toThrow();
        expect(routes.text_slide.load({ id: "x" })).toBeUndefined();
        expect(() => routes.web.load({ id: "x" })).not.toThrow();
    });
});

describe("runDeleteCascade", () => {
    function buildTargets() {
        // The 9 cascade entry-points -- matches main.js's delete handler
        // shape. Each method returns a resolved promise so the cascade
        // settles immediately in the call-count tests.
        return {
            playlistTrack: { refresh: vi.fn().mockResolvedValue(undefined) },
            editor: { refreshBrowser: vi.fn().mockResolvedValue(undefined) },
            imageUploader: { refreshBrowser: vi.fn().mockResolvedValue(undefined) },
            videoUploader: { refreshBrowser: vi.fn().mockResolvedValue(undefined) },
            streamUploader: { refreshBrowser: vi.fn().mockResolvedValue(undefined) },
            webSlideEditor: { refreshBrowser: vi.fn().mockResolvedValue(undefined) },
            slidesShell: { refreshCounts: vi.fn().mockResolvedValue(undefined) },
            refreshSidebarCounts: vi.fn().mockResolvedValue(undefined),
            inlinePreviewHandle: { refresh: vi.fn().mockResolvedValue(undefined) },
        };
    }

    it("invokes all 9 refresh entry-points exactly once", async () => {
        // Per-target assertions (not a count assertion) so a future
        // addition to the cascade doesn't silently break this test --
        // a new entry would just need its own assertion line, not a
        // count bump that hides the intent.
        const targets = buildTargets();
        await runDeleteCascade(targets);
        expect(targets.playlistTrack.refresh).toHaveBeenCalledTimes(1);
        expect(targets.editor.refreshBrowser).toHaveBeenCalledTimes(1);
        expect(targets.imageUploader.refreshBrowser).toHaveBeenCalledTimes(1);
        expect(targets.videoUploader.refreshBrowser).toHaveBeenCalledTimes(1);
        expect(targets.streamUploader.refreshBrowser).toHaveBeenCalledTimes(1);
        expect(targets.webSlideEditor.refreshBrowser).toHaveBeenCalledTimes(1);
        expect(targets.slidesShell.refreshCounts).toHaveBeenCalledTimes(1);
        expect(targets.refreshSidebarCounts).toHaveBeenCalledTimes(1);
        expect(targets.inlinePreviewHandle.refresh).toHaveBeenCalledTimes(1);
    });

    it("runs all 9 refreshes in parallel (Promise.all, not sequential await)", async () => {
        // Real parallelism check: each refresh fn captures its own
        // resolver but does NOT auto-resolve. After kickoff, every
        // refresh should have STARTED (incremented startedCount) before
        // we resolve any of them. If the implementation accidentally
        // serialized (e.g. a `for...of await` rewrite), only the first
        // would start until we resolved it.
        let startedCount = 0;
        const resolvers = [];
        const makeBlocking = () =>
            vi.fn(() => new Promise((resolve) => {
                startedCount++;
                resolvers.push(resolve);
            }));
        const targets = {
            playlistTrack: { refresh: makeBlocking() },
            editor: { refreshBrowser: makeBlocking() },
            imageUploader: { refreshBrowser: makeBlocking() },
            videoUploader: { refreshBrowser: makeBlocking() },
            streamUploader: { refreshBrowser: makeBlocking() },
            webSlideEditor: { refreshBrowser: makeBlocking() },
            slidesShell: { refreshCounts: makeBlocking() },
            refreshSidebarCounts: makeBlocking(),
            inlinePreviewHandle: { refresh: makeBlocking() },
        };

        const cascadePromise = runDeleteCascade(targets);
        // Drain the microtask queue so every refresh has had a chance
        // to be invoked. None will RESOLVE yet -- the resolvers are
        // still parked. If parallel, all 9 should be started.
        await Promise.resolve();
        expect(startedCount).toBe(9);

        // Now let the cascade finish so vitest's afterEach doesn't see
        // a leaked pending promise.
        for (const resolve of resolvers) resolve();
        await cascadePromise;
    });

    it("tolerates missing targets without throwing (cascade fires before all panels mount)", async () => {
        // Production reality: an `openmarquee:delete-slide` can fire
        // before every panel's mount has resolved. Each entry is `?.`
        // chained so a missing handle is a clean no-op.
        await expect(runDeleteCascade({})).resolves.toBeUndefined();
        await expect(
            runDeleteCascade({
                playlistTrack: { refresh: vi.fn().mockResolvedValue(undefined) },
                // The other 8 entries are deliberately undefined.
            }),
        ).resolves.toBeUndefined();
    });

    it("tolerates handle objects missing the expected method", async () => {
        // Half-mounted handle (the object exists but the method isn't
        // wired yet -- e.g. an async initializer that resolves the handle
        // before its surface is fully populated) shouldn't throw.
        await expect(
            runDeleteCascade({
                playlistTrack: {}, // no refresh()
                editor: { refreshBrowser: vi.fn().mockResolvedValue(undefined) },
                refreshSidebarCounts: undefined,
            }),
        ).resolves.toBeUndefined();
    });

    it("propagates rejection when ANY refresh fails (default Promise.all semantics)", async () => {
        // Locking the current contract: a refresh failure surfaces to
        // the delete handler in main.js. Future refactor that wants
        // tolerance (cascade-keeps-going-on-failure) would have to
        // change this test intentionally -- which is the point.
        const targets = buildTargets();
        targets.editor.refreshBrowser = vi
            .fn()
            .mockRejectedValue(new Error("editor refresh failed"));
        await expect(runDeleteCascade(targets)).rejects.toThrow(
            "editor refresh failed",
        );
    });
});
