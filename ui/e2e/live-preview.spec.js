// Live preview e2e: save a text slide + (small) video, put them both
// into the default playlist, start playback, assert the preview
// widget swaps between <img> and <video> as the loop advances.
//
// This exercises the full stack: ffmpeg.wasm transcode → storage →
// playback loop's current_item_type → live-preview element selection.

import { mkdirSync, statSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

const FIXTURE_DIR = "/tmp/om-video-verify";
const FIXTURE = path.join(FIXTURE_DIR, "input.mp4");

test.beforeAll(() => {
    mkdirSync(FIXTURE_DIR, { recursive: true });
    let needsGen = true;
    try {
        needsGen = statSync(FIXTURE).size < 1024;
    } catch {
        needsGen = true;
    }
    if (needsGen) {
        const r = spawnSync(
            "ffmpeg",
            [
                "-y",
                "-f", "lavfi",
                "-i", "testsrc=duration=2:size=320x240:rate=10",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-t", "2",
                FIXTURE,
            ],
            { stdio: "inherit" },
        );
        if (r.status !== 0) {
            throw new Error("failed to generate fixture video");
        }
    }
});

test.beforeEach(() => {
    resetServerState();
});

test("live preview swaps to <video> when a video slide is currently playing", async ({ page }) => {
    test.setTimeout(180_000);

    await page.goto("/");

    // 1) Save a short-duration text slide so the loop has a non-video item.
    await page.locator(".editor .field-name").fill("HelloText");
    await page.locator(".editor .field-text").fill("HI");
    await page.locator(".editor .field-duration").fill("1");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor-status")).toHaveText("Saved.");

    // 2) Upload a video via the real ffmpeg.wasm-driven uploader.
    await page.locator('.nav-link[data-section="slides/video"]').click();
    await page.locator(".video-upload .field-file").setInputFiles(FIXTURE);
    await expect(page.locator(".video-upload-log-body")).toContainText(
        "transcoded to",
        { timeout: 90_000 },
    );
    await page.locator(".video-upload .field-duration").fill("2");
    await page.locator(".video-upload .field-save").click();
    await expect(page.locator(".video-upload-status")).toHaveText("Saved.");

    // 3) Put both items into the default playlist, video first so we can
    //    assert the <video> element without racing duration_ms.
    const content = await (await page.request.get("/api/content")).json();
    const byType = Object.fromEntries(content.map((c) => [c.type, c.id]));
    await page.request.put("/api/playlist", {
        data: {
            items: [
                { item_id: byType.video, transition: "cut", transition_ms: 0 },
                { item_id: byType.text_slide, transition: "cut", transition_ms: 0 },
            ],
        },
    });

    // 4) Make sure playback is stopped before starting — the backend is
    //    shared across tests so a leaked running loop would break the
    //    idle-state assertion.
    await page.request.post("/api/playback/stop");

    // 5) Navigate to Playlists, confirm the preview widget mounted.
    await page.locator('.nav-link[data-section="playlists"]').click();
    await expect(page.locator(".live-preview")).toBeVisible();

    // 6) Press Play. The preview polls /api/playback/state every 500ms; the
    //    loop stamps current_item_type on each iteration; the UI then
    //    renders <video> when the video slide is current.
    await page.locator(".playback-btn").click();

    // Poll the preview for up to 20s for a <video> to appear. (The video
    // is item 0, so it should land on the first iteration.)
    await expect(page.locator(".live-preview video")).toBeVisible({
        timeout: 20_000,
    });
    const videoSrc = await page.locator(".live-preview video").getAttribute("src");
    expect(videoSrc).toContain(`/api/content/${byType.video}/video`);

    await page.locator(".playback-btn").click();
    await expect(page.locator(".playback-btn")).toHaveText("Play all");
});
