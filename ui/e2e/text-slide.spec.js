import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("end-to-end: save → list → play → delete", async ({ page }) => {
    await page.goto("/");

    // Type a slide.
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

    // Save.
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor-status")).toHaveText("Saved.");

    // The new slide shows in the list.
    await expect(page.locator(".slide")).toHaveCount(1);
    await expect(page.locator(".slide-name").first()).toHaveText("Opening");
    await expect(page.locator(".slide-text").first()).toHaveText("GRAND OPENING");

    // The thumbnail loads.
    const thumb = page.locator(".slide-thumb").first();
    await expect(thumb).toBeVisible();
    const thumbResp = await page.request.get(await thumb.getAttribute("src"));
    expect(thumbResp.status()).toBe(200);
    expect(thumbResp.headers()["content-type"]).toBe("image/png");

    // Click Play; the preview frame should now exist on the backend.
    await page.locator(".slide button", { hasText: "Play" }).click();
    // Wait for the play request to land before checking the preview.
    await expect
        .poll(async () => {
            const preview = await page.request.get("/dev/preview/frame.png");
            return preview.status();
        })
        .toBe(200);
    const preview = await page.request.get("/dev/preview/frame.png");
    expect(preview.headers()["content-type"]).toBe("image/png");
    const previewBody = await preview.body();
    expect(previewBody.length).toBeGreaterThan(0);
    // PNG magic number — confirms the bytes are an actual image, not an
    // empty file or HTML 404 page.
    expect(previewBody.slice(0, 8)).toEqual(
        Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    );

    // Click Delete; the slide disappears.
    await page.locator(".slide button", { hasText: "Delete" }).click();
    await expect(page.locator(".slide")).toHaveCount(0);
    await expect(page.locator(".list-status")).toContainText("No slides yet");
});

test("two slides can coexist, both retrievable", async ({ page }) => {
    await page.goto("/");

    for (const name of ["Open", "Closed"]) {
        await page.locator(".editor .field-name").fill(name);
        await page.locator(".editor .field-text").fill(name.toUpperCase());
        await page.locator(".editor .field-save").click();
        await expect(page.locator(".editor-status")).toHaveText("Saved.");
    }

    await expect(page.locator(".slide")).toHaveCount(2);
    const names = await page.locator(".slide-name").allTextContents();
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
    // flaky, so we verify the contract the drag handler depends on: PUT the
    // new order via the exact same client path, and the list reflects it.
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

    // Reverse via PUT (same shape the drag handler uses via setPlaylistOrder).
    const putResponse = await page.request.put("/api/playlist", {
        data: { item_ids: [third, second, first] },
    });
    expect(putResponse.status()).toBe(200);

    // GET /api/content now reflects the new order (and so would the UI after
    // a refresh, and so would the playback loop).
    const reordered = await (await page.request.get("/api/content")).json();
    expect(reordered.map((item) => item.name)).toEqual(["Third", "Second", "First"]);
});

test("Play all starts the backend loop; Stop stops it", async ({ page }) => {
    await page.goto("/");

    // Save one slide so the loop has something to render.
    await page.locator(".editor .field-name").fill("Loop");
    await page.locator(".editor .field-text").fill("LOOP");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor-status")).toHaveText("Saved.");

    // Start playback.
    const playBtn = page.locator(".playback-btn");
    await expect(playBtn).toHaveText("Play all");
    await playBtn.click();
    await expect(playBtn).toHaveText("Stop");

    // Confirm the backend sees itself as running.
    const state = await (await page.request.get("/api/playback/state")).json();
    expect(state.is_running).toBe(true);

    // Stop playback.
    await playBtn.click();
    await expect(playBtn).toHaveText("Play all");
    const stoppedState = await (await page.request.get("/api/playback/state")).json();
    expect(stoppedState.is_running).toBe(false);
});
