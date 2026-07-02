import { expect, test } from "@playwright/test";
import { clickNewSlide, resetServerState, saveTextSlide } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("save a text slide → it shows up in the Playlists pallet + the asset serves", async ({ page }) => {
    await page.goto("/#/slides/text");

    // Type a slide on the Text subpage and let autosave land.
    await expect(page.locator(".editor .field-name")).toHaveValue(/Text Slide \d+/);
    await page.locator(".editor .field-name").fill("Opening");
    await page.locator(".editor .field-text").fill("GRAND OPENING");
    await page.locator(".editor .field-text-color").evaluate((el) => {
        el.value = "#ffffff";
        el.dispatchEvent(new Event("input", { bubbles: true }));
    });
    // Slide bg color is now reached via pattern color_a (B14).
    await page.locator(".editor .field-bg-pat-color-a").evaluate((el) => {
        el.value = "#cc0000";
        el.dispatchEvent(new Event("input", { bubbles: true }));
    });

    await expect(page.locator(".editor-status"))
        .toHaveAttribute("data-state", "saved", { timeout: 5_000 });

    // Over on the Playlists subpage, the new slide is in the pallet.
    await page.locator('.nav-link[data-section="playlists"]').click();
    await expect(page.locator('.panel[data-section="playlists"]')).toBeVisible();

    const palletTiles = page.locator(".pallet-tile");
    await expect(palletTiles).toHaveCount(1);
    await expect(palletTiles.first().locator(".pallet-tile-name")).toHaveText(
        "Opening",
    );

    // And the tile's thumbnail URL returns a real JPEG. 2026-07-02
    // (handover-blocker fix): tile thumbnails now hit the new small-
    // JPEG /thumbnail endpoint instead of the raw 1-3 MB /asset PNG
    // — the raw endpoint was OOM-rebooting a Pi Zero 2 W dashboard.
    const thumb = palletTiles.first().locator(".pallet-tile-thumb");
    await expect(thumb).toBeVisible();
    const thumbResp = await page.request.get(await thumb.getAttribute("src"));
    expect(thumbResp.status()).toBe(200);
    expect(thumbResp.headers()["content-type"]).toBe("image/jpeg");
});

test("two text slides both land in the pallet", async ({ page }) => {
    await saveTextSlide(page, "Open", { text: "OPEN" });
    await clickNewSlide(page);
    await saveTextSlide(page, "Closed", { text: "CLOSED" });

    await page.locator('.nav-link[data-section="playlists"]').click();
    await expect(page.locator(".pallet-tile")).toHaveCount(2);
    const names = await page.locator(".pallet-tile-name").allTextContents();
    expect(new Set(names)).toEqual(new Set(["Open", "Closed"]));
});

test("rejected save (text too long) surfaces the error", async ({ page }) => {
    await page.goto("/#/slides/text");
    await expect(page.locator(".editor .field-name")).toHaveValue(/Text Slide \d+/);
    await page.locator(".editor .field-name").fill("Big");
    // Server caps text length; autosave PUT/POST will 422 and the auto-save
    // helper paints the error into the status pill as "Couldn't save · …".
    await page.locator(".editor .field-text").fill("X".repeat(10_001));
    await expect(page.locator(".editor-status"))
        .toContainText(/Couldn't save/, { timeout: 5_000 });
});

test("playlist PUT reorders content via the API (what drag-reorder invokes)", async ({
    page,
}) => {
    // The UI drag-reorder flow ends in a PUT /api/playlists/{id} with the
    // new id order. Driving Sortable's pointer-event internals from
    // Playwright is flaky, so we verify the contract the drag handler
    // depends on: PUT the new order via the exact same client path, and
    // GET reflects it.
    await saveTextSlide(page, "First");
    await clickNewSlide(page);
    await saveTextSlide(page, "Second");
    await clickNewSlide(page);
    await saveTextSlide(page, "Third");

    const content = await (await page.request.get("/api/content")).json();
    expect(content.map((item) => item.name)).toEqual(["First", "Second", "Third"]);
    const [first, second, third] = content.map((item) => item.id);

    const putResponse = await page.request.put(
        "/api/playlists/00000000-0000-4000-8000-000000000001",
        { data: { item_ids: [third, second, first] } },
    );
    expect(putResponse.status()).toBe(200);

    const reordered = await (await page.request.get("/api/content")).json();
    expect(reordered.map((item) => item.name)).toEqual(["Third", "Second", "First"]);
});

test("inline preview renders the playlist on the Playlists subpage", async ({ page }) => {
    await saveTextSlide(page, "Loop", { text: "LOOP" });

    const content = await (await page.request.get("/api/content")).json();
    await page.request.put(
        "/api/playlists/00000000-0000-4000-8000-000000000001",
        { data: { item_ids: [content[0].id] } },
    );

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
