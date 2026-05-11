// @vitest-environment jsdom
//
// 20.3: needs jsdom for window.location + localStorage (apiFetch reads
// both). The rest of the existing tests in this file are environment-
// neutral; jsdom is a superset.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    apiFetch,
    AUTH_TOKEN_KEY,
    deleteContent,
    effectiveDisplayDims,
    extractDetailMessage,
    generateBackground,
    getPlaybackState,
    getSchedule,
    getSettings,
    listContent,
    saveImage,
    saveSchedule,
    saveSettings,
    saveTextSlide,
    saveVideo,
    startPlayback,
    startStream,
    stopPlayback,
    takeoverStream,
} from "./api.js";

// 20.3: apiFetch reads localStorage on every call. The tests below pre-
// clear it so each test starts in the "no token" state; tests that need
// a token call localStorage.setItem(AUTH_TOKEN_KEY, ...) explicitly.
beforeEach(() => {
    try { localStorage.removeItem(AUTH_TOKEN_KEY); } catch { /* no localStorage in setup */ }
});

afterEach(() => {
    vi.unstubAllGlobals();
});

function mockFetch(response) {
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
}

// 20.3: helper for tests that don't care about the Authorization header
// arg; just unwrap the URL of the first fetch call.
function calledUrl(fetchMock) {
    return fetchMock.mock.calls[0][0];
}

// 20.3: helper for tests that need to assert method/body without
// caring about the Authorization header injected by apiFetch.
function calledInit(fetchMock) {
    return fetchMock.mock.calls[0][1] || {};
}

describe("saveTextSlide", () => {
    it("POSTs JSON to the text-slides endpoint and returns the new item", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ id: "abc", type: "text_slide", name: "Hi" }),
        });

        const result = await saveTextSlide({
            name: "Hi",
            text: "Hi",
            text_color: "#FFFFFF",
            background_color: "#000000",
            png_base64: "FAKE",
        });

        expect(result).toEqual({ id: "abc", type: "text_slide", name: "Hi" });

        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/content/text-slides");
        expect(init.method).toBe("POST");
        // 20.3: apiFetch wraps init.headers in a Headers instance to
        // attach Authorization, so the strict-equal {Content-Type:...}
        // check used to pass against a plain object but doesn't now.
        // Assert Content-Type via the Headers API instead.
        expect(init.headers.get("Content-Type")).toBe("application/json");
        expect(JSON.parse(init.body).name).toBe("Hi");
    });

    it("throws on non-ok response", async () => {
        mockFetch({ ok: false, status: 422, text: async () => "bad color" });
        await expect(saveTextSlide({})).rejects.toThrow("422");
    });
});

describe("listContent", () => {
    it("GETs /api/content and returns the array", async () => {
        const items = [{ id: "a", name: "x" }];
        const fetchMock = mockFetch({ ok: true, json: async () => items });
        const result = await listContent();
        expect(result).toEqual(items);
        // 20.3: apiFetch always passes a 2nd arg (the wrapped init); use
        // calledUrl() to ignore it.
        expect(calledUrl(fetchMock)).toBe("/api/content");
    });

    it("throws on non-ok response", async () => {
        mockFetch({ ok: false, status: 500 });
        await expect(listContent()).rejects.toThrow("500");
    });
});

describe("deleteContent", () => {
    it("DELETEs /api/content/{id}", async () => {
        const fetchMock = mockFetch({ ok: true });
        await deleteContent("abc");
        expect(calledUrl(fetchMock)).toBe("/api/content/abc");
        expect(calledInit(fetchMock).method).toBe("DELETE");
    });

    it("throws on non-ok response", async () => {
        mockFetch({ ok: false, status: 404 });
        await expect(deleteContent("missing")).rejects.toThrow("404");
    });
});

