import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchHealth, saveTextSlide } from "./api.js";

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
