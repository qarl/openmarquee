import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("app loads on the Text subpage with the text editor visible", async ({ page }) => {
    await page.goto("/");

    await expect(page).toHaveTitle("openMarquee");
    // Sidebar wordmark is the brand chrome (no <header><h1>); the redesign
    // moved branding into the .om-side wordmark + topbar.
    // The 2026-04-28 design landed a stacked wordmark — "OPEN" sits
    // above "Marquee" instead of running them together. Playwright's
    // toContainText normalizes the inter-element whitespace to a
    // single space, so the rendered match is "OPEN Marquee" (with a
    // space). \s* lets either layout pass.
    await expect(page.locator(".om-wordmark")).toContainText(/open\s*marquee/i);

    await expect(page.locator(".editor .editor-canvas")).toBeVisible();
    await expect(page.locator(".editor .field-text")).toBeVisible();
    // Autosave model: status pill stands in for the old explicit Save button.
    await expect(page.locator(".editor .editor-status")).toBeAttached();

    // Image uploader lives on its own slides sub-tab — hidden on boot.
    await expect(page.locator(".image-upload")).toBeHidden();
});

test("autosave is suppressed on a fresh editor until text is entered", async ({ page }) => {
    // Replaces the legacy "Save button disabled until text is entered" test.
    // Editor's `canSave` gate is `state.editingId || state.text.trim().length > 0`,
    // so a brand-new editor with no text never POSTs a junk slide on first
    // focus. Confirms that gate via /api/content (no slides created until
    // text lands).
    await page.goto("/#/slides/text");
    await expect(page.locator(".editor .field-name")).toHaveValue(/Text Slide \d+/);

    // Wait past the autosave debounce; nothing should have been saved.
    await page.waitForTimeout(1500);
    let items = await (await page.request.get("/api/content")).json();
    expect(items).toHaveLength(0);

    // Type text → autosave fires → exactly one slide lands on the server.
    await page.fill(".editor .field-text", "Hi");
    await expect(page.locator(".editor .editor-status"))
        .toContainText(/Saved/, { timeout: 5_000 });
    items = await (await page.request.get("/api/content")).json();
    expect(items).toHaveLength(1);
});

test("sidebar nav routes through every top-level section on click", async ({ page }) => {
    // Sidebar collapsed Text/Image/Video into a single Slides entry with an
    // in-page tab subnav (slides.js). Test the top-level routes here; the
    // tab subnav is exercised by the slides-shell vitest coverage.
    await page.goto("/");

    // Slides is the default landing section.
    await expect(page.locator('.panel[data-section="slides"]')).toBeVisible();
    await expect(page.locator('.nav-link[data-section="slides"]')).toHaveClass(/active/);
    await expect(page.locator('.panel[data-section="playlists"]')).toBeHidden();

    const routes = ["playlists", "flock", "schedule", "settings", "slides"];
    for (const name of routes) {
        await page.locator(`.nav-link[data-section="${name}"]`).click();
        await expect(page.locator(`.panel[data-section="${name}"]`)).toBeVisible();
        await expect(page.locator(`.nav-link[data-section="${name}"]`)).toHaveClass(/active/);
    }

    // Slides shell exposes a 3-tab subnav (text / image / video). Default is
    // text; clicking image / video swaps the active .tab-pane. Hash routing
    // keeps `#/slides/<tab>` URLs working — covered separately by the
    // slides-shell vitest.
    await page.locator('.nav-link[data-section="slides"]').click();
    await expect(page.locator('.tab-pane[data-tab="text"]')).toBeVisible();
    await expect(page.locator('.tab-pane[data-tab="image"]')).toBeHidden();

    // Settings still renders the editable form hydrated from /api/settings.
    await page.locator('.nav-link[data-section="settings"]').click();
    await expect(page.locator("section.settings h1")).toHaveText("Settings");
    await expect(page.locator(".field-output-mode")).toHaveValue("hdmi");
    await expect(page.locator(".field-display-width")).toHaveValue("1920");
});

test("the ffmpeg worker + vendored core assets are bundled into dist/", async ({ page }) => {
    // Worker and core files must exist on the captive portal — otherwise
    // the first click on a pipeline button 404s and the operator sees an
    // ffmpeg.wasm init error. Regression guard for the esbuild worker-
    // entry-point bundling step.
    const worker = await page.request.get("/dist/ffmpeg-worker.js");
    expect(worker.status()).toBe(200);
    const coreJs = await page.request.get("/dist/vendor/ffmpeg-core/ffmpeg-core.js");
    expect(coreJs.status()).toBe(200);
    const coreWasm = await page.request.head("/dist/vendor/ffmpeg-core/ffmpeg-core.wasm");
    expect(coreWasm.status()).toBe(200);
});

test("welcome page renders SSID, password, and a real (not placeholder) QR", async ({ page }) => {
    // Phase 7 swaps in real values from the device side; this smoke test
    // ensures the page chrome stays wired up AND the welcome.js script
    // generates a real QR (not the no-JS placeholder fallback).
    await page.goto("/welcome.html");
    await expect(page).toHaveTitle(/openMarquee/);
    await expect(page.locator(".brand")).toHaveText("openMarquee");
    await expect(page.locator('[data-field="ssid"]')).toBeVisible();
    await expect(page.locator('[data-field="password"]')).toBeVisible();

    // The welcome.js script removes the qr-placeholder class once it has
    // rendered a real QR. If the script ran, the watermark is gone.
    await expect(page.locator(".qr")).not.toHaveClass(/qr-placeholder/);
    // And the SVG inside is the qrcode library's output, not the hand-drawn
    // placeholder pattern (different module count → different viewBox).
    const viewBox = await page.locator(".qr svg").getAttribute("viewBox");
    expect(viewBox).not.toBe("0 0 21 21");
});
