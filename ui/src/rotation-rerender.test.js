// Tests for the FYS bug 7 rotation thumbnail bulk re-render.
//
// rerenderAllSlidesForRotation takes a `deps` object so the per-type
// re-render + the content fetches can be stubbed — no canvas / network.

import { describe, expect, it, vi } from "vitest";

import { rerenderAllSlidesForRotation } from "./rotation-rerender.js";

function makeDeps(items, overrides = {}) {
    const rerenderTextSlide = overrides.rerenderTextSlide
        ?? vi.fn(async () => {});
    const rerenderVideoSlide = overrides.rerenderVideoSlide
        ?? vi.fn(async () => {});
    return {
        listContent: overrides.listContent ?? vi.fn(async () => items),
        // The single-item re-fetch just echoes the list item by id.
        fetchContentItem: overrides.fetchContentItem
            ?? vi.fn(async (id) => items.find((i) => String(i.id) === String(id))),
        rerenderTextSlide,
        rerenderVideoSlide,
    };
}

describe("rerenderAllSlidesForRotation", () => {
    it("re-renders text + video slides, skips image + stream", async () => {
        const items = [
            { id: "t1", type: "text_slide" },
            { id: "v1", type: "video" },
            { id: "i1", type: "image_slide" },
            { id: "s1", type: "stream" },
        ];
        const deps = makeDeps(items);

        const summary = await rerenderAllSlidesForRotation(90, deps);

        expect(deps.rerenderTextSlide).toHaveBeenCalledTimes(1);
        expect(deps.rerenderVideoSlide).toHaveBeenCalledTimes(1);
        // Rotation is threaded through to the per-type re-render.
        expect(deps.rerenderTextSlide).toHaveBeenCalledWith(
            expect.objectContaining({ id: "t1" }),
            90,
        );
        expect(summary).toEqual({
            total: 4,
            rerendered: 2,
            skipped: 2,
            failed: 0,
        });
    });

    it("a single slide failing does not abort the rest", async () => {
        const items = [
            { id: "t1", type: "text_slide" },
            { id: "t2", type: "text_slide" },
            { id: "t3", type: "text_slide" },
        ];
        const rerenderTextSlide = vi.fn(async (item) => {
            if (item.id === "t2") throw new Error("boom");
        });
        const deps = makeDeps(items, { rerenderTextSlide });

        const summary = await rerenderAllSlidesForRotation(270, deps);

        // All three attempted; t2 failed, t1 + t3 still re-rendered.
        expect(rerenderTextSlide).toHaveBeenCalledTimes(3);
        expect(summary).toEqual({
            total: 3,
            rerendered: 2,
            skipped: 0,
            failed: 1,
        });
    });

    it("returns an empty summary when listing content fails", async () => {
        const deps = makeDeps([], {
            listContent: vi.fn(async () => {
                throw new Error("network down");
            }),
        });

        const summary = await rerenderAllSlidesForRotation(180, deps);

        expect(summary).toEqual({
            total: 0,
            rerendered: 0,
            skipped: 0,
            failed: 0,
        });
        expect(deps.rerenderTextSlide).not.toHaveBeenCalled();
    });

    it("skips list entries with no id", async () => {
        const items = [
            { type: "text_slide" }, // no id
            { id: "t1", type: "text_slide" },
        ];
        const deps = makeDeps(items);

        const summary = await rerenderAllSlidesForRotation(90, deps);

        expect(deps.rerenderTextSlide).toHaveBeenCalledTimes(1);
        expect(summary.total).toBe(2);
        expect(summary.rerendered).toBe(1);
        expect(summary.skipped).toBe(1);
    });
});
