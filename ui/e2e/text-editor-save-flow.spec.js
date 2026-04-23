// Regression tests for two text-editor bugs:
//
//   (#2) Pressing <Enter> inside a single-line input must NOT submit
//        the form. It used to — which triggered a save that reset the
//        slide state, wiping whatever the operator was mid-typing.
//        Textarea Enter still means newline.
//
//   (#3) After a successful save, the editor should remain on the
//        slide it just saved. It used to clear to a blank "new slide,"
//        so an operator tweaking + re-saving the same slide would
//        create a duplicate every time.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

async function fillAndFocusTextInput(page) {
    await page.goto("/#/slides/text");
    // Wait for the editor's initial async tail (fetchItems → auto-name)
    // to land before filling — otherwise it races us and overwrites the
    // name field with "Text Slide 1" after our fill.
    await expect(page.locator(".editor .field-name")).toHaveValue(/Text Slide \d+/);
    await page.fill(".editor .field-name", "OriginalName");
    await page.fill(".editor .field-text", "hello world");
    // Focus a SINGLE-LINE input (the name field) so Enter would
    // otherwise submit the form.
    await page.locator(".editor .field-name").focus();
}

test("Enter in the name input does NOT submit / reset editor state", async ({ page }) => {
    await fillAndFocusTextInput(page);
    await page.keyboard.press("Enter");
    // The text field should keep its value — if the form submitted,
    // resetToBlank() (the old behavior) would have emptied it.
    await expect(page.locator(".editor .field-text")).toHaveValue("hello world");
    await expect(page.locator(".editor .field-name")).toHaveValue("OriginalName");
    // Status line should be blank (no "Saved." / "Updated.").
    await expect(page.locator(".editor .editor-status")).toHaveText("");
});

test("Enter in the textarea still inserts a newline", async ({ page }) => {
    await page.goto("/#/slides/text");
    await page.locator(".editor .field-text").focus();
    await page.locator(".editor .field-text").pressSequentially("line1");
    await page.keyboard.press("Enter");
    await page.locator(".editor .field-text").pressSequentially("line2");
    await expect(page.locator(".editor .field-text")).toHaveValue("line1\nline2");
});

test("Save stays on the just-saved slide (does not clear to a blank)", async ({ page }) => {
    await page.goto("/#/slides/text");
    await page.fill(".editor .field-name", "SaveStayTest");
    await page.fill(".editor .field-text", "first version");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor .editor-status")).toContainText(/Saved|Updated/i);
    // Editor should still reflect the just-saved slide.
    await expect(page.locator(".editor .field-name")).toHaveValue("SaveStayTest");
    await expect(page.locator(".editor .field-text")).toHaveValue("first version");
    // The matching tile should be highlighted.
    const tile = page.locator('.slide-browser-tile[data-id]', { hasText: "SaveStayTest" });
    await expect(tile).toHaveClass(/slide-browser-tile--selected/);
});

test("Re-save updates the same slide — no duplicate in the browser", async ({ page }) => {
    await page.goto("/#/slides/text");
    await page.fill(".editor .field-name", "NoDup");
    await page.fill(".editor .field-text", "v1");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor .editor-status")).toContainText(/Saved/i);
    // Edit the text and save again.
    await page.fill(".editor .field-text", "v2");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor .editor-status")).toContainText(/Updated/i);
    // Only one tile named "NoDup" should exist in the browser.
    const tiles = page.locator('.slide-browser-tile[data-id]', { hasText: "NoDup" });
    await expect(tiles).toHaveCount(1);
});
