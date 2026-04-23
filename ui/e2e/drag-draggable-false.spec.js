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
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

async function saveTextSlide(page, name) {
    await page.goto("/#/slides/text");
    await page.fill(".editor .field-name", name);
    await page.fill(".editor .field-text", name);
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor .editor-status")).toContainText(/Saved|Updated/);
}

test("pallet-tile <img> has draggable=false so Sortable wins the drag", async ({ page }) => {
    await saveTextSlide(page, "DragMe");
    await page.goto("/#/playlists");
    // Wait until the playlist track has rendered tiles (not just the
    // "+ New" placeholder from the browser).
    const img = page.locator(".pallet-tile:not(.pallet-tile--new) img").first();
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