describe("saveImage", () => {
    it("POSTs JSON to /api/content/images and returns the new item", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ id: "x", type: "image", name: "Logo" }),
        });
        const result = await saveImage({ name: "Logo", image_base64: "FAKE" });
        expect(result).toEqual({ id: "x", type: "image", name: "Logo" });
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/content/images");
        expect(init.method).toBe("POST");
    });

    it("throws on non-ok response", async () => {
        mockFetch({ ok: false, status: 422, text: async () => "bad" });
        await expect(saveImage({})).rejects.toThrow("422");
    });
});

describe("generateBackground", () => {
    it("POSTs the prompt to /api/backgrounds/generate and returns the ImageSlide", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ id: "bg1", type: "image", name: "sunset — Background" }),
        });
        const result = await generateBackground({ prompt: "sunset gradient" });
        expect(result.id).toBe("bg1");
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/backgrounds/generate");
        expect(init.method).toBe("POST");
        expect(JSON.parse(init.body).prompt).toBe("sunset gradient");
    });

    it("attaches the HTTP status so callers can branch on 503 vs other errors", async () => {
        mockFetch({
            ok: false,
            status: 503,
            json: async () => ({ detail: "OPENAI_API_KEY is not set" }),
        });
        try {
            await generateBackground({ prompt: "x" });
            throw new Error("should have thrown");
        } catch (err) {
            expect(err.status).toBe(503);
            expect(err.message).toMatch(/OPENAI_API_KEY/);
        }
    });

    it("falls back to text() when the non-ok body isn't JSON", async () => {
        mockFetch({
            ok: false,
            status: 500,
            json: async () => {
                throw new Error("not json");
            },
            text: async () => "internal server error",
        });
        await expect(generateBackground({ prompt: "x" })).rejects.toThrow(/500/);
    });
});

describe("saveVideo", () => {
    it("POSTs JSON to /api/content/videos and returns the new item", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ id: "v", type: "video", name: "Promo" }),
        });
        const result = await saveVideo({
            name: "Promo",
            pipeline: "h264_mp4",
            png_base64: "THUMB",
            mp4_base64: "MP4",
        });
        expect(result.type).toBe("video");
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/content/videos");
        expect(init.method).toBe("POST");
        expect(JSON.parse(init.body).pipeline).toBe("h264_mp4");
    });

    it("throws on non-ok response with status detail", async () => {
        mockFetch({ ok: false, status: 400, text: async () => "bad mp4" });
        await expect(saveVideo({})).rejects.toThrow(/400/);
    });
});

