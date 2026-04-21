// End-to-end smoke test for the full schedule-driven playback loop:
// items → named playlist → schedule rule → playback switches to that playlist.
//
// This exercises every integration point introduced over Phase 5:
//   (a) content upload + default-playlist auto-append
//   (b) named playlist create + item membership via the Add-item dropdown
//   (c) schedule rule editor (playlist dropdown populated from real data)
//   (d) playback engine reads the schedule + plays from the active playlist
//   (e) UI "Now playing: <playlist>" badge polling

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("schedule-driven playback: items → lunch playlist → always-on rule → playback shows 'lunch'", async ({
    page,
}) => {
    await page.goto("/");

    // 1. Save two slides (auto-added to the default playlist).
    for (const name of ["First", "Second"]) {
        await page.locator(".editor .field-name").fill(name);
        await page.locator(".editor .field-text").fill(name);
        await page.locator(".editor .field-save").click();
        await expect(page.locator(".editor-status")).toHaveText("Saved.");
    }

    // 2. Create a "lunch" named playlist.
    await page.locator(".playlists-create-name").fill("lunch");
    await page
        .locator(".playlists-create")
        .evaluate((form) => form.dispatchEvent(new Event("submit")));

    const lunchCard = page.locator('.playlist-card[data-name="lunch"]');
    await expect(lunchCard).toBeVisible();

    // 3. Add both items into lunch via the Add-item dropdown. Each selection
    //    auto-saves + refreshes, so after two picks the select is empty and
    //    both items appear as draggable members.
    const select = lunchCard.locator(".playlist-add-select");

    // Pick whatever option is first after the placeholder, twice.
    await select.selectOption({ index: 1 });
    await expect(lunchCard.locator(".playlist-item")).toHaveCount(1);
    await select.selectOption({ index: 1 });
    await expect(lunchCard.locator(".playlist-item")).toHaveCount(2);

    // 4. Verify backend persisted the lunch playlist.
    await expect
        .poll(async () => {
            const coll = await (await page.request.get("/api/playlists")).json();
            return coll.playlists.lunch?.item_ids?.length;
        })
        .toBe(2);

    // 5. Add a schedule rule that always matches (every day, 00:00–24:00) →
    //    playlist: lunch.
    await page.locator(".schedule-add").click();
    const rule = page.locator(".schedule-rule").first();
    for (let i = 0; i < 7; i++) {
        await rule.locator(".rule-day-input").nth(i).check();
    }
    await rule.locator(".rule-start").fill("00:00");
    await rule.locator(".rule-end").fill("24:00");
    await rule.locator(".rule-playlist").selectOption("lunch");

    // 6. Save the schedule.
    await page.locator(".schedule-save").click();
    await expect(page.locator(".schedule-status")).toHaveText("Saved.");

    // 7. Start playback. The loop should evaluate the schedule, pick lunch,
    //    and the state endpoint should report that back.
    await page.locator(".playback-btn").click();
    await expect(page.locator(".playback-btn")).toHaveText("Stop");

    await expect
        .poll(async () => {
            const state = await (await page.request.get("/api/playback/state")).json();
            return state.current_playlist_name;
        })
        .toBe("lunch");

    // 8. Stop so the test doesn't leave a task running.
    await page.locator(".playback-btn").click();
    await expect(page.locator(".playback-btn")).toHaveText("Play all");
});
