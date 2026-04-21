// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountSettingsView } from "./settings-view.js";

afterEach(() => {
    vi.restoreAllMocks();
});

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

const SAMPLE = {
    schema_version: 1,
    sign_name: "Lobby",
    output_mode: "hdmi",
    display_width: 1920,
    display_height: 1080,
    brightness: 75,
    gamma: 2.2,
    wifi_ssid: "OpenMarquee-A3F7",
    wifi_password: "correct-horse-battery",
    timezone: "America/New_York",
};

describe("mountSettingsView", () => {
    it("renders every field with its value", async () => {
        const container = document.createElement("div");
        mountSettingsView(container, { fetchSettings: async () => SAMPLE });
        await tick();
        expect(container.querySelector('dd[data-key="sign_name"]').textContent).toBe("Lobby");
        expect(container.querySelector('dd[data-key="output_mode"]').textContent).toBe("hdmi");
        expect(container.querySelector('dd[data-key="display_width"]').textContent).toBe("1920");
        expect(container.querySelector('dd[data-key="brightness"]').textContent).toBe("75");
        expect(container.querySelector('dd[data-key="timezone"]').textContent).toBe(
            "America/New_York",
        );
    });

    it("masks the wifi_password rather than leaking it into the DOM", async () => {
        const container = document.createElement("div");
        mountSettingsView(container, { fetchSettings: async () => SAMPLE });
        await tick();
        const dd = container.querySelector('dd[data-key="wifi_password"]');
        expect(dd.textContent).not.toContain("correct-horse-battery");
        expect(dd.textContent.length).toBeGreaterThan(0);
    });

    it("falls back to '(device local)' when timezone is null", async () => {
        const container = document.createElement("div");
        mountSettingsView(container, {
            fetchSettings: async () => ({ ...SAMPLE, timezone: null }),
        });
        await tick();
        expect(container.querySelector('dd[data-key="timezone"]').textContent).toBe(
            "(device local)",
        );
    });

    it("surfaces fetch errors into the status line", async () => {
        const container = document.createElement("div");
        mountSettingsView(container, {
            fetchSettings: async () => {
                throw new Error("network");
            },
        });
        await tick();
        expect(container.querySelector(".settings-status").textContent).toMatch(
            /Could not load/,
        );
    });
});
