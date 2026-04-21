import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchHealth } from "./api.js";

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
