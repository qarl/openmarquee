// Auto-slide end-to-end: create a time slide via the editor, start
// playback, assert the live preview shows a ticking HH:MM:SS overlay
// (first value → wait ≥1s → second value differs). Exercises the full
// stack: editor save → auto_format wire shape → backend compose_auto_frame
// on the playback loop → /api/playback/state surfacing current_item_auto_*
// → live-preview overlay in DOM.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("auto-mode time slide ticks in the live preview overlay", async ({ page }) => {
    test.setTimeout(60_000);

    await page.goto("/");

    // 1) Create a "current time" slide with HH:MM:SS format so seconds
    //    visibly change each refresh.
    await page.locator(".editor .field-name").fill("Clock");
    await page.locator(".editor .field-text").fill("--:--:--");
    const autoMode = page.locator(".editor .field-auto-mode");
    await autoMode.selectOption("time");
    await expect(page.locator(".field-auto-format-wrap")).toBeVisible();
    await page.locator(".editor .field-auto-format").selectOption("time_hms");
    // Keep slide short so the e2e runs fast; 3s is plenty for two ticks.
    await page.locator(".editor .field-duration").fill("3");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor-status")).toHaveText("Saved.");

    // 2) Put the slide in the default playlist and start playback.
    const content = await (await page.request.get("/api/content")).json();
    const clockId = content[0].id;
    await page.request.put("/api/playlist", {
        data: {
            items: [{ item_id: clockId, transition: "cut", transition_ms: 0 }],
        },
    });
    await page.request.post("/api/playback/stop");

    // 3) Open the Playlists panel (where the live preview mounts).
    await page.locator('.nav-link[data-section="playlists"]').click();
    await expect(page.locator(".live-preview")).toBeVisible();
    await page.locator(".playback-btn").click();

    // 4) Auto-text overlay appears with HH:MM:SS formatting.
    const overlay = page.locator(".live-preview-auto-text");
    await expect(overlay).toBeVisible({ timeout: 10_000 });
    await expect(overlay).toHaveText(/^\d{2}:\d{2}:\d{2}$/);

    // 5) After ~1.5s the overlay text MUST have changed — the preview's
    //    500ms poll picks up the new seconds value. (Timezone mismatch
    //    between browser and backend is fine here; both tick.)
    const firstValue = await overlay.textContent();
    await page.waitForTimeout(1500);
    const secondValue = await overlay.textContent();
    expect(secondValue).not.toBe(firstValue);
    // Sanity: still HH:MM:SS.
    expect(secondValue).toMatch(/^\d{2}:\d{2}:\d{2}$/);

    // 6) State endpoint reflects the auto metadata.
    const state = await (await page.request.get("/api/playback/state")).json();
    expect(state.current_item_auto_mode).toBe("time");
    expect(state.current_item_auto_format).toBe("time_hms");

    await page.request.post("/api/playback/stop");
});
