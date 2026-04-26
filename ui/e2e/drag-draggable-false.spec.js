// Regression for bug (#7): "drag and drop does not work for all
// thumbnails." Root cause was that <img> elements default to
// draggable=true, which races Sortable's pointer-event handling and
// can preempt the drag start — the browser's native "drag this image"
// gesture runs instead. Fix: explicitly set draggable="false" on the
// thumbnail <img> everywhere.
//
// Driving Sortable.js end-to-end from Playwright is flaky (see the
// comment in text-slide.spec.js:68); this test guards the pre-requisite
// that was failing upstream instead.

import { expect, test } from "@playwright/test";
import { resetServerState, saveTextSlide } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("pallet-tile <img> has draggable=false so Sortable wins the drag", async ({ page }) => {
    await saveTextSlide(page, "DragMe");
    await page.goto("/#/playlists");
    // The +New placeholder tile inside the slide-browser was retired in the
    // redesign; the pallet only renders real slides now. First .pallet-tile
    // is the saved slide.
    const img = page.locator(".pallet-tile img").first();
    await expect(img).toBeVisible();
    const draggable = await img.evaluate((el) => el.draggable);
    expect(draggable).toBe(false);
});

test("slide-browser-tile <img> has draggable=false", async ({ page }) => {
    await saveTextSlide(page, "DragMe");
    const img = page.locator(".editor .slide-browser-tile img").first();
    await expect(img).toBeVisible();
    const draggable = await img.evaluate((el) => el.draggable);
    expect(draggable).toBe(false);
});
