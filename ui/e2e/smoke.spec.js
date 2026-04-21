import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("app loads, editor + list visible, list shows empty state", async ({ page }) => {
    await page.goto("/");

    await expect(page).toHaveTitle("OpenMarquee");
    await expect(page.locator("header h1")).toHaveText("OpenMarquee");

    await expect(page.locator(".editor-canvas")).toBeVisible();
    await expect(page.locator(".field-text")).toBeVisible();
    await expect(page.locator(".field-save")).toBeVisible();

    await expect(page.locator(".list")).toBeVisible();
    await expect(page.locator(".list-status")).toContainText("No slides yet");
});

test("Save button is disabled until text is entered", async ({ page }) => {
    await page.goto("/");

    const saveBtn = page.locator(".field-save");
    await expect(saveBtn).toBeDisabled();

    await page.locator(".field-text").fill("Hi");
    await expect(saveBtn).toBeEnabled();

    await page.locator(".field-text").fill("");
    await expect(saveBtn).toBeDisabled();
});
