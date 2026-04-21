import { afterEach, describe, expect, it, vi } from "vitest";
import {
    deleteContent,
    fetchHealth,
    getPlaybackState,
    getSchedule,
    listContent,
    playContent,
    saveImage,
    saveSchedule,
    saveTextSlide,
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
        const result = await saveImage({ name: "Logo", png_base64: "FAKE" });
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
