// Auto-slide end-to-end: create a time slide via the editor, start
// playback, assert the live preview shows a ticking HH:MM:SS overlay
// (first value → wait ≥1s → second value differs). Exercises the full
// stack: editor save → auto_format wire shape → backend
// compose_motion_frame on the playback loop → /api/playback/state
// surfacing current_item_auto_* → live-preview overlay in DOM.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("auto-mode time slide ticks in the live preview overlay", async ({ page }) => {
    test.setTimeout(60_000);

    await page.goto("/#/slides/text");

    // 1) Create a "current time" slide with HH:MM:SS format so seconds
    //    visibly change each refresh. Autosave fires on each field change;
    //    we wait once at the end for the round-trip.
    await expect(page.locator(".editor .field-name")).toHaveValue(/Text Slide \d+/);
    await page.locator(".editor .field-name").fill("Clock");
    await page.locator(".editor .field-text").fill("--:--:--");
    // `.field-auto-mode` was a <select> originally; it was refactored into
    // a segmented-button control + a hidden state-of-record input. Click
    // the Time button (editor.js:806 wires the click handler that mirrors
    // the old select's change event — sets hidden input, populates the
    // format options, kicks autosave).
    await page
        .locator('.editor .field-auto-mode-segmented button[data-value="time"]')
        .click();
    await expect(page.locator(".field-auto-format-wrap")).toBeVisible();
    await page.locator(".editor .field-auto-format").selectOption("time_hms");
    // Keep slide short so the e2e runs fast; 3s is plenty for two ticks.
    await page.locator(".editor .field-duration").fill("3");
    await expect(page.locator(".editor .editor-status"))
        .toHaveAttribute("data-state", "saved", { timeout: 5_000 });

    // 2) Put the slide in the default playlist.
    const content = await (await page.request.get("/api/content")).json();
    const clockId = content[0].id;
    await page.request.put(
        "/api/playlists/00000000-0000-4000-8000-000000000001",
        {
            data: {
                items: [{ item_id: clockId, transition: "cut", transition_ms: 0 }],
            },
        },
    );

    // 3) Playlists panel: the inline preview is the client-side simulator.
    //    Click the inline play button to start its own playback engine.
    await page.locator('.nav-link[data-section="playlists"]').click();
    await expect(page.locator(".inline-preview")).toBeVisible();
    await page.locator(".inline-preview-play").click();

    // 4) Auto-text overlay appears with HH:MM:SS formatting.
    const overlay = page.locator(".inline-preview-auto-text");
    await expect(overlay).toBeVisible({ timeout: 10_000 });
    await expect(overlay).toHaveText(/^\d{2}:\d{2}:\d{2}$/);

    // 5) After ~1.5s the overlay text MUST have changed — the inline
    //    preview's rAF loop advances position + re-renders.
    const firstValue = await overlay.textContent();
    await page.waitForTimeout(1500);
    const secondValue = await overlay.textContent();
    expect(secondValue).not.toBe(firstValue);
    expect(secondValue).toMatch(/^\d{2}:\d{2}:\d{2}$/);

    // 6) Backend state reflects the auto metadata too (the hardware
    //    loop runs the same slide in parallel). Explicit start since
    //    e2e config disables lifespan autostart.
    await page.request.post("/api/playback/start");
    const state = await (await page.request.get("/api/playback/state")).json();
    expect(state.current_item_auto_mode).toBe("time");
    expect(state.current_item_auto_format).toBe("time_hms");
    await page.request.post("/api/playback/stop");
});
