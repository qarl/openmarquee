// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { mountPlaybackControls } from "./playback.js";

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

describe("mountPlaybackControls", () => {
    it("paints the button as 'Play all' when the backend reports not running", async () => {
        const container = document.createElement("div");
        mountPlaybackControls(container, {
            fetchState: async () => ({ is_running: false }),
            onStart: vi.fn(),
            onStop: vi.fn(),
        });
        await tick();
        const btn = container.querySelector(".playback-btn");
        expect(btn.textContent).toBe("Play all");
        expect(btn.classList.contains("primary")).toBe(true);
    });

    it("paints the button as 'Stop' when the backend reports running", async () => {
        const container = document.createElement("div");
        mountPlaybackControls(container, {
            fetchState: async () => ({ is_running: true }),
            onStart: vi.fn(),
            onStop: vi.fn(),
        });
        await tick();
        const btn = container.querySelector(".playback-btn");
        expect(btn.textContent).toBe("Stop");
        expect(btn.classList.contains("danger")).toBe(true);
    });

    it("click from stopped → running invokes onStart and flips label", async () => {
        const container = document.createElement("div");
        const onStart = vi.fn().mockResolvedValue(undefined);
        mountPlaybackControls(container, {
            fetchState: async () => ({ is_running: false }),
            onStart,
            onStop: vi.fn(),
        });
        await tick();

        container.querySelector(".playback-btn").click();
        await tick();

        expect(onStart).toHaveBeenCalledOnce();
        expect(container.querySelector(".playback-btn").textContent).toBe("Stop");
    });

    it("click from running → stopped invokes onStop and flips label", async () => {
        const container = document.createElement("div");
        const onStop = vi.fn().mockResolvedValue(undefined);
        mountPlaybackControls(container, {
            fetchState: async () => ({ is_running: true }),
            onStart: vi.fn(),
            onStop,
        });
        await tick();

        container.querySelector(".playback-btn").click();
        await tick();

        expect(onStop).toHaveBeenCalledOnce();
        expect(container.querySelector(".playback-btn").textContent).toBe("Play all");
    });

    it("shows an error message and reverts state when onStart rejects", async () => {
        const container = document.createElement("div");
        mountPlaybackControls(container, {
            fetchState: async () => ({ is_running: false }),
            onStart: async () => {
                throw new Error("backend rejected");
            },
            onStop: vi.fn(),
        });
        await tick();

        container.querySelector(".playback-btn").click();
        await tick();

        expect(container.querySelector(".playback-status").textContent).toContain(
            "backend rejected",
        );
        // Still reads as "Play all" because the click errored.
        expect(container.querySelector(".playback-btn").textContent).toBe("Play all");
    });

    it("exposes a refresh() that re-queries backend state", async () => {
        const container = document.createElement("div");
        const fetchState = vi
            .fn()
            .mockResolvedValueOnce({ is_running: false })
            .mockResolvedValueOnce({ is_running: true });
        const { refresh, stopPolling } = mountPlaybackControls(container, {
            fetchState,
            onStart: vi.fn(),
            onStop: vi.fn(),
        });
        stopPolling(); // don't let the 5s interval fire during tests
        await tick();
        expect(container.querySelector(".playback-btn").textContent).toBe("Play all");

        await refresh();
        expect(container.querySelector(".playback-btn").textContent).toBe("Stop");
        expect(fetchState).toHaveBeenCalledTimes(2);
    });

    it("shows 'Now playing: <name>' when state has current_playlist_name", async () => {
        const container = document.createElement("div");
        const { stopPolling } = mountPlaybackControls(container, {
            fetchState: async () => ({
                is_running: true,
                current_playlist_name: "lunch",
            }),
            onStart: vi.fn(),
            onStop: vi.fn(),
        });
        stopPolling();
        await tick();
        expect(container.querySelector(".playback-now-playing").textContent).toBe(
            "Now playing: lunch",
        );
    });

    it("shows 'Running…' when running but no current playlist name yet", async () => {
        const container = document.createElement("div");
        const { stopPolling } = mountPlaybackControls(container, {
            fetchState: async () => ({
                is_running: true,
                current_playlist_name: null,
            }),
            onStart: vi.fn(),
            onStop: vi.fn(),
        });
        stopPolling();
        await tick();
        expect(container.querySelector(".playback-now-playing").textContent).toBe(
            "Running…",
        );
    });

    it("clears the now-playing badge when stopped", async () => {
        const container = document.createElement("div");
        const { stopPolling } = mountPlaybackControls(container, {
            fetchState: async () => ({
                is_running: false,
                current_playlist_name: null,
            }),
            onStart: vi.fn(),
            onStop: vi.fn(),
        });
        stopPolling();
        await tick();
        expect(container.querySelector(".playback-now-playing").textContent).toBe("");
    });
});
