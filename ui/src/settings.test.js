// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountSettings } from "./settings.js";

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
    gamma: 2.4,
    wifi_ssid: "openMarquee-A3F7",
    wifi_password: "correct-horse-battery",
    timezone: "America/New_York",
};

describe("mountSettings", () => {
    it("hydrates every field from the fetched settings", async () => {
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();

        expect(container.querySelector(".field-sign-name").value).toBe("Lobby");
        expect(container.querySelector(".field-output-mode").value).toBe("hdmi");
        expect(container.querySelector(".field-display-width").value).toBe("1920");
        expect(container.querySelector(".field-display-height").value).toBe("1080");
        expect(container.querySelector(".field-brightness").value).toBe("75");
        expect(container.querySelector(".field-gamma").value).toBe("2.4");
        expect(container.querySelector(".field-wifi-ssid").value).toBe(
            "openMarquee-A3F7",
        );
        expect(container.querySelector(".field-wifi-password").value).toBe(
            "correct-horse-battery",
        );
        expect(container.querySelector(".field-timezone").value).toBe(
            "America/New_York",
        );
    });

    it("output mode select covers every SYSTEM_SPEC output variant", async () => {
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const values = Array.from(
            container.querySelectorAll(".field-output-mode option"),
        ).map((o) => o.value);
        expect(values).toEqual(["hdmi", "hub75", "ws281x", "composite"]);
    });

    it("preserves a stored timezone value even if Intl doesn't surface it", async () => {
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => ({ ...SAMPLE, timezone: "Mars/Olympus_Mons" }),
            onSave: vi.fn(),
        });
        await tick();
        const tz = container.querySelector(".field-timezone");
        expect(tz.value).toBe("Mars/Olympus_Mons");
        const stored = Array.from(tz.options).find(
            (o) => o.value === "Mars/Olympus_Mons",
        );
        expect(stored.textContent).toMatch(/stored/);
    });

    it("Save sends the full settings payload to onSave", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave,
        });
        await tick();

        // Mutate a couple of fields and save.
        container.querySelector(".field-brightness").value = "42";
        container.querySelector(".field-sign-name").value = "Kitchen";
        container.querySelector(".settings-form").dispatchEvent(new Event("submit"));
        await tick();

        expect(onSave).toHaveBeenCalledTimes(1);
        const payload = onSave.mock.calls[0][0];
        expect(payload.sign_name).toBe("Kitchen");
        expect(payload.brightness).toBe(42);
        expect(payload.gamma).toBeCloseTo(2.4);
        expect(payload.display_width).toBe(1920);
        expect(payload.wifi_password).toBe("correct-horse-battery");
        expect(payload.timezone).toBe("America/New_York");
    });

    it("Save with timezone cleared sends null (not empty string)", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave,
        });
        await tick();

        container.querySelector(".field-timezone").value = "";
        container.querySelector(".settings-form").dispatchEvent(new Event("submit"));
        await tick();

        expect(onSave.mock.calls[0][0].timezone).toBeNull();
    });

    it("surfaces backend failures into the status line without throwing", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockRejectedValue(new Error("backend rejected"));
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave,
        });
        await tick();

        container.querySelector(".settings-form").dispatchEvent(new Event("submit"));
        await tick();

        expect(container.querySelector(".settings-status").textContent).toMatch(
            /Save failed: backend rejected/,
        );
    });
});
