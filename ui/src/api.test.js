import { afterEach, describe, expect, it, vi } from "vitest";
import {
    deleteContent,
    fetchHealth,
    generateBackground,
    getPlaybackState,
    getSchedule,
    getSettings,
    listContent,
    playContent,
    saveImage,
    saveSchedule,
    saveSettings,
    saveTextSlide,
    saveVideo,
    setPlaylistOrder,
    startPlayback,
    stopPlayback,
} from "./api.js";

afterEach(() => {
    vi.unstubAllGlobals();
});

function mockFetch(response) {
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
}

describe("fetchHealth", () => {
    it("returns the JSON body when the response is ok", async () => {
        mockFetch({
            ok: true,
            json: async () => ({ status: "alive", version: "0.0.0" }),
        });

        const result = await fetchHealth();
        expect(result).toEqual({ status: "alive", version: "0.0.0" });
    });

    it("throws when the response is not ok", async () => {
        mockFetch({ ok: false, status: 503 });
        await expect(fetchHealth()).rejects.toThrow("503");
    });

    it("calls the right endpoint", async () => {
        const fetchMock = mockFetch({ ok: true, json: async () => ({}) });
        await fetchHealth();
        expect(fetchMock).toHaveBeenCalledWith("/healthz");
    });
});

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
        expect(init.headers).toEqual({ "Content-Type": "application/json" });
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
        expect(fetchMock).toHaveBeenCalledWith("/api/content");
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
        expect(fetchMock).toHaveBeenCalledWith("/api/content/abc", { method: "DELETE" });
    });

    it("throws on non-ok response", async () => {
        mockFetch({ ok: false, status: 404 });
        await expect(deleteContent("missing")).rejects.toThrow("404");
    });
});

describe("playContent", () => {
    it("POSTs /dev/play/{id}", async () => {
        const fetchMock = mockFetch({ ok: true });
        await playContent("abc");
        expect(fetchMock).toHaveBeenCalledWith("/dev/play/abc", { method: "POST" });
    });

    it("throws on non-ok response", async () => {
        mockFetch({ ok: false, status: 422 });
        await expect(playContent("bad")).rejects.toThrow("422");
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

describe("named-playlists API", () => {
    it("listPlaylists GETs /api/playlists", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ schema_version: 2, playlists: {} }),
        });
        const result = await (await import("./api.js")).listPlaylists();
        expect(result.schema_version).toBe(2);
        expect(fetchMock).toHaveBeenCalledWith("/api/playlists");
    });

    it("savePlaylistByName PUTs to /api/playlists/{name} with item_ids body", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ item_ids: ["a", "b"] }),
        });
        const { savePlaylistByName } = await import("./api.js");
        const result = await savePlaylistByName("lunch", ["a", "b"]);
        expect(result.item_ids).toEqual(["a", "b"]);
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/playlists/lunch");
        expect(init.method).toBe("PUT");
        expect(JSON.parse(init.body)).toEqual({ item_ids: ["a", "b"] });
    });

    it("deletePlaylistByName DELETEs /api/playlists/{name}", async () => {
        const fetchMock = mockFetch({ ok: true });
        const { deletePlaylistByName } = await import("./api.js");
        await deletePlaylistByName("lunch");
        expect(fetchMock).toHaveBeenCalledWith("/api/playlists/lunch", {
            method: "DELETE",
        });
    });

    it("savePlaylistByName encodes funky names in the URL path", async () => {
        const fetchMock = mockFetch({ ok: true, json: async () => ({}) });
        const { savePlaylistByName } = await import("./api.js");
        await savePlaylistByName("spaces in name", []);
        const [url] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/playlists/spaces%20in%20name");
    });

    it("savePlaylistByName routes v3 entry objects to the `items` key, not `item_ids`", async () => {
        // Regression: sending {item_id, transition, transition_ms} objects
        // under the `item_ids` key 422s server-side ("UUID input should
        // be a string"). Same shape detection as setPlaylistOrder.
        const fetchMock = mockFetch({ ok: true, json: async () => ({}) });
        const { savePlaylistByName } = await import("./api.js");
        const entries = [
            { item_id: "a-uuid", transition: "fade", transition_ms: 500 },
            { item_id: "b-uuid", transition: "cut", transition_ms: 500 },
        ];
        await savePlaylistByName("default", entries);
        const [, init] = fetchMock.mock.calls[0];
        const body = JSON.parse(init.body);
        expect(body).toHaveProperty("items");
        expect(body.items).toEqual(entries);
        expect(body).not.toHaveProperty("item_ids");
    });

    it("savePlaylistByName keeps legacy string-array callers on the `item_ids` key", async () => {
        const fetchMock = mockFetch({ ok: true, json: async () => ({}) });
        const { savePlaylistByName } = await import("./api.js");
        await savePlaylistByName("default", ["a", "b"]);
        const [, init] = fetchMock.mock.calls[0];
        const body = JSON.parse(init.body);
        expect(body).toEqual({ item_ids: ["a", "b"] });
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
        expect(fetchMock).toHaveBeenCalledWith("/api/schedules");
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
        expect(fetchMock).toHaveBeenCalledWith("/api/settings");
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

describe("setPlaylistOrder", () => {
    it("PUTs item_ids to /api/playlist", async () => {
        const fetchMock = mockFetch({
            ok: true,
            json: async () => ({ item_ids: ["b", "a"] }),
        });
        const result = await setPlaylistOrder(["b", "a"]);
        expect(result).toEqual({ item_ids: ["b", "a"] });
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/playlist");
        expect(init.method).toBe("PUT");
        expect(JSON.parse(init.body)).toEqual({ item_ids: ["b", "a"] });
    });

    it("throws on non-ok response", async () => {
        mockFetch({ ok: false, status: 422, text: async () => "bad" });
        await expect(setPlaylistOrder(["not-a-uuid"])).rejects.toThrow("422");
    });
});

describe("playback control API", () => {
    it("getPlaybackState GETs /api/playback/state", async () => {
        const state = { is_running: true, current_item_id: "abc" };
        const fetchMock = mockFetch({ ok: true, json: async () => state });
        const result = await getPlaybackState();
        expect(result).toEqual(state);
        expect(fetchMock).toHaveBeenCalledWith("/api/playback/state");
    });

    it("startPlayback POSTs /api/playback/start", async () => {
        const fetchMock = mockFetch({ ok: true });
        await startPlayback();
        expect(fetchMock).toHaveBeenCalledWith("/api/playback/start", { method: "POST" });
    });

    it("stopPlayback POSTs /api/playback/stop", async () => {
        const fetchMock = mockFetch({ ok: true });
        await stopPlayback();
        expect(fetchMock).toHaveBeenCalledWith("/api/playback/stop", { method: "POST" });
    });

    it("throws on non-ok responses", async () => {
        mockFetch({ ok: false, status: 500 });
        await expect(startPlayback()).rejects.toThrow("500");
    });
});
