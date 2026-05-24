// Settings save triggers a live re-mount of the dims-dependent panels
// so the editor canvas, live preview, and pallet tiles all match the
// new display config without a page reload.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("saving a new display_width re-mounts the editor with the fresh canvas size", async ({ page }) => {
    await page.goto("/");

    // The editor canvas at boot is whatever /api/settings reports.
    // Defaults now match the default output mode (HDMI → 1920×1080).
    const canvas = page.locator(".editor .editor-canvas");
    await expect(canvas).toBeVisible();
    const initialWidth = await canvas.getAttribute("width");
    const initialHeight = await canvas.getAttribute("height");
    expect(Number(initialWidth)).toBe(1920);
    expect(Number(initialHeight)).toBe(1080);

    // Navigate to Settings, flip the display dims.
    await page.locator('.nav-link[data-section="settings"]').click();
    await page.locator(".field-display-width").fill("192");
    await page.locator(".field-display-height").fill("64");
    // No explicit save click — settings.js attaches attachAutoSave to
    // each field; the fill() above already fired input events, which
    // debounce-trigger the save (auto-save.js default 600ms).
    await expect(page.locator(".settings-status"))
        .toHaveAttribute("data-state", "saved", { timeout: 5_000 });

    // Back to the text subpage — the editor should have been
    // re-mounted. Its canvas attribute reflects the new dims.
    await page.locator('.nav-link[data-section="slides"]').click();
    await page.locator('.om-subnav button[data-tab="text"]').click();
    const canvasAfter = page.locator(".editor .editor-canvas");
    await expect(canvasAfter).toHaveAttribute("width", "192");
    await expect(canvasAfter).toHaveAttribute("height", "64");
});

test("re-mount wipes the editor's in-progress draft", async ({ page }) => {
    // A settings change is infrequent and usually means "I want the
    // new dims." Re-mounting at new dims blanks any partial slide
    // rather than trying to preserve text at the wrong canvas size.
    // Documented trade-off — locking it in with a test.
    await page.goto("/");
    const textArea = page.locator(".editor .field-text");
    await textArea.fill("draft-that-will-be-lost");

    await page.locator('.nav-link[data-section="settings"]').click();
    await page.locator(".field-display-width").fill("64");
    await page.locator(".field-display-height").fill("32");
    // No explicit save click — settings.js attaches attachAutoSave to
    // each field; the fill() above already fired input events, which
    // debounce-trigger the save (auto-save.js default 600ms).
    await expect(page.locator(".settings-status"))
        .toHaveAttribute("data-state", "saved", { timeout: 5_000 });

    await page.locator('.nav-link[data-section="slides"]').click();
    await page.locator('.om-subnav button[data-tab="text"]').click();
    // Draft is gone (empty textarea).
    await expect(page.locator(".editor .field-text")).toHaveValue("");
});

test("image + video uploader canvases also pick up the new dims", async ({ page }) => {
    await page.goto("/");
    await page.locator('.nav-link[data-section="settings"]').click();
    await page.locator(".field-display-width").fill("256");
    await page.locator(".field-display-height").fill("128");
    // No explicit save click — settings.js attaches attachAutoSave to
    // each field; the fill() above already fired input events, which
    // debounce-trigger the save (auto-save.js default 600ms).
    await expect(page.locator(".settings-status"))
        .toHaveAttribute("data-state", "saved", { timeout: 5_000 });

    await page.locator('.nav-link[data-section="slides"]').click();
    await page.locator('.om-subnav button[data-tab="image"]').click();
    const imgCanvas = page.locator(".image-upload .image-upload-canvas");
    await expect(imgCanvas).toHaveAttribute("width", "256");
    await expect(imgCanvas).toHaveAttribute("height", "128");

    await page.locator('.om-subnav button[data-tab="video"]').click();
    // Video uploader's preview is a <video> (not a canvas) — its shape
    // is driven by inline `style.aspectRatio`, set from the panel dims
    // at mount time. The thumbnail-generation canvas is offscreen.
    const vidPreview = page.locator(".video-upload .video-upload-video");
    await expect(vidPreview).toHaveAttribute("style", /aspect-ratio:\s*256\s*\/\s*128/);
});
