// VLC-stream slide editor e2e (STREAM/VLC slice 8). Create / edit /
// delete a "VLC stream" playlist slide through the real editor against
// a running uvicorn — no mocking; the backend's /api/content/vlc-
// streams endpoints do a real round trip.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

async function gotoVlcEditor(page) {
    await page.goto("/#/slides/vlc");
    // Wait for the editor's async tail (computeDefaultName) to land
    // before filling — otherwise it races and overwrites the name.
    await expect(
        page.locator(".vlc-stream-upload .field-name"),
    ).toHaveValue(/VLC Stream \d+/);
}

async function vlcItems(page) {
    const items = await (await page.request.get("/api/content")).json();
    return items.filter((it) => it.type === "vlc_stream");
}

test("create: a VLC slide saves to /api/content and joins the playlist", async ({
    page,
}) => {
    await gotoVlcEditor(page);
    await page.fill(".vlc-stream-upload .field-name", "Lobby Cam");
    await page.fill(
        ".vlc-stream-upload .field-rtsp-url",
        "rtsp://laptop:8554/live",
    );

    // Autosave POSTs once the URL is non-empty — poll the real API.
    await expect
        .poll(async () => (await vlcItems(page))[0]?.rtsp_url ?? null, {
            timeout: 5_000,
        })
        .toBe("rtsp://laptop:8554/live");

    // Auto-appended to a playlist like every other slide type.
    const created = (await vlcItems(page))[0];
    const playlists = await (
        await page.request.get("/api/playlists")
    ).json();
    const inAPlaylist = (playlists.playlists || []).some((p) =>
        (p.item_ids || []).map(String).includes(String(created.id)),
    );
    expect(inAPlaylist).toBe(true);
});

test("edit: changing the RTSP URL PATCHes the same slide (no duplicate)", async ({
    page,
}) => {
    await gotoVlcEditor(page);
    await page.fill(".vlc-stream-upload .field-name", "EditMe");
    await page.fill(".vlc-stream-upload .field-rtsp-url", "rtsp://h/v1");
    await expect
        .poll(async () => (await vlcItems(page))[0]?.rtsp_url ?? null, {
            timeout: 5_000,
        })
        .toBe("rtsp://h/v1");

    // Edit the URL — autosave PUTs the existing id, not a new POST.
    await page.fill(".vlc-stream-upload .field-rtsp-url", "rtsp://h/v2");
    await expect
        .poll(async () => (await vlcItems(page))[0]?.rtsp_url ?? null, {
            timeout: 5_000,
        })
        .toBe("rtsp://h/v2");

    // Exactly one vlc_stream slide — the edit PATCHed, didn't duplicate.
    expect((await vlcItems(page)).length).toBe(1);
});

test("delete: the slide-browser tile delete button removes the VLC slide", async ({
    page,
}) => {
    await gotoVlcEditor(page);
    await page.fill(".vlc-stream-upload .field-name", "DeleteMe");
    await page.fill(".vlc-stream-upload .field-rtsp-url", "rtsp://h/x");

    const tile = page.locator(
        ".vlc-stream-upload .slide-browser-tile[data-id]",
        { hasText: "DeleteMe" },
    );
    await expect(tile).toHaveCount(1, { timeout: 5_000 });

    page.once("dialog", (d) => d.accept());
    await tile.locator(".slide-browser-tile-delete").click();
    await expect(tile).toHaveCount(0, { timeout: 5_000 });
    expect((await vlcItems(page)).length).toBe(0);
});
