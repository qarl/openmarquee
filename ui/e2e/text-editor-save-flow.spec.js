// Regression tests for two text-editor bugs:
//
//   (#2) Pressing <Enter> inside a single-line input must NOT submit
//        the form. It used to — which triggered a save that reset the
//        slide state, wiping whatever the operator was mid-typing.
//        Textarea Enter still means newline. (Now mostly moot since
//        autosave replaced the explicit Save button, but the Enter-no-
//        submit guard still pays for itself any time someone wires a
//        new submit handler.)
//
//   (#3) After a successful save, the editor should remain on the
//        slide it just saved. It used to clear to a blank "new slide,"
//        so an operator tweaking + re-saving the same slide would
//        create a duplicate every time. Autosave preserves editingId
//        across the round-trip, so a follow-up edit PATCHes the same id.

import { expect, test } from "@playwright/test";
import { resetServerState, saveTextSlide } from "./_helpers.js";

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
    // Even with autosave wired, the form must not submit on Enter — that
    // path used to call resetToBlank() and wipe the operator's draft.
    await expect(page.locator(".editor .field-text")).toHaveValue("hello world");
    await expect(page.locator(".editor .field-name")).toHaveValue("OriginalName");
});

test("Enter in the textarea still inserts a newline", async ({ page }) => {
    await page.goto("/#/slides/text");
    await page.locator(".editor .field-text").focus();
    await page.locator(".editor .field-text").pressSequentially("line1");
    await page.keyboard.press("Enter");
    await page.locator(".editor .field-text").pressSequentially("line2");
    await expect(page.locator(".editor .field-text")).toHaveValue("line1\nline2");
});

test("Autosave stays on the just-saved slide (does not clear to a blank)", async ({ page }) => {
    await saveTextSlide(page, "SaveStayTest", { text: "first version" });
    // Editor should still reflect the just-saved slide.
    await expect(page.locator(".editor .field-name")).toHaveValue("SaveStayTest");
    await expect(page.locator(".editor .field-text")).toHaveValue("first version");
    // The matching tile should be highlighted.
    const tile = page.locator('.slide-browser-tile[data-id]', { hasText: "SaveStayTest" });
    await expect(tile).toHaveClass(/slide-browser-tile--selected/);
});

test("Re-edit autosaves into the same slide — no duplicate in the browser", async ({ page }) => {
    await saveTextSlide(page, "NoDup", { text: "v1" });
    // Edit the text — autosave will PATCH the existing id (editingId is
    // promoted by performSave on first success), not POST a new slide.
    await page.fill(".editor .field-text", "v2");
    // Poll the API directly: the canonical signal that the second save
    // hit is `text === "v2"` on the existing item. Avoids racing the
    // status pill, which is sticky for 2.4s after the first save.
    await expect.poll(
        async () => {
            const items = await (await page.request.get("/api/content")).json();
            const noDup = items.find((it) => it.name === "NoDup");
            // §5.10a v3: text lives on text_layers[0], not the slide root.
            return noDup?.text_layers?.[0]?.text;
        },
        { timeout: 5_000 },
    ).toBe("v2");
    // Only one tile named "NoDup" exists in the browser.
    const tiles = page.locator('.slide-browser-tile[data-id]', { hasText: "NoDup" });
    await expect(tiles).toHaveCount(1);
});
