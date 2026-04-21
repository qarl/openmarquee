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

    for (const name of ["Open", "Closed"]) {
        await page.locator(".editor .field-name").fill(name);
        await page.locator(".editor .field-text").fill(name.toUpperCase());
        await page.locator(".editor .field-save").click();
        await expect(page.locator(".editor-status")).toHaveText("Saved.");
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

    for (const name of ["First", "Second", "Third"]) {
        await page.locator(".editor .field-name").fill(name);
        await page.locator(".editor .field-text").fill(name);
        await page.locator(".editor .field-save").click();
        await expect(page.locator(".editor-status")).toHaveText("Saved.");
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

test("Play all on the Playlists subpage starts the backend loop", async ({ page }) => {
    await page.goto("/");

    // Save a slide so the loop has something to render.
    await page.locator(".editor .field-name").fill("Loop");
    await page.locator(".editor .field-text").fill("LOOP");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor-status")).toHaveText("Saved.");

    // Add it to the default playlist via the drag-handler contract endpoint.
    const content = await (await page.request.get("/api/content")).json();
    await page.request.put("/api/playlist", {
        data: { item_ids: [content[0].id] },
    });

    // Playback controls live on the Playlists subpage now.
    await page.locator('.nav-link[data-section="playlists"]').click();
    const playBtn = page.locator(".playback-btn");
    await expect(playBtn).toHaveText("Play all");
    await playBtn.click();
    await expect(playBtn).toHaveText("Stop");

    const state = await (await page.request.get("/api/playback/state")).json();
    expect(state.is_running).toBe(true);

    await playBtn.click();
    await expect(playBtn).toHaveText("Play all");
    const stoppedState = await (await page.request.get("/api/playback/state")).json();
    expect(stoppedState.is_running).toBe(false);
});
