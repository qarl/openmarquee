// Regression test for the QA-flagged 2026-04-26 bug: the visual font
// picker fired only `change` on the underlying <select>, but the
// editor's canvas-redraw path (syncAndRender) listens to `input`. So
// clicking a tile updated the saved slide via autosave (which listens
// to both events) but the live preview stayed frozen on the previous
// font — operators would think the picker was broken and walk.
//
// The fix dispatches both `input` and `change` to mimic native <select>
// user-pick semantics. This spec exercises the actual click path
// (fonts.spec.js uses `selectOption` which Playwright simulates with
// both events, so it never hit the bug).

import { expect, test } from "@playwright/test";

test("clicking a font picker tile redraws the canvas with the new face", async ({ page }) => {
    await page.goto("/#/slides/text");
    await page.waitForSelector(".field-font-family");

    // Start with deterministic text so the canvas has shape to compare.
    await page.fill(".field-text", "Hello QA");

    async function canvasHash() {
        return await page.evaluate(() => {
            const c = document.querySelector(".editor-canvas");
            return c.toDataURL().slice(-200);
        });
    }

    const before = await canvasHash();

    // Open the picker, pick a face that's visually distinct from the
    // default sans-serif (VT323 is bitmap-like; impossible to confuse).
    await page.click(".font-picker-trigger");
    await expect(page.locator(".font-picker-popover")).toBeVisible();
    await page.click('.font-picker-tile[data-value="VT323"]');
    await expect(page.locator(".font-picker-popover")).toBeHidden();

    // Wait for the @font-face TTF to finish loading + the redraw to
    // settle. The change-listener path runs document.fonts.load() then
    // calls syncAndRender; budget enough time for both.
    await page.evaluate(async () => {
        await document.fonts.load('400 40px "VT323"');
    });
    await page.waitForTimeout(150);

    const after = await canvasHash();
    expect(after).not.toBe(before);

    // Trigger label syncs to the new face.
    const label = page.locator(".font-picker-trigger-label");
    await expect(label).toHaveText("VT323");
});
