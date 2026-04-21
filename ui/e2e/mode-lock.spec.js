// Mode-lock e2e: upload an HDMI (h264_mp4) video, flip the device
// settings to hub75, confirm:
//   - the pallet tile picks up the mode-locked badge on reload
//   - the playback loop skips the incompatible video + plays the
//     compatible text slide instead
// Exercises the real backend skip logic wired through dependencies.py.

import { mkdirSync, statSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

const FIXTURE_DIR = "/tmp/om-video-verify";
const FIXTURE = path.join(FIXTURE_DIR, "input.mp4");

function settingsPayload(outputMode, width, height) {
    return {
        output_mode: outputMode,
        display_width: width,
        display_height: height,
        display_rotation: 0,
        wifi_ap_enabled: true,
        wifi_station_enabled: false,
        wifi_station_ssid: null,
        wifi_station_password: null,
        timezone: "UTC",
        tailscale_enabled: false,
        tailscale_auth_key: null,
        tailscale_hostname: null,
    };
}

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

test.beforeEach(async ({ page }) => {
    resetServerState();
    // Start with HDMI so the upload produces an h264_mp4 asset.
    await page.request.put("/api/settings", {
        data: settingsPayload("hdmi", 128, 96),
    });
});

test.afterEach(async ({ page }) => {
    await page.request.put("/api/settings", {
        data: settingsPayload("hdmi", 128, 96),
    });
});

test("HDMI-mode video becomes mode-locked after switching to hub75", async ({ page }) => {
    test.setTimeout(180_000);

    await page.goto("/");

    // 1) Upload a video in HDMI mode → produces an h264_mp4 VideoSlide.
    await page.locator('.nav-link[data-section="slides/video"]').click();
    await page.locator(".video-upload .field-file").setInputFiles(FIXTURE);
    await expect(page.locator(".video-upload-log-body")).toContainText(
        "transcoded to",
        { timeout: 90_000 },
    );
    await page.locator(".video-upload .field-save").click();
    await expect(page.locator(".video-upload-status")).toHaveText("Saved.");

    // 2) Also create a TextSlide so the playback loop has something
    //    compatible to fall through to.
    await page.locator('.nav-link[data-section="slides/text"]').click();
    await page.locator(".editor .field-name").fill("CompatText");
    await page.locator(".editor .field-text").fill("OK");
    await page.locator(".editor .field-duration").fill("1");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor-status")).toHaveText("Saved.");

    // 3) Flip the device to hub75 — and reload so the UI picks up the
    //    new output_mode (resolvePanelDims runs once on boot).
    await page.request.put("/api/settings", {
        data: settingsPayload("hub75", 64, 32),
    });
    await page.reload();

    // 4) The video's pallet tile should carry the mode-lock badge.
    await page.locator('.nav-link[data-section="playlists"]').click();
    const content = await (await page.request.get("/api/content")).json();
    const videoItem = content.find((c) => c.type === "video");
    const textItem = content.find((c) => c.type === "text_slide");
    expect(videoItem.pipeline).toBe("h264_mp4");

    const palletVideo = page.locator(
        `.pallet-tile[data-id="${videoItem.id}"]`,
    );
    await expect(palletVideo).toHaveClass(/pallet-tile--locked/);
    await expect(palletVideo.locator(".pallet-tile-lock")).toBeVisible();

    // 5) Put both items in the playlist with the video first; start
    //    playback; backend should skip the video and land on the text
    //    slide. /api/playback/state reflects the advance.
    await page.request.put("/api/playlist", {
        data: {
            items: [
                { item_id: videoItem.id, transition: "cut", transition_ms: 0 },
                { item_id: textItem.id, transition: "cut", transition_ms: 0 },
            ],
        },
    });

    await page.request.post("/api/playback/stop");
    await page.request.post("/api/playback/start");

    // Poll up to ~8s for the loop to settle on the text slide.
    const deadline = Date.now() + 8_000;
    let state = null;
    while (Date.now() < deadline) {
        state = await (await page.request.get("/api/playback/state")).json();
        if (state.current_item_id === textItem.id) break;
        await page.waitForTimeout(200);
    }
    expect(state.current_item_id).toBe(textItem.id);
    expect(state.current_item_type).toBe("text_slide");

    await page.request.post("/api/playback/stop");
});