describe("playlists API (id-based)", () => {
    it("listPlaylists GETs /api/playlists", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ schema_version: 4, playlists: [] }),
        });
        const result = await (await import("./api.js")).listPlaylists();
        expect(result.schema_version).toBe(4);
        expect(calledUrl(fetchMock)).toBe("/api/playlists");
    });

    it("createPlaylist POSTs name + items to /api/playlists", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ id: "new-uuid", name: "lunch", items: [] }),
        });
        const { createPlaylist } = await import("./api.js");
        const result = await createPlaylist({ name: "lunch", entries: [] });
        expect(result.id).toBe("new-uuid");
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/playlists");
        expect(init.method).toBe("POST");
        expect(JSON.parse(init.body)).toEqual({
            name: "lunch",
            item_ids: [],
        });
    });

    it("savePlaylistById PUTs to /api/playlists/{id} with name + items body", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ id: "the-uuid", name: "lunch", item_ids: ["a", "b"] }),
        });
        const { savePlaylistById } = await import("./api.js");
        const result = await savePlaylistById("the-uuid", {
            name: "lunch",
            entries: ["a", "b"],
        });
        expect(result.item_ids).toEqual(["a", "b"]);
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/playlists/the-uuid");
        expect(init.method).toBe("PUT");
        expect(JSON.parse(init.body)).toEqual({
            name: "lunch",
            item_ids: ["a", "b"],
        });
    });

    it("deletePlaylistById DELETEs /api/playlists/{id}", async () => {
        const fetchMock = mockFetch({ ok: true });
        const { deletePlaylistById } = await import("./api.js");
        await deletePlaylistById("the-uuid");
        expect(calledUrl(fetchMock)).toBe("/api/playlists/the-uuid");
        expect(calledInit(fetchMock).method).toBe("DELETE");
    });

    it("savePlaylistById routes v3 entry objects to the `items` key, not `item_ids`", async () => {
        const fetchMock = mockFetch({ ok: true, json: async () => ({}) });
        const { savePlaylistById } = await import("./api.js");
        const entries = [
            { item_id: "a-uuid", transition: "fade", transition_ms: 500 },
            { item_id: "b-uuid", transition: "cut", transition_ms: 500 },
        ];
        await savePlaylistById("the-uuid", { name: "x", entries });
        const [, init] = fetchMock.mock.calls[0];
        const body = JSON.parse(init.body);
        expect(body).toHaveProperty("items");
        expect(body.items).toEqual(entries);
        expect(body).not.toHaveProperty("item_ids");
    });

    it("savePlaylistById keeps legacy string-array callers on the `item_ids` key", async () => {
        const fetchMock = mockFetch({ ok: true, json: async () => ({}) });
        const { savePlaylistById } = await import("./api.js");
        await savePlaylistById("the-uuid", { name: "x", entries: ["a", "b"] });
        const [, init] = fetchMock.mock.calls[0];
        const body = JSON.parse(init.body);
        expect(body).toEqual({ name: "x", item_ids: ["a", "b"] });
    });

    it("savePlaylistById omits the name when caller passes undefined", async () => {
        // PUT with name=undefined → server preserves existing name.
        const fetchMock = mockFetch({ ok: true, json: async () => ({}) });
        const { savePlaylistById } = await import("./api.js");
        await savePlaylistById("the-uuid", { entries: ["a"] });
        const [, init] = fetchMock.mock.calls[0];
        const body = JSON.parse(init.body);
        expect(body).not.toHaveProperty("name");
        expect(body).toEqual({ item_ids: ["a"] });
    });
});

describe("schedule API", () => {
    it("getSchedule GETs /api/schedules", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ rules: [], default_playlist_name: "default" }),
        });
        const result = await getSchedule();
        expect(result.default_playlist_name).toBe("default");
        expect(calledUrl(fetchMock)).toBe("/api/schedules");
    });

    it("saveSchedule PUTs to /api/schedules", async () => {
        const schedule = { rules: [], default_playlist_name: "x" };
        const fetchMock = mockFetch({ ok: true, json: async () => schedule });
        const result = await saveSchedule(schedule);
        expect(result).toEqual(schedule);
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/schedules");
        expect(init.method).toBe("PUT");
        expect(JSON.parse(init.body)).toEqual(schedule);
    });

    it("saveSchedule throws on non-ok", async () => {
        mockFetch({ ok: false, status: 422, text: async () => "bad" });
        await expect(saveSchedule({})).rejects.toThrow("422");
    });
});

describe("settings API", () => {
    it("getSettings GETs /api/settings", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ output_mode: "hdmi", brightness: 80 }),
        });
        const result = await getSettings();
        expect(result.output_mode).toBe("hdmi");
        expect(calledUrl(fetchMock)).toBe("/api/settings");
    });

    it("getSettings throws on non-ok", async () => {
        mockFetch({ ok: false, status: 500 });
        await expect(getSettings()).rejects.toThrow("500");
    });

    it("saveSettings PUTs the payload to /api/settings", async () => {
        const payload = { output_mode: "hub75", brightness: 50 };
        const fetchMock = mockFetch({ ok: true, json: async () => payload });
        const result = await saveSettings(payload);
        expect(result).toEqual(payload);
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/settings");
        expect(init.method).toBe("PUT");
        expect(JSON.parse(init.body)).toEqual(payload);
    });

    it("saveSettings surfaces backend detail on non-ok", async () => {
        mockFetch({ ok: false, status: 422, text: async () => "bad mode" });
        await expect(saveSettings({})).rejects.toThrow(/422/);
    });
});

