// End-to-end smoke for schedule-driven playback. The named-playlist UI
// was collapsed to a single default playlist (the track editor), so we
// drive named-playlist creation + schedule rules through the API and
// verify the playback engine picks the right playlist at runtime. This
// preserves test coverage of the schedule → playback wiring without
// leaning on UI affordances the user doesn't see anymore.

import { expect, test } from "@playwright/test";
import { clickNewSlide, resetServerState, saveTextSlide } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("schedule-driven playback: API-built lunch playlist + always-on rule → playback shows lunch", async ({
    page,
}) => {
    // 1. Save two slides via the Text editor (autosave appends to default).
    await saveTextSlide(page, "First");
    await clickNewSlide(page);
    await saveTextSlide(page, "Second");

    const content = await (await page.request.get("/api/content")).json();
    const ids = content.map((item) => String(item.id));
    expect(ids.length).toBe(2);

    // 2. Create a "lunch" named playlist via the API. Playlists are
    //    id-keyed since the UUID refactor (commit 577acc9), so we POST to
    //    /api/playlists and capture the server-assigned id.
    const created = await page.request.post("/api/playlists", {
        data: { name: "lunch", item_ids: ids },
    });
    expect(created.status()).toBe(201);
    const lunchPlaylist = await created.json();
    const lunchId = lunchPlaylist.id;

    // 3. Add a schedule rule that always matches via the Schedule UI. The
    //    playlist dropdown's option labels are display names; we
    //    selectOption({label: "lunch"}) so the test reads as
    //    name-driven even though the wire shape uses ids.
    await page.locator('.nav-link[data-section="schedule"]').click();
    await expect(page.locator('.panel[data-section="schedule"]')).toBeVisible();
    await page.locator(".schedule-add").click();
    const rule = page.locator(".schedule-rule").first();
    for (let i = 0; i < 7; i++) {
        await rule.locator(".rule-day-input").nth(i).check();
    }
    await rule.locator(".rule-start").fill("00:00");
    await rule.locator(".rule-end").fill("24:00");
    await rule.locator(".rule-playlist").selectOption({ label: "lunch" });

    // Schedule editor auto-saves (no explicit Save button); wait for the
    // round-trip via the status pill's data-state (FYS bug 6 — no copy).
    await expect(page.locator(".schedule-status"))
        .toHaveAttribute("data-state", "saved", { timeout: 5_000 });

    // 4. Hardware playback is autonomous — kick the loop directly.
    //    (The UI no longer has a Play-all button; e2e config disables
    //    the lifespan autostart so each spec opts in.)
    await page.request.post("/api/playback/start");

    // 5. The loop evaluates the schedule, picks "lunch", and the state
    //    endpoint reports the playlist's id (not name — the wire is
    //    id-keyed since the UUID refactor; QA #08 nailed down the
    //    contract).
    await expect
        .poll(async () => {
            const state = await (await page.request.get("/api/playback/state")).json();
            return state.current_playlist_id;
        }, { timeout: 10_000 })
        .toBe(lunchId);

    await page.request.post("/api/playback/stop");
});
