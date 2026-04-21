// End-to-end smoke for schedule-driven playback. The named-playlist UI
// was collapsed to a single default playlist (the track editor), so we
// drive named-playlist creation + schedule rules through the API and
// verify the playback engine picks the right playlist at runtime. This
// preserves test coverage of the schedule → playback wiring without
// leaning on UI affordances the user doesn't see anymore.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("schedule-driven playback: API-built lunch playlist + always-on rule → playback shows 'lunch'", async ({
    page,
}) => {
    await page.goto("/");

    // 1. Save two slides via the Text editor (they auto-append to default).
    for (const name of ["First", "Second"]) {
        await page.locator(".editor .field-name").fill(name);
        await page.locator(".editor .field-text").fill(name);
        await page.locator(".editor .field-save").click();
        await expect(page.locator(".editor-status")).toHaveText("Saved.");
    }

    const content = await (await page.request.get("/api/content")).json();
    const ids = content.map((item) => String(item.id));
    expect(ids.length).toBe(2);

    // 2. Create a "lunch" named playlist via the API, containing both slides.
    const put = await page.request.put("/api/playlists/lunch", {
        data: { item_ids: ids },
    });
    expect(put.status()).toBe(200);

    // 3. Add a schedule rule that always matches via the Schedule UI. The
    //    playlist dropdown pulls from the API so "lunch" surfaces.
    await page.locator('.nav-link[data-section="schedule"]').click();
    await expect(page.locator('.panel[data-section="schedule"]')).toBeVisible();
    await page.locator(".schedule-add").click();
    const rule = page.locator(".schedule-rule").first();
    for (let i = 0; i < 7; i++) {
        await rule.locator(".rule-day-input").nth(i).check();
    }
    await rule.locator(".rule-start").fill("00:00");
    await rule.locator(".rule-end").fill("24:00");
    await rule.locator(".rule-playlist").selectOption("lunch");

    await page.locator(".schedule-save").click();
    await expect(page.locator(".schedule-status")).toHaveText("Saved.");

    // 4. Kick playback from the Playlists subpage where Play / Stop lives.
    await page.locator('.nav-link[data-section="playlists"]').click();
    const playBtn = page.locator(".playback-btn");
    await expect(playBtn).toHaveText("Play all");
    await playBtn.click();
    await expect(playBtn).toHaveText("Stop");

    // 5. The loop should evaluate the schedule, pick "lunch", and the
    //    state endpoint should report that back.
    await expect
        .poll(async () => {
            const state = await (await page.request.get("/api/playback/state")).json();
            return state.current_playlist_name;
        })
        .toBe("lunch");

    // 6. Stop so the test doesn't leave a task running.
    await playBtn.click();
    await expect(playBtn).toHaveText("Play all");
});