describe("playback control API", () => {
    it("getPlaybackState GETs /api/playback/state", async () => {
        const state = { is_running: true, current_item_id: "abc" };
        const fetchMock = mockFetch({ ok: true, json: async () => state });
        const result = await getPlaybackState();
        expect(result).toEqual(state);
        expect(calledUrl(fetchMock)).toBe("/api/playback/state");
    });

    it("getPlaybackState response uses is_running + current_playlist_id (regression: QA 2026-04-26 #08)", async () => {
        // Pins the contract pulled from backend api_playback.py::PlaybackState
        // so a future dev who reads the JSDoc here can rely on those keys.
        // QA flagged the UI's old `state.running` / `state.current_playlist_name`
        // usages as undefined-by-design — those names never existed on the
        // wire, only in stale JSDoc. Don't reintroduce them by accident.
        const state = {
            is_running: true,
            current_item_id: "abc-123",
            current_item_type: "text_slide",
            current_item_transition: "fade",
            current_item_transition_ms: 600,
            current_item_auto_mode: null,
            current_item_auto_format: null,
            current_playlist_id: "pl-456",
        };
        mockFetch({ ok: true, json: async () => state });
        const result = await getPlaybackState();
        expect(result.is_running).toBe(true);
        expect(result.current_playlist_id).toBe("pl-456");
        // Anti-aliases — these never existed; pin so we don't quietly add them.
        expect(result.running).toBeUndefined();
        expect(result.current_playlist_name).toBeUndefined();
    });

    it("startPlayback POSTs /api/playback/start", async () => {
        const fetchMock = mockFetch({ ok: true });
        await startPlayback();
        expect(calledUrl(fetchMock)).toBe("/api/playback/start");
        expect(calledInit(fetchMock).method).toBe("POST");
    });

    it("stopPlayback POSTs /api/playback/stop", async () => {
        const fetchMock = mockFetch({ ok: true });
        await stopPlayback();
        expect(calledUrl(fetchMock)).toBe("/api/playback/stop");
        expect(calledInit(fetchMock).method).toBe("POST");
    });

    it("throws on non-ok responses", async () => {
        mockFetch({ ok: false, status: 500 });
        await expect(startPlayback()).rejects.toThrow("500");
    });
});

describe("stream API", () => {
    it("startStream attaches an AbortSignal so a wedged backend can't strand the panel", async () => {
        // Regression for the QA finding from Phase 12.2 review: without
        // a timeout, a hung /api/stream/start fetch leaves the panel in
        // 'Connecting…' indefinitely. AbortSignal.timeout(15s) gives
        // Tailscale + aiortc plenty of headroom but caps the worst case.
        const fetchMock = mockFetch({
            ok: true,
            status: 200,
            json: async () => ({
                session_id: "11111111-1111-1111-1111-111111111111",
                sdp_answer: "v=0\r\nfake-answer\r\n",
            }),
        });
        await startStream("v=0\r\noffer\r\n");
        const init = fetchMock.mock.calls[0][1];
        expect(init.signal).toBeInstanceOf(AbortSignal);
    });

    it("takeoverStream also attaches an AbortSignal", async () => {
        const fetchMock = mockFetch({
            ok: true,
            status: 200,
            json: async () => ({
                session_id: "22222222-2222-2222-2222-222222222222",
                sdp_answer: "v=0\r\nfake-answer\r\n",
            }),
        });
        await takeoverStream("v=0\r\noffer\r\n");
        const init = fetchMock.mock.calls[0][1];
        expect(init.signal).toBeInstanceOf(AbortSignal);
    });

    it("startStream surfaces a 409 with active_session_id pulled from the body", async () => {
        // The phone uses err.activeSessionId to switch from "Go Live"
        // to "Take Over" without a separate /status round trip.
        mockFetch({
            ok: false,
            status: 409,
            json: async () => ({
                detail: {
                    error: "stream_already_active",
                    active_session_id: "33333333-3333-3333-3333-333333333333",
                },
            }),
        });
        const err = await startStream("v=0\r\noffer\r\n").catch((e) => e);
        expect(err.code).toBe("stream_already_active");
        expect(err.activeSessionId).toBe(
            "33333333-3333-3333-3333-333333333333",
        );
        expect(err.status).toBe(409);
    });
});

