import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("save a text slide → it shows up in the Playlists pallet + the asset serves", async ({ page }) => {
    await page.goto("/");

    // Type a slide on the Text subpage (default route).
    await page.locator(".editor .field-name").fill("Opening");
    await page.locator(".editor .field-text").fill("GRAND OPENING");
    await page.locator(".editor .field-text-color").evaluate((el) => {
        el.value = "#ffffff";
        el.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await page.locator(".editor .field-bg-color").evaluate((el) => {
        el.value = "#cc0000";
        el.dispatchEvent(new Event("input", { bubbles: true }));
    });

    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor-status")).toHaveText("Saved.");

    // Over on the Playlists subpage, the new slide is in the pallet.
    await page.locator('.nav-link[data-section="playlists"]').click();
    await expect(page.locator('.panel[data-section="playlists"]')).toBeVisible();

    const palletTiles = page.locator(".pallet-tile");
    await expect(palletTiles).toHaveCount(1);
    await expect(palletTiles.first().locator(".pallet-tile-name")).toHaveText(
        "Opening",
    );

    // And the asset URL returns a real PNG.
    const thumb = palletTiles.first().locator(".pallet-tile-thumb");
    await expect(thumb).toBeVisible();
    const thumbResp = await page.request.get(await thumb.getAttribute("src"));
    expect(thumbResp.status()).toBe(200);
    expect(thumbResp.headers()["content-type"]).toBe("image/png");
});

test("two text slides both land in the pallet", async ({ page }) => {
    await page.goto("/");

    for (const [i, name] of ["Open", "Closed"].entries()) {
        // Save-flow fix (bug #3): saves stay on the just-saved slide.
        // Click "+ New" between slides to get a fresh blank editor.
        if (i > 0) {
            await page.locator(".editor .slide-browser-tile--new .slide-browser-tile-action").click();
            // resetToBlank has an async tail that fills the name field
            // with the next auto-name ("Text Slide N"). Wait for that to
            // land so the test's .fill() below isn't overwritten by it.
            await expect(page.locator(".editor .field-name")).toHaveValue(/Text Slide \d+/);
        }
        await page.locator(".editor .field-name").fill(name);
        await page.locator(".editor .field-text").fill(name.toUpperCase());
        await page.locator(".editor .field-save").click();
        await expect(page.locator(".editor-status")).toContainText(/Saved|Updated/);
    }

    await page.locator('.nav-link[data-section="playlists"]').click();
    await expect(page.locator(".pallet-tile")).toHaveCount(2);
    const names = await page.locator(".pallet-tile-name").allTextContents();
    expect(new Set(names)).toEqual(new Set(["Open", "Closed"]));
});

test("rejected save (text too long) surfaces the error", async ({ page }) => {
    await page.goto("/");
    await page.locator(".editor .field-name").fill("Big");
    await page.locator(".editor .field-text").fill("X".repeat(10_001));
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor-status")).toContainText("Save failed");
});

test("playlist PUT reorders content via the API (what drag-reorder invokes)", async ({
    page,
}) => {
    // The UI drag-reorder flow ends in a PUT /api/playlist with the new id
    // order. Driving Sortable's pointer-event internals from Playwright is
    // flaky, so we verify the contract the drag handler depends on: PUT
    // the new order via the exact same client path, and GET reflects it.
    await page.goto("/");

    for (const [i, name] of ["First", "Second", "Third"].entries()) {
        if (i > 0) {
            await page.locator(".editor .slide-browser-tile--new .slide-browser-tile-action").click();
            // resetToBlank has an async tail that fills the name field
            // with the next auto-name ("Text Slide N"). Wait for that to
            // land so the test's .fill() below isn't overwritten by it.
            await expect(page.locator(".editor .field-name")).toHaveValue(/Text Slide \d+/);
        }
        await page.locator(".editor .field-name").fill(name);
        await page.locator(".editor .field-text").fill(name);
        await page.locator(".editor .field-save").click();
        await expect(page.locator(".editor-status")).toContainText(/Saved|Updated/);
    }

    const content = await (await page.request.get("/api/content")).json();
    expect(content.map((item) => item.name)).toEqual(["First", "Second", "Third"]);
    const [first, second, third] = content.map((item) => item.id);

    const putResponse = await page.request.put("/api/playlist", {
        data: { item_ids: [third, second, first] },
    });
    expect(putResponse.status()).toBe(200);

    const reordered = await (await page.request.get("/api/content")).json();
    expect(reordered.map((item) => item.name)).toEqual(["Third", "Second", "First"]);
});

test("inline preview renders the playlist on the Playlists subpage", async ({ page }) => {
    await page.goto("/");

    // Save a slide so the playlist has something to render.
    await page.locator(".editor .field-name").fill("Loop");
    await page.locator(".editor .field-text").fill("LOOP");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor-status")).toHaveText("Saved.");

    const content = await (await page.request.get("/api/content")).json();
    await page.request.put("/api/playlist", {
        data: { item_ids: [content[0].id] },
    });

    // The Playlists subpage hosts the inline preview (client-side
    // simulator). The transport controls render unconditionally; the
    // idle placeholder hides once a non-empty playlist arrives.
    await page.locator('.nav-link[data-section="playlists"]').click();
    await expect(page.locator(".inline-preview-play")).toBeVisible();
    await expect(page.locator(".inline-preview-scrub")).toBeVisible();
    await expect(page.locator(".inline-preview-time")).toBeVisible();
    await expect(page.locator(".inline-preview-idle")).toBeHidden({
        timeout: 5_000,
    });
});
