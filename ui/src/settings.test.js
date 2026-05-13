// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountSettings } from "./settings.js";

afterEach(() => {
    vi.restoreAllMocks();
});

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

// Batch 20.4: GET /api/settings now returns the secret fields redacted
// (sentinel "<set>" when populated, null when unset). The fixture
// reflects the wire shape post-20.4.
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
    wifi_password: "<set>",
    wifi_station_enabled: false,
    wifi_station_ssid: null,
    wifi_station_password: null,
    timezone: "America/New_York",
    tailscale_enabled: false,
    tailscale_hostname: null,
    tailscale_auth_key: null,
};

describe("mountSettings", () => {
    it("device_id row is read-only and populated from /api/system/info when present", async () => {
        // qarl 2026-05-12 (a2): MySignXXX device_id is exposed by
        // /api/system/info, surfaced as a read-only row above the
        // (editable) display name. Mock fetch so we don't hit the
        // real network from jsdom.
        // URL-routed mock: only respond to /api/system/info; let any
        // other fetch call (rescan, etc.) return an empty-ok stub so
        // the rest of the page doesn't crash.
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
            (url) => Promise.resolve({
                ok: true,
                json: async () => String(url).includes("/api/system/info")
                    ? { device_id: "MySign7K2" }
                    : { networks: [] },
            }),
        );
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const row = container.querySelector(".field-device-id-row");
        const input = container.querySelector(".field-device-id");
        expect(row).not.toBeNull();
        expect(row.hidden).toBe(false);
        expect(input.value).toBe("MySign7K2");
        expect(input.readOnly).toBe(true);
        // The Sign-name field is the editable display label; device_id
        // row is its IMMUTABLE companion, not a replacement.
        expect(container.querySelector(".field-sign-name").value).toBe("Lobby");
        fetchSpy.mockRestore();
    });

    it("device_id row is hidden when /api/system/info returns null device_id", async () => {
        // Off-device dev / pre-firstboot: identity.json absent ->
        // device_id is null. Row must hide so the operator doesn't see
        // a blank "Device ID:" label.
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue({
            ok: true,
            json: async () => ({ device_id: null }),
        });
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const row = container.querySelector(".field-device-id-row");
        expect(row.hidden).toBe(true);
        fetchSpy.mockRestore();
    });

    it("device_id row stays hidden when /api/system/info fetch errors", async () => {
        // Network glitch / 401 from the auth middleware / etc -- the
        // settings page must still render. Row default-hides.
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValue(
            new Error("network glitch"),
        );
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const row = container.querySelector(".field-device-id-row");
        expect(row.hidden).toBe(true);
        // And the rest of the form still hydrated.
        expect(container.querySelector(".field-sign-name").value).toBe("Lobby");
        fetchSpy.mockRestore();
    });

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
        // Batch 20.4: field-wifi-password is now a hidden input carrying
        // the redacted wire value; the displayed indicator is in
        // .secret-status. The hidden value is what gets echoed back on
        // PUT so the backend can substitute the stored value.
        expect(container.querySelector(".field-wifi-password").value).toBe("<set>");
        const apSecretStatus = container.querySelector(
            '.secret-field[data-secret="wifi-ap-password"] .secret-status',
        );
        expect(apSecretStatus.textContent).toMatch(/Set/i);
        expect(container.querySelector(".field-timezone").value).toBe(
            "America/New_York",
        );
    });

    it("Rescan nearby networks button renders with a visible border", async () => {
        // Bug B14 (qarl batch 2026-04-29): the button used `om-btn ghost`
        // which strips the border, making it read as a hint string
        // rather than a tappable control next to the SSID field.
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const rescanBtn = container.querySelector(".settings-wifi-rescan");
        expect(rescanBtn).not.toBeNull();
        expect(rescanBtn.classList.contains("ghost")).toBe(false);
    });

    it("Detect from device button renders with a visible border", async () => {
        // qarl 2026-04-30 follow-up to B12-B14: same `om-btn ghost
        // field-hint-btn` combo as Rescan — flagged in 67e50a1's review
        // as out-of-scope, now greenlit.
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const detectBtn = container.querySelector(".settings-detect-dims");
        expect(detectBtn).not.toBeNull();
        expect(detectBtn.classList.contains("ghost")).toBe(false);
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
        // Batch 20.4: PUT body echoes the redacted sentinel; the
        // backend substitutes the stored value before persisting.
        expect(payload.wifi_password).toBe("<set>");
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
                // Batch 20.4: redacted wire value.
                wifi_station_password: "<set>",
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
        // Batch 20.4: PUT body echoes the sentinel; the backend
        // substitutes the stored value.
        expect(p.wifi_station_password).toBe("<set>");
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
                // Batch 20.4: redacted wire value.
                tailscale_auth_key: "<set>",
            }),
            onSave,
        });
        await tick();

        expect(container.querySelector(".field-tailscale-enabled").checked).toBe(true);
        expect(container.querySelector(".field-tailscale-hostname").value).toBe(
            "lobby-sign-01",
        );
        // Batch 20.4: hidden input carries the redacted wire value.
        expect(container.querySelector(".field-tailscale-auth-key").value).toBe("<set>");
        const tsSecretStatus = container.querySelector(
            '.secret-field[data-secret="tailscale-auth-key"] .secret-status',
        );
        expect(tsSecretStatus.textContent).toMatch(/Set/i);

        container.querySelector(".settings-form").dispatchEvent(new Event("submit"));
        await tick();
        const payload = onSave.mock.calls[0][0];
        expect(payload.tailscale_enabled).toBe(true);
        expect(payload.tailscale_hostname).toBe("lobby-sign-01");
        // Batch 20.4: PUT body echoes the sentinel.
        expect(payload.tailscale_auth_key).toBe("<set>");
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

    // --- Batch 20.4: secret-field UI (Change… inline form) ---

    it("renders 'Set' indicator when GET returns the <set> sentinel", async () => {
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const apStatus = container.querySelector(
            '.secret-field[data-secret="wifi-ap-password"] .secret-status',
        );
        expect(apStatus.textContent).toMatch(/Set/i);
        // station + tailscale start null (per SAMPLE) -> "Not set"
        const stationStatus = container.querySelector(
            '.secret-field[data-secret="wifi-station-password"] .secret-status',
        );
        expect(stationStatus.textContent).toMatch(/Not set/i);
        const tsStatus = container.querySelector(
            '.secret-field[data-secret="tailscale-auth-key"] .secret-status',
        );
        expect(tsStatus.textContent).toMatch(/Not set/i);
    });

    it("Change… reveals the inline current_password + new_value form", async () => {
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const row = container.querySelector(
            '.secret-field[data-secret="wifi-ap-password"]',
        );
        expect(row.querySelector(".secret-form").hidden).toBe(true);
        row.querySelector(".secret-change-btn").click();
        expect(row.querySelector(".secret-form").hidden).toBe(false);
        expect(row.querySelector(".secret-display").hidden).toBe(true);
    });

    it("Cancel collapses the form back to the display row", async () => {
        const container = document.createElement("div");
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const row = container.querySelector(
            '.secret-field[data-secret="wifi-ap-password"]',
        );
        row.querySelector(".secret-change-btn").click();
        row.querySelector(".secret-current-password").value = "typed-something";
        row.querySelector(".secret-cancel-btn").click();
        expect(row.querySelector(".secret-form").hidden).toBe(true);
        expect(row.querySelector(".secret-display").hidden).toBe(false);
        // Cancel clears the inputs so the next open() doesn't reveal
        // what the operator typed before.
        expect(row.querySelector(".secret-current-password").value).toBe("");
    });

    it("Save Secret PATCHes the right endpoint with current_password + new_value", async () => {
        const container = document.createElement("div");
        const fetchMock = vi.fn().mockResolvedValue(
            new Response(JSON.stringify({ wifi_password: "<set>" }), {
                status: 200,
                headers: { "Content-Type": "application/json" },
            }),
        );
        vi.stubGlobal("fetch", fetchMock);
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const row = container.querySelector(
            '.secret-field[data-secret="wifi-ap-password"]',
        );
        row.querySelector(".secret-change-btn").click();
        row.querySelector(".secret-current-password").value = "hunter2hunter";
        row.querySelector(".secret-new-value").value = "new-ap-pass-12";
        row.querySelector(".secret-save-btn").click();
        await tick();
        await tick();
        // First fetch call: PATCH /api/settings/wifi-ap-password.
        const patchCall = fetchMock.mock.calls.find(([url]) =>
            String(url).endsWith("/api/settings/wifi-ap-password"),
        );
        expect(patchCall).toBeDefined();
        const init = patchCall[1];
        expect(init.method).toBe("PATCH");
        expect(JSON.parse(init.body)).toEqual({
            current_password: "hunter2hunter",
            new_value: "new-ap-pass-12",
        });
    });

    it("Save Secret shows 'Incorrect current password' on 401", async () => {
        const container = document.createElement("div");
        const fetchMock = vi.fn().mockImplementation(async (url) => {
            const u = String(url);
            if (u.endsWith("/api/settings/wifi-ap-password")) {
                return new Response(JSON.stringify({ detail: "wrong" }), {
                    status: 401,
                    headers: { "Content-Type": "application/json" },
                });
            }
            // GET /api/settings + display-dims/wifi-scan stubs.
            return new Response(JSON.stringify(SAMPLE), {
                status: 200,
                headers: { "Content-Type": "application/json" },
            });
        });
        vi.stubGlobal("fetch", fetchMock);
        mountSettings(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const row = container.querySelector(
            '.secret-field[data-secret="wifi-ap-password"]',
        );
        row.querySelector(".secret-change-btn").click();
        row.querySelector(".secret-current-password").value = "wrong";
        row.querySelector(".secret-new-value").value = "doesnt-matter";
        row.querySelector(".secret-save-btn").click();
        await tick();
        await tick();
        const err = row.querySelector(".secret-error");
        expect(err.textContent).toMatch(/Incorrect current password/i);
        // Form stays open so the operator can retry.
        expect(row.querySelector(".secret-form").hidden).toBe(false);
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