describe("effectiveDisplayDims", () => {
    it("passes width/height through at rotation 0", () => {
        expect(
            effectiveDisplayDims({
                display_width: 1920,
                display_height: 1080,
                display_rotation: 0,
            }),
        ).toEqual({ width: 1920, height: 1080 });
    });

    it("passes width/height through at rotation 180", () => {
        expect(
            effectiveDisplayDims({
                display_width: 1920,
                display_height: 1080,
                display_rotation: 180,
            }),
        ).toEqual({ width: 1920, height: 1080 });
    });

    it("swaps dims at rotation 90 (portrait-mounted sign)", () => {
        expect(
            effectiveDisplayDims({
                display_width: 1920,
                display_height: 1080,
                display_rotation: 90,
            }),
        ).toEqual({ width: 1080, height: 1920 });
    });

    it("swaps dims at rotation 270", () => {
        expect(
            effectiveDisplayDims({
                display_width: 64,
                display_height: 32,
                display_rotation: 270,
            }),
        ).toEqual({ width: 32, height: 64 });
    });

    it("treats missing display_rotation as 0 (no swap)", () => {
        expect(
            effectiveDisplayDims({
                display_width: 64,
                display_height: 32,
            }),
        ).toEqual({ width: 64, height: 32 });
    });

    it("returns null for invalid / missing dims so callers fall back", () => {
        expect(effectiveDisplayDims(null)).toBe(null);
        expect(effectiveDisplayDims({})).toBe(null);
        expect(
            effectiveDisplayDims({ display_width: 0, display_height: 32 }),
        ).toBe(null);
        expect(
            effectiveDisplayDims({ display_width: 64, display_height: -1 }),
        ).toBe(null);
        expect(
            effectiveDisplayDims({
                display_width: "huh",
                display_height: 32,
            }),
        ).toBe(null);
    });
});

describe("extractDetailMessage (15.3)", () => {
    it("returns body.detail when it's a string (HTTPException(detail=...))", async () => {
        const response = {
            status: 409,
            statusText: "Conflict",
            json: async () => ({ detail: "peer already exists" }),
        };
        expect(await extractDetailMessage(response)).toBe("peer already exists");
    });

    it("returns the first msg from a 422 ValidationError array", async () => {
        const response = {
            status: 422,
            statusText: "Unprocessable Entity",
            json: async () => ({
                detail: [
                    { msg: "string too long", loc: ["body", "name"] },
                    { msg: "additional later error", loc: ["body", "color"] },
                ],
            }),
        };
        expect(await extractDetailMessage(response)).toBe("string too long");
    });

    it("falls back to '<status> <statusText>' when body isn't JSON", async () => {
        const response = {
            status: 500,
            statusText: "Internal Server Error",
            json: async () => {
                throw new Error("not json");
            },
        };
        expect(await extractDetailMessage(response)).toBe(
            "500 Internal Server Error",
        );
    });

    it("falls back when body is JSON but lacks a detail field", async () => {
        const response = {
            status: 503,
            statusText: "Service Unavailable",
            json: async () => ({ error: "down" }),
        };
        expect(await extractDetailMessage(response)).toBe("503 Service Unavailable");
    });
});

