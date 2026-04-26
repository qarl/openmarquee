// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mountSettings } from "./settings.js";

beforeEach(() => {
    // Stub global fetch so the wifi-scan and display-dims helpers — which
    // mountSettings kicks off during refresh() — don't crash on jsdom's
    // missing base URL and dump multi-line stack traces into the test
    // output (regression: QA 2026-04-26 #06). Returns an empty network
    // list so the picker degrades to its "(type manually)" fallback.
    vi.stubGlobal(
        "fetch",
        vi.fn(async (url) => {
            const path = String(url || "");
            if (path.endsWith("/api/system/wifi-scan")) {
                return new Response(JSON.stringify({ networks: [] }), {
                    status: 200,
                    headers: { "Content-Type": "application/json" },
                });
            }
            if (path.endsWith("/api/system/display-dims")) {
                return new Response(JSON.stringify({}), {
                    status: 200,
                    headers: { "Content-Type": "application/json" },
                });
            }
            return new Response("", { status: 404 });
        }),
    );
});

afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
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
    display_rotation: 0,
    brightness: 75,
    gamma: 2.4,
    wifi_ap_enabled: true,
    wifi_ssid: "openMarquee-A3F7",
    wifi_password: "correct-horse-battery",
    wifi_station_enabled: false,
    wifi_station_ssid: null,
    wifi_station_password: null,
    timezone: "America/New_York",
    tailscale_enabled: false,
    tailscale_hostname: null,
    tailscale_auth_key: null,
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

    it("WiFi station fieldset is grayed out when station toggle is off", async () => {
        const container = document.createElement("div");
        mountSettings(container, { fetchSettings: async () => SAMPLE, onSave: vi.fn() });
        await tick();
        const stationFieldset = container.querySelector(".settings-wifi-station");
        expect(stationFieldset.classList.contains("is-disabled")).toBe(true);
        expect(container.querySelector(".field-wifi-station-ssid").disabled).toBe(true);
    });

    it("enabling the station toggle un-grays its fieldset", async () => {
        const container = document.createElement("div");
        mountSettings(container, { fetchSettings: async () => SAMPLE, onSave: vi.fn() });
        await tick();
        const toggle = container.querySelector(".field-wifi-station-enabled");
        toggle.checked = true;
        toggle.dispatchEvent(new Event("change"));
        const stationFieldset = container.querySelector(".settings-wifi-station");
        expect(stationFieldset.classList.contains("is-disabled")).toBe(false);
        expect(container.querySelector(".field-wifi-station-ssid").disabled).toBe(false);
    });

    it("refuses to let the operator disable both WiFi modes", async () => {
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => ({ ...SAMPLE, wifi_station_enabled: false }),
            onSave: vi.fn(),
        });
        await tick();
        // Only AP is on. Try to turn it off.
        const apToggle = container.querySelector(".field-wifi-ap-enabled");
        apToggle.checked = false;
        apToggle.dispatchEvent(new Event("change"));
        // Bounced back on.
        expect(apToggle.checked).toBe(true);
        expect(container.querySelector(".settings-status").textContent).toMatch(
            /can't disable both/i,
        );
    });

    it("sends all WiFi fields in the save payload", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountSettings(container, {
            fetchSettings: async () => ({
                ...SAMPLE,
                wifi_station_enabled: true,
                wifi_station_ssid: "home-net",
                wifi_station_password: "correct-horse-battery",
            }),
            onSave,
        });
        await tick();
        container.querySelector(".settings-form").dispatchEvent(new Event("submit"));
        await tick();
        const p = onSave.mock.calls[0][0];
        expect(p.wifi_ap_enabled).toBe(true);
        expect(p.wifi_station_enabled).toBe(true);
        expect(p.wifi_station_ssid).toBe("home-net");
        expect(p.wifi_station_password).toBe("correct-horse-battery");
    });

    it("rotation dropdown exposes the four cardinal angles + hydrates from settings", async () => {
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => ({ ...SAMPLE, display_rotation: 90 }),
            onSave: vi.fn(),
        });
        await tick();
        const rot = container.querySelector(".field-display-rotation");
        const values = Array.from(rot.options).map((o) => o.value);
        expect(values).toEqual(["0", "90", "180", "270"]);
        expect(rot.value).toBe("90");
    });

    it("changing output_mode from a default snaps dims to the new mode's default", async () => {
        const container = document.createElement("div");
        // Operator is at HUB75 defaults (128x96).
        mountSettings(container, {
            fetchSettings: async () => ({
                ...SAMPLE,
                output_mode: "hub75",
                display_width: 128,
                display_height: 96,
            }),
            onSave: vi.fn(),
        });
        await tick();

        const modeEl = container.querySelector(".field-output-mode");
        modeEl.value = "hdmi";
        modeEl.dispatchEvent(new Event("change"));

        expect(container.querySelector(".field-display-width").value).toBe("1920");
        expect(container.querySelector(".field-display-height").value).toBe("1080");
    });

    it("leaves customized dims alone on output_mode change", async () => {
        const container = document.createElement("div");
        // Operator has non-default dims (say a 256x128 HUB75 cluster).
        mountSettings(container, {
            fetchSettings: async () => ({
                ...SAMPLE,
                output_mode: "hub75",
                display_width: 256,
                display_height: 128,
            }),
            onSave: vi.fn(),
        });
        await tick();

        const modeEl = container.querySelector(".field-output-mode");
        modeEl.value = "hdmi";
        modeEl.dispatchEvent(new Event("change"));

        // Dims untouched — we don't know what HDMI panel the operator plans.
        expect(container.querySelector(".field-display-width").value).toBe("256");
        expect(container.querySelector(".field-display-height").value).toBe("128");
    });

    it("saves display_rotation in the payload", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave,
        });
        await tick();
        container.querySelector(".field-display-rotation").value = "270";
        container.querySelector(".settings-form").dispatchEvent(new Event("submit"));
        await tick();
        expect(onSave.mock.calls[0][0].display_rotation).toBe(270);
    });

    it("hydrates Tailscale fields + round-trips them to onSave", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountSettings(container, {
            fetchSettings: async () => ({
                ...SAMPLE,
                tailscale_enabled: true,
                tailscale_hostname: "lobby-sign-01",
                tailscale_auth_key: "tskey-auth-existing-12345",
            }),
            onSave,
        });
        await tick();

        expect(container.querySelector(".field-tailscale-enabled").checked).toBe(true);
        expect(container.querySelector(".field-tailscale-hostname").value).toBe(
            "lobby-sign-01",
        );
        expect(container.querySelector(".field-tailscale-auth-key").value).toBe(
            "tskey-auth-existing-12345",
        );

        container.querySelector(".settings-form").dispatchEvent(new Event("submit"));
        await tick();
        const payload = onSave.mock.calls[0][0];
        expect(payload.tailscale_enabled).toBe(true);
        expect(payload.tailscale_hostname).toBe("lobby-sign-01");
        expect(payload.tailscale_auth_key).toBe("tskey-auth-existing-12345");
    });

    it("sends Tailscale hostname + key as null when cleared", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave,
        });
        await tick();

        container.querySelector(".field-tailscale-hostname").value = "  ";
        container.querySelector(".field-tailscale-auth-key").value = "";
        container.querySelector(".settings-form").dispatchEvent(new Event("submit"));
        await tick();

        const payload = onSave.mock.calls[0][0];
        expect(payload.tailscale_hostname).toBeNull();
        expect(payload.tailscale_auth_key).toBeNull();
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
