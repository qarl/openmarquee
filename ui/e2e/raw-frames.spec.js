// Raw-frames pipeline e2e: switch the device's output_mode to hub75
// so the video uploader picks extractRawFrames, upload a real ffmpeg-
// generated test clip, assert the .rgb asset lands + the live preview
// renders the thumbnail (not a broken <video>).

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

test.beforeEach(async ({ page }) => {
    resetServerState();
    // Reset output_mode to hdmi after test — settings persist to disk and
    // would leak into other specs otherwise. Per-test the beforeEach
    // starts from reset state already.
    await page.request.put("/api/settings", {
        data: {
            output_mode: "hub75",
            display_width: 64,
            display_height: 32,
            display_rotation: 0,
            wifi_ap_enabled: true,
            wifi_station_enabled: false,
            wifi_station_ssid: null,
            wifi_station_password: null,
            timezone: "UTC",
            tailscale_enabled: false,
            tailscale_auth_key: null,
            tailscale_hostname: null,
        },
    });
});

test.afterEach(async ({ page }) => {
    // Leave the backend in hdmi mode for subsequent specs.
    await page.request.put("/api/settings", {
        data: {
            output_mode: "hdmi",
            display_width: 128,
            display_height: 96,
            display_rotation: 0,
            wifi_ap_enabled: true,
            wifi_station_enabled: false,
            wifi_station_ssid: null,
            wifi_station_password: null,
            timezone: "UTC",
            tailscale_enabled: false,
            tailscale_auth_key: null,
            tailscale_hostname: null,
        },
    });
});

test("panel-mode video upload produces a raw-frames asset and the preview uses the thumbnail", async ({ page }) => {
    test.setTimeout(180_000);

    await page.goto("/");

    // Confirm the uploader picked up hub75 output_mode and shows the
    // panel-mode banner.
    await page.locator('.nav-link[data-section="slides/video"]').click();
    await expect(page.locator(".video-upload-panel-hint")).toBeVisible();

    await page.locator(".video-upload .field-file").setInputFiles(FIXTURE);
    // Wait for ffmpeg.wasm to finish the raw-frames extraction.
    await expect(page.locator(".video-upload-log-body")).toContainText(
        "RGB frames",
        { timeout: 120_000 },
    );
    await page.locator(".video-upload .field-save").click();
    await expect(page.locator(".video-upload-status")).toHaveText("Saved.");

    // Backend sanity: the VideoSlide should be pipeline='raw_frames' and
    // have the frames_* metadata populated.
    const content = await (await page.request.get("/api/content")).json();
    const video = content.find((c) => c.type === "video");
    expect(video).toBeTruthy();
    expect(video.pipeline).toBe("raw_frames");
    expect(video.frames_fps).toBe(15);
    expect(video.frames_width).toBe(64);
    expect(video.frames_height).toBe(32);

    // /frames serves the raw RGB bytes.
    const frames = await page.request.get(`/api/content/${video.id}/frames`);
    expect(frames.status()).toBe(200);
    const buf = await frames.body();
    const frameSize = video.frames_width * video.frames_height * 3;
    expect(buf.length % frameSize).toBe(0);
    expect(buf.length / frameSize).toBeGreaterThan(0);

    // Put the video in the default playlist and play — the preview
    // should show an <img> (thumbnail), not a broken <video>.
    await page.request.put("/api/playlist", {
        data: {
            items: [{ item_id: video.id, transition: "cut", transition_ms: 0 }],
        },
    });

    await page.request.post("/api/playback/stop");
    await page.locator('.nav-link[data-section="playlists"]').click();
    await page.locator(".playback-btn").click();

    await expect(page.locator(".live-preview img")).toBeVisible({ timeout: 20_000 });
    expect(await page.locator(".live-preview video").count()).toBe(0);
    const imgSrc = await page.locator(".live-preview img").getAttribute("src");
    expect(imgSrc).toContain(`/api/content/${video.id}/asset`);

    await page.locator(".playback-btn").click();
});