// --- 20.3 / phase A.3: apiFetch wrapper ---
//
// These tests pin the bearer-injection + 401-redirect contract. The
// rest of api.test.js exercises the wrapper indirectly (every existing
// suite goes through apiFetch now), but the contract pieces themselves
// -- header set, token cleared, redirect fired, throw raised -- deserve
// explicit assertions.

describe("apiFetch (20.3 wrapper)", () => {
    // jsdom's window.location.replace is a noop+throws in some setups.
    // Each test stubs it independently with a spy so we can both
    // observe the redirect intention AND avoid the "Not implemented:
    // navigation" jsdom warning.
    function stubLocation(pathname = "/") {
        const replace = vi.fn();
        // Window.location can't be reassigned wholesale in jsdom; mock
        // only the methods we need.
        const original = {
            replace: window.location.replace,
            pathname: window.location.pathname,
        };
        Object.defineProperty(window.location, "replace", {
            configurable: true,
            writable: true,
            value: replace,
        });
        Object.defineProperty(window.location, "pathname", {
            configurable: true,
            writable: true,
            value: pathname,
        });
        return { replace, restore: () => {
            Object.defineProperty(window.location, "replace", {
                configurable: true,
                writable: true,
                value: original.replace,
            });
            Object.defineProperty(window.location, "pathname", {
                configurable: true,
                writable: true,
                value: original.pathname,
            });
        }};
    }

    it("attaches Authorization: Bearer <token> when localStorage has a token", async () => {
        localStorage.setItem(AUTH_TOKEN_KEY, "1.fake-token-aaaaaaaaaaaaaaaaaaaaaaaa");
        const fetchMock = mockFetch({ ok: true, json: async () => ({}) });
        await apiFetch("/api/content");
        const init = calledInit(fetchMock);
        expect(init.headers.get("Authorization")).toBe(
            "Bearer 1.fake-token-aaaaaaaaaaaaaaaaaaaaaaaa",
        );
    });

    it("sends no Authorization header when localStorage has no token", async () => {
        // beforeEach already cleared the token; just verify.
        const fetchMock = mockFetch({ ok: true, json: async () => ({}) });
        await apiFetch("/api/content");
        const init = calledInit(fetchMock);
        expect(init.headers.has("Authorization")).toBe(false);
    });

    it("clears the localStorage token on a 401 response", async () => {
        localStorage.setItem(AUTH_TOKEN_KEY, "1.stale-token");
        mockFetch({ ok: false, status: 401 });
        const { restore } = stubLocation("/");
        try {
            await apiFetch("/api/content").catch(() => {});
        } finally {
            restore();
        }
        expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBeNull();
    });

    it("redirects to /login.html on 401", async () => {
        localStorage.setItem(AUTH_TOKEN_KEY, "1.stale");
        mockFetch({ ok: false, status: 401 });
        const { replace, restore } = stubLocation("/");
        try {
            await apiFetch("/api/content").catch(() => {});
        } finally {
            restore();
        }
        expect(replace).toHaveBeenCalledWith("/login.html");
    });

    it("does NOT redirect when already on /login.html (no loop)", async () => {
        localStorage.setItem(AUTH_TOKEN_KEY, "1.stale");
        mockFetch({ ok: false, status: 401 });
        const { replace, restore } = stubLocation("/login.html");
        try {
            await apiFetch("/api/auth/login", { method: "POST" }).catch(() => {});
        } finally {
            restore();
        }
        expect(replace).not.toHaveBeenCalled();
    });

    it("throws on 401 so the caller's then-chain doesn't proceed against a stale Response", async () => {
        localStorage.setItem(AUTH_TOKEN_KEY, "1.stale");
        mockFetch({ ok: false, status: 401 });
        const { restore } = stubLocation("/login.html");
        try {
            await expect(apiFetch("/api/auth/login")).rejects.toThrow(/authentication required/);
        } finally {
            restore();
        }
    });
});
