// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountSettings } from "./settings.js";

afterEach(() => {
    vi.restoreAllMocks();
});

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

// arc-3 (qarl 2026-05-12): mountSettings switched from explicit Save
// button to attachAutoSave debounced PUT. Tests use this helper to
// inject debounceMs:0 so dispatching an `input` event on the form
// fires the save on the next microtask, matching the (pre-arc-3)
// behavior where dispatching `submit` called onSave synchronously.
function mount(container, opts) {
    return mountSettings(container, { debounceMs: 0, ...opts });
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
    // r53: HTTPS toggle defaults to true on the model; tests that
    // override SETTINGS pass their own value when needed.
    tailscale_https_enabled: true,
};

describe("mountSettings", () => {
    it("wifi mode fieldsets use single-select 'Create / Join' radio copy", async () => {
        // 2026-07-14 (qarl): the two WiFi modes were renamed and
        // converted from concurrent checkboxes to single-select radios.
        // "Setup Mode" → "Create WiFi Network"; "Join existing WiFi" →
        // "Join Existing Network". Copy + control-type regression guard.
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue({
            ok: true,
            json: async () => ({}),
        });
        const container = document.createElement("div");
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const text = container.textContent;
        // New mode names MUST appear.
        expect(text).toMatch(/Create WiFi Network/);
        expect(text).toMatch(/Join Existing Network/);
        // Old names/copy MUST NOT appear.
        expect(text).not.toMatch(/Setup Mode/);
        expect(text).not.toMatch(/Join existing WiFi/);
        expect(text).not.toMatch(/Access point \(captive-portal/i);
        expect(text).not.toMatch(/Runs concurrently with the access point/i);
        // Both modes are single-select radios sharing one group.
        const apRadio = container.querySelector(".field-wifi-ap-enabled");
        const staRadio = container.querySelector(".field-wifi-station-enabled");
        expect(apRadio.type).toBe("radio");
        expect(staRadio.type).toBe("radio");
        expect(apRadio.name).toBe(staRadio.name);
        expect(apRadio.name).toBeTruthy();
        // Field label renamed off "Setup Mode …".
        const ssidLabel = container.querySelector(".field-wifi-ssid")
            ?.closest("label")
            ?.querySelector("span")?.textContent;
        expect(ssidLabel).toMatch(/Network name \(SSID\)/);
        fetchSpy.mockRestore();
    });

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
        mount(container, {
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
        mount(container, {
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
        mount(container, {
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
        mount(container, {
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
        mount(container, {
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
        mount(container, {
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
        mount(container, {
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
        mount(container, {
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
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave,
        });
        await tick();

        // Mutate a couple of fields and save.
        container.querySelector(".field-brightness").value = "42";
        container.querySelector(".field-sign-name").value = "Kitchen";
        container.querySelector(".settings-form").dispatchEvent(new Event("input", { bubbles: true }));
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
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave,
        });
        await tick();

        container.querySelector(".field-timezone").value = "";
        container.querySelector(".settings-form").dispatchEvent(new Event("input", { bubbles: true }));
        await tick();

        expect(onSave.mock.calls[0][0].timezone).toBeNull();
    });

    it("Join section is collapsed (is-disabled) when Join is not selected", async () => {
        // 2026-07-14 (qarl): the deselected mode's body is collapsed via
        // CSS (.is-disabled > :not(legend) { display:none }); the class
        // toggle is the tested mechanism, the display:none is CSS-only.
        const container = document.createElement("div");
        mount(container, { fetchSettings: async () => SAMPLE, onSave: vi.fn() });
        await tick();
        const stationFieldset = container.querySelector(".settings-wifi-station");
        expect(stationFieldset.classList.contains("is-disabled")).toBe(true);
        expect(container.querySelector(".field-wifi-station-ssid").disabled).toBe(true);
    });

    it("selecting Join expands its section (removes is-disabled)", async () => {
        const container = document.createElement("div");
        mount(container, { fetchSettings: async () => SAMPLE, onSave: vi.fn() });
        await tick();
        const joinRadio = container.querySelector(".field-wifi-station-enabled");
        joinRadio.checked = true;
        joinRadio.dispatchEvent(new Event("change"));
        const stationFieldset = container.querySelector(".settings-wifi-station");
        expect(stationFieldset.classList.contains("is-disabled")).toBe(false);
        expect(container.querySelector(".field-wifi-station-ssid").disabled).toBe(false);
    });

    it("WiFi modes are mutually-exclusive radios (selecting one deselects the other)", async () => {
        // 2026-07-14 (qarl): checkboxes → single-select radios. The old
        // "can't disable both" guard is gone — a radio group makes the
        // both-off state structurally unreachable.
        const container = document.createElement("div");
        mount(container, {
            fetchSettings: async () => ({ ...SAMPLE, wifi_station_enabled: false }),
            onSave: vi.fn(),
        });
        await tick();
        const apRadio = container.querySelector(".field-wifi-ap-enabled");
        const staRadio = container.querySelector(".field-wifi-station-enabled");
        // Fresh device (station off): Create selected, Join not.
        expect(apRadio.checked).toBe(true);
        expect(staRadio.checked).toBe(false);
        // Selecting Join deselects Create (radio-group exclusivity).
        staRadio.checked = true;
        staRadio.dispatchEvent(new Event("change"));
        expect(staRadio.checked).toBe(true);
        expect(apRadio.checked).toBe(false);
        // Join expands, Create collapses — is-disabled drives the CSS
        // collapse (display:none on the deselected mode's body).
        expect(
            container.querySelector(".settings-wifi-station").classList.contains("is-disabled"),
        ).toBe(false);
        expect(
            container.querySelector(".settings-wifi-ap").classList.contains("is-disabled"),
        ).toBe(true);
    });

    it("collapses a legacy both-modes-enabled device to a single Join selection", async () => {
        // 2026-07-14 (qarl): the old concurrent regime allowed AP+STA
        // both on. Under single-select radios, loading such a device
        // selects Join (station is the more specific intent), and the
        // next save writes the mutually-exclusive pair — migrating the
        // device off concurrent mode.
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mount(container, {
            fetchSettings: async () => ({
                ...SAMPLE,
                wifi_ap_enabled: true,
                wifi_station_enabled: true,
            }),
            onSave,
        });
        await tick();
        const apRadio = container.querySelector(".field-wifi-ap-enabled");
        const staRadio = container.querySelector(".field-wifi-station-enabled");
        expect(staRadio.checked).toBe(true);
        expect(apRadio.checked).toBe(false);
        // Saving migrates the concurrent device to Join-only.
        container.querySelector(".settings-form").dispatchEvent(new Event("input", { bubbles: true }));
        await tick();
        const p = onSave.mock.calls[0][0];
        expect(p.wifi_ap_enabled).toBe(false);
        expect(p.wifi_station_enabled).toBe(true);
    });

    it("nests the saved-networks list in Join and hides it under Create", async () => {
        // 2026-07-14 (qarl): the wifi-networks list moved INTO the Join
        // Existing Network section and is hidden (not just grayed) when
        // Create WiFi Network is selected — only relevant when joining.
        const container = document.createElement("div");
        mount(container, {
            fetchSettings: async () => ({ ...SAMPLE, wifi_station_enabled: false }),
            onSave: vi.fn(),
        });
        await tick();
        const networks = container.querySelector(".settings-wifi-networks");
        const station = container.querySelector(".settings-wifi-station");
        // Nested inside the Join Existing Network fieldset.
        expect(station.contains(networks)).toBe(true);
        // Create selected → list hidden.
        expect(networks.hidden).toBe(true);
        // Select Join → list shown.
        const staRadio = container.querySelector(".field-wifi-station-enabled");
        staRadio.checked = true;
        staRadio.dispatchEvent(new Event("change"));
        expect(networks.hidden).toBe(false);
    });

    it("sends all WiFi fields in the save payload", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mount(container, {
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
        container.querySelector(".settings-form").dispatchEvent(new Event("input", { bubbles: true }));
        await tick();
        const p = onSave.mock.calls[0][0];
        // Single-select: loading with station enabled selects Join, so
        // the payload is mutually exclusive (AP off, station on).
        expect(p.wifi_ap_enabled).toBe(false);
        expect(p.wifi_station_enabled).toBe(true);
        expect(p.wifi_station_ssid).toBe("home-net");
        // Batch 20.4: PUT body echoes the sentinel; the backend
        // substitutes the stored value.
        expect(p.wifi_station_password).toBe("<set>");
    });

    it("rotation dropdown exposes the four cardinal angles + hydrates from settings", async () => {
        const container = document.createElement("div");
        mount(container, {
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
        mount(container, {
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
        mount(container, {
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
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave,
        });
        await tick();
        container.querySelector(".field-display-rotation").value = "270";
        container.querySelector(".settings-form").dispatchEvent(new Event("input", { bubbles: true }));
        await tick();
        expect(onSave.mock.calls[0][0].display_rotation).toBe(270);
    });

    it("hydrates Tailscale fields + round-trips them to onSave", async () => {
        // qarl 2026-05-12 arc 4: the auth-key secret-field UI was
        // replaced by a URL-auth flow (Enable button + status pill +
        // auth-URL inline display). The wire-shape tailscale_auth_key
        // field is preserved as a hidden input for back-compat — it
        // round-trips the redacted sentinel through PUT so the
        // backend's secret-substitution doesn't clobber an existing
        // key. Hostname is now readonly (pinned to device_id per a2
        // semantics).
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mount(container, {
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
        // Hidden input carries the redacted wire value.
        expect(container.querySelector(".field-tailscale-auth-key").value).toBe("<set>");
        // arc 4: Enable button + state pill + auth box wired into DOM.
        expect(container.querySelector(".field-tailscale-enable-btn")).not.toBeNull();
        expect(container.querySelector(".field-tailscale-state")).not.toBeNull();
        const authBox = container.querySelector(".field-tailscale-auth");
        // Auth-URL box is hidden until the operator clicks Enable.
        expect(authBox.hidden).toBe(true);

        container.querySelector(".settings-form").dispatchEvent(new Event("input", { bubbles: true }));
        await tick();
        const payload = onSave.mock.calls[0][0];
        expect(payload.tailscale_enabled).toBe(true);
        expect(payload.tailscale_hostname).toBe("lobby-sign-01");
        // PUT body echoes the sentinel intact.
        expect(payload.tailscale_auth_key).toBe("<set>");
    });

    it("sends Tailscale hostname + key as null when cleared", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave,
        });
        await tick();

        container.querySelector(".field-tailscale-hostname").value = "  ";
        container.querySelector(".field-tailscale-auth-key").value = "";
        container.querySelector(".settings-form").dispatchEvent(new Event("input", { bubbles: true }));
        await tick();

        const payload = onSave.mock.calls[0][0];
        expect(payload.tailscale_hostname).toBeNull();
        expect(payload.tailscale_auth_key).toBeNull();
    });

    // --- Batch 20.4: secret-field UI (Change… inline form) ---

    it("renders 'Set' indicator when GET returns the <set> sentinel", async () => {
        // arc 4 (qarl 2026-05-12): Tailscale auth-key secret-field
        // UI removed. Only wifi-ap + wifi-station retain the
        // secret-field Change… affordance.
        const container = document.createElement("div");
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const apStatus = container.querySelector(
            '.secret-field[data-secret="wifi-ap-password"] .secret-status',
        );
        expect(apStatus.textContent).toMatch(/Set/i);
        // station starts null (per SAMPLE) -> "Not set"
        const stationStatus = container.querySelector(
            '.secret-field[data-secret="wifi-station-password"] .secret-status',
        );
        expect(stationStatus.textContent).toMatch(/Not set/i);
    });

    it("Change… reveals the inline current_password + new_value form", async () => {
        const container = document.createElement("div");
        mount(container, {
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
        mount(container, {
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
        mount(container, {
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
        mount(container, {
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
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave,
        });
        await tick();

        container.querySelector(".settings-form").dispatchEvent(new Event("input", { bubbles: true }));
        await tick();

        // arc-3: attachAutoSave's error format is "Couldn't save · <msg>"
        // (auto-save.js setStatus); replaces the prior submit-handler's
        // "Save failed: <msg>".
        expect(container.querySelector(".settings-status").textContent).toMatch(
            /Couldn't save.*backend rejected/,
        );
    });

    // --- populateWifiScan operator-visible failure (round 13) ---
    //
    // Regression-locks: catch path (apiFetch threw) AND !res.ok early-
    // return path (apiFetch resolved with a non-2xx). Both paths
    // pre-fix went silent; operator saw the "(type manually)" SSID
    // picker fallback with no idea WHY. Sibling of bg-picker's
    // statusEl pattern (7aeec9a).

    it("populateWifiScan surfaces network failure to .settings-wifi-status", async () => {
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValue(
            new Error("network drop"),
        );
        const container = document.createElement("div");
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        await tick();
        const status = container.querySelector(".settings-wifi-status");
        expect(status).not.toBeNull();
        expect(status.hidden).toBe(false);
        expect(status.textContent).toContain("WiFi scan unavailable");
        expect(status.textContent).toContain("network drop");
        fetchSpy.mockRestore();
    });

    it("populateWifiScan surfaces non-2xx HTTP responses too", async () => {
        // Covers the `if (!res.ok) return;` early-return path that
        // also went silent pre-fix. Real-world triggers: 401 auth-
        // required, 403 permission denied, 500 backend exception,
        // 503 wifi card unavailable.
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
            new Response("", { status: 503 }),
        );
        const container = document.createElement("div");
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        await tick();
        const status = container.querySelector(".settings-wifi-status");
        expect(status.hidden).toBe(false);
        expect(status.textContent).toContain("WiFi scan unavailable");
        expect(status.textContent).toContain("503");
        fetchSpy.mockRestore();
    });

    // --- tickNow timer lifecycle (round 11) ---
    //
    // Regression-locks the fix in this commit: pre-fix, mountSettings's
    // setInterval(tickNow, 1000) had no teardown path, and the defensive
    // `if (!nowValueEl) return;` at the top of tickNow was unreachable
    // because nowValueEl is captured-once + never reassigned.

    it("destroy() stops the tickNow timer (regression-locks the leak fix)", async () => {
        vi.useFakeTimers();
        try {
            const container = document.createElement("div");
            // Attach to document so nowValueEl.isConnected === true and
            // the post-fix `if (!nowValueEl?.isConnected) return;` check
            // doesn't no-op every tick.
            document.body.appendChild(container);
            const handle = mount(container, {
                fetchSettings: async () => SAMPLE,
                onSave: vi.fn(),
            });
            // Flush refresh()'s microtasks (await fetchSettings()).
            // advanceTimersByTimeAsync(0) processes the microtask queue
            // under fake timers.
            await vi.advanceTimersByTimeAsync(0);
            await vi.advanceTimersByTimeAsync(0);

            const nowValueEl = container.querySelector(
                '[data-field="device-now-value"]',
            );
            expect(nowValueEl).not.toBeNull();
            const setSpy = vi.spyOn(nowValueEl, "textContent", "set");

            // Advance well past one tick interval -- tickNow fires
            // at least once. Don't pin an exact count: under fake
            // timers the precise fire count can interact with the
            // mount's own internal queued timers in ways that aren't
            // the SUT here. The teardown assertion below is what
            // locks the fix.
            await vi.advanceTimersByTimeAsync(3500);
            const beforeStop = setSpy.mock.calls.length;
            expect(beforeStop).toBeGreaterThan(0);

            // Tear down. Subsequent timer advance must NOT trigger ticks.
            handle.destroy();
            await vi.advanceTimersByTimeAsync(3500);
            expect(setSpy.mock.calls.length).toBe(beforeStop);

            // Idempotency: calling destroy() again is a no-op (no throw).
            expect(() => handle.destroy()).not.toThrow();
        } finally {
            vi.useRealTimers();
        }
    });

    it("tickNow no-ops when its element is detached from the DOM", async () => {
        // Pins the `if (!nowValueEl?.isConnected) return;` semantic.
        // Before the fix this check was `if (!nowValueEl) return;`
        // which was unreachable (nowValueEl was captured once at
        // mount and never reassigned), so a detached element would
        // silently keep getting textContent writes forever.
        vi.useFakeTimers();
        try {
            const container = document.createElement("div");
            document.body.appendChild(container);  // attach
            const handle = mount(container, {
                fetchSettings: async () => SAMPLE,
                onSave: vi.fn(),
            });
            await vi.advanceTimersByTimeAsync(0);
            await vi.advanceTimersByTimeAsync(0);

            const nowValueEl = container.querySelector(
                '[data-field="device-now-value"]',
            );
            const setSpy = vi.spyOn(nowValueEl, "textContent", "set");

            // While connected: tick should write.
            await vi.advanceTimersByTimeAsync(1100);
            const beforeDetach = setSpy.mock.calls.length;
            expect(beforeDetach).toBeGreaterThanOrEqual(1);

            // Detach + advance: no further writes (no throw either).
            container.remove();
            expect(nowValueEl.isConnected).toBe(false);
            await vi.advanceTimersByTimeAsync(2200);
            expect(setSpy.mock.calls.length).toBe(beforeDetach);

            handle.destroy();
        } finally {
            vi.useRealTimers();
        }
    });

    // Perf-night r2 (2026-05-26): Diagnostics sub-section toggle.
    // Pins the wire contract between the Settings checkbox + main.js's
    // perf-overlay lifecycle listener (via the custom event +
    // localStorage). Two ends of the contract get separate coverage:
    // here we test that Settings WRITES the right state; perf-overlay.test.js
    // covers the overlay's READ side.

    it("perf-overlay toggle: initializes unchecked when localStorage is empty", async () => {
        try { localStorage.removeItem("om.perf.show"); } catch {}
        const container = document.createElement("div");
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        const checkbox = container.querySelector(".field-perf-overlay-toggle");
        expect(checkbox).not.toBeNull();
        expect(checkbox.checked).toBe(false);
    });

    it("perf-overlay toggle: initializes checked when localStorage flag is set", async () => {
        try { localStorage.setItem("om.perf.show", "1"); } catch {}
        try {
            const container = document.createElement("div");
            mount(container, {
                fetchSettings: async () => SAMPLE,
                onSave: vi.fn(),
            });
            await tick();
            const checkbox = container.querySelector(".field-perf-overlay-toggle");
            expect(checkbox.checked).toBe(true);
        } finally {
            try { localStorage.removeItem("om.perf.show"); } catch {}
        }
    });

    it("perf-overlay toggle: ON dispatches openmarquee:perf-overlay-toggle{enabled:true} + sets localStorage", async () => {
        try { localStorage.removeItem("om.perf.show"); } catch {}
        const container = document.createElement("div");
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();

        const events = [];
        const listener = (e) => events.push(e.detail);
        document.addEventListener("openmarquee:perf-overlay-toggle", listener);
        try {
            const checkbox = container.querySelector(".field-perf-overlay-toggle");
            checkbox.checked = true;
            checkbox.dispatchEvent(new Event("change", { bubbles: true }));
            expect(events).toEqual([{ enabled: true }]);
            expect(localStorage.getItem("om.perf.show")).toBe("1");
        } finally {
            document.removeEventListener("openmarquee:perf-overlay-toggle", listener);
            try { localStorage.removeItem("om.perf.show"); } catch {}
        }
    });

    it("perf-overlay toggle: OFF dispatches openmarquee:perf-overlay-toggle{enabled:false} + clears localStorage", async () => {
        try { localStorage.setItem("om.perf.show", "1"); } catch {}
        const container = document.createElement("div");
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();

        const events = [];
        const listener = (e) => events.push(e.detail);
        document.addEventListener("openmarquee:perf-overlay-toggle", listener);
        try {
            const checkbox = container.querySelector(".field-perf-overlay-toggle");
            // Was initialized checked from localStorage.
            expect(checkbox.checked).toBe(true);
            checkbox.checked = false;
            checkbox.dispatchEvent(new Event("change", { bubbles: true }));
            expect(events).toEqual([{ enabled: false }]);
            expect(localStorage.getItem("om.perf.show")).toBe(null);
        } finally {
            document.removeEventListener("openmarquee:perf-overlay-toggle", listener);
        }
    });

    // Perf-night r8 (r2.5 follow-up): histogram capture control
    // mounts inside the Diagnostics card.

    it("perf-histogram capture button mounts in the Diagnostics card", async () => {
        const container = document.createElement("div");
        mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        // The Diagnostics card carries both the perf-overlay toggle
        // AND the histogram capture control's slot.
        const card = container.querySelector(".settings-perf-overlay");
        expect(card).not.toBeNull();
        const slot = card.querySelector(".settings-perf-histogram-slot");
        expect(slot).not.toBeNull();
        // mountPerfHistogramControl populates the slot with the
        // perf-histogram root element.
        const histogramRoot = slot.querySelector("[data-perf-histogram]");
        expect(histogramRoot).not.toBeNull();
        // Button defaults to enabled + "Capture phase histogram".
        const btn = slot.querySelector("[data-perf-histogram-capture]");
        expect(btn).not.toBeNull();
        expect(btn.disabled).toBe(false);
        expect(btn.textContent).toBe("Capture phase histogram");
    });

    it("perf-histogram capture control destroys cleanly on Settings teardown", async () => {
        const container = document.createElement("div");
        const handle = mount(container, {
            fetchSettings: async () => SAMPLE,
            onSave: vi.fn(),
        });
        await tick();
        expect(container.querySelector("[data-perf-histogram]")).not.toBeNull();
        handle.destroy();
        expect(container.querySelector("[data-perf-histogram]")).toBeNull();
    });

    // r53: Tailscale HTTPS toggle exposure (closes r49 F078 +
    // project_https_phase_1_shipped 9-day backlog).
    it("hydrates tailscale_https_enabled checkbox from settings and serializes it on save", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mount(container, {
            fetchSettings: async () => ({
                ...SAMPLE,
                tailscale_enabled: true,
                tailscale_https_enabled: false,
            }),
            onSave,
        });
        await tick();
        const httpsBox = container.querySelector(
            ".field-tailscale-https-enabled",
        );
        expect(httpsBox).not.toBeNull();
        expect(httpsBox.checked).toBe(false);

        // Toggle on -> dirty -> save payload reflects the new value.
        httpsBox.checked = true;
        httpsBox.dispatchEvent(new Event("change", { bubbles: true }));
        await tick();
        const payload = onSave.mock.calls[0][0];
        expect(payload.tailscale_https_enabled).toBe(true);
    });

    it("hydrates tailscale_https_enabled as true when settings omits the key (model default)", async () => {
        // r53: SystemSettings.tailscale_https_enabled defaults True
        // on the model, so a legacy settings.json missing the key
        // should hydrate as checked. Implementation reads
        // `settings.tailscale_https_enabled !== false` so undefined +
        // null + true all hydrate true; only literal false hydrates false.
        const container = document.createElement("div");
        const { tailscale_https_enabled: _omit, ...legacy } = SAMPLE;
        mount(container, {
            fetchSettings: async () => legacy,
            onSave: vi.fn(),
        });
        await tick();
        const httpsBox = container.querySelector(
            ".field-tailscale-https-enabled",
        );
        expect(httpsBox.checked).toBe(true);
    });

    // Phase B2 (qarl handover 2026-07-03): multi-network Wi-Fi list.
    // On first load the imported profiles (qarl / NEBULA / admin on
    // the Jason device) show up; add form appends; remove drops; the
    // sentinel-preserved password roundtrips through save without
    // rotating the stored PSK.
    describe("Phase B2 wifi_networks section", () => {
        const SAMPLE_MULTI = {
            ...SAMPLE,
            wifi_networks: [
                { ssid: "NEBULA", password: "<set>", autoconnect: true, priority: 0 },
                { ssid: "qarl", password: "<set>", autoconnect: true, priority: 0 },
                { ssid: "admin", password: null, autoconnect: true, priority: 0 },
            ],
        };

        it("renders the adopted networks on first load", async () => {
            const container = document.createElement("div");
            mount(container, {
                fetchSettings: async () => SAMPLE_MULTI,
                onSave: vi.fn(),
            });
            await tick();
            const items = container.querySelectorAll(".field-wifi-networks-item");
            expect(items).toHaveLength(3);
            const ssids = Array.from(items).map(
                (li) => li.querySelector(".field-wifi-networks-item-ssid").textContent,
            );
            expect(ssids).toEqual(["NEBULA", "qarl", "admin"]);
        });

        it("shows masked '<set>' for passwords + 'no password' for open networks", async () => {
            const container = document.createElement("div");
            mount(container, {
                fetchSettings: async () => SAMPLE_MULTI,
                onSave: vi.fn(),
            });
            await tick();
            const passwords = Array.from(
                container.querySelectorAll(".field-wifi-networks-item-password"),
            ).map((el) => el.textContent);
            expect(passwords[0]).toBe("password: <set>");
            expect(passwords[1]).toBe("password: <set>");
            expect(passwords[2]).toBe("no password");
        });

        it("emits an empty-state hint when no networks are saved", async () => {
            const container = document.createElement("div");
            mount(container, {
                fetchSettings: async () => ({ ...SAMPLE, wifi_networks: [] }),
                onSave: vi.fn(),
            });
            await tick();
            const empty = container.querySelector(".field-wifi-networks-empty");
            expect(empty.hidden).toBe(false);
            const items = container.querySelectorAll(".field-wifi-networks-item");
            expect(items).toHaveLength(0);
        });

        it("Add-network form appends the entry + triggers a save with wifi_networks in the payload", async () => {
            const onSave = vi.fn().mockResolvedValue(undefined);
            const container = document.createElement("div");
            mount(container, {
                fetchSettings: async () => SAMPLE_MULTI,
                onSave,
            });
            await tick();
            const ssidInput = container.querySelector(".field-wifi-networks-add-ssid");
            const pwInput = container.querySelector(".field-wifi-networks-add-password");
            const btn = container.querySelector(".field-wifi-networks-add-btn");
            ssidInput.value = "GuestNet";
            pwInput.value = "guest-password-1234";
            btn.click();
            await tick();
            const items = container.querySelectorAll(".field-wifi-networks-item");
            expect(items).toHaveLength(4);
            const ssids = Array.from(items).map(
                (li) => li.querySelector(".field-wifi-networks-item-ssid").textContent,
            );
            expect(ssids).toContain("GuestNet");
            // Save fired at least once; the last call should carry
            // the new entry with the operator-typed plaintext PSK.
            expect(onSave).toHaveBeenCalled();
            const lastPayload = onSave.mock.calls.at(-1)[0];
            expect(lastPayload.wifi_networks).toEqual(
                expect.arrayContaining([
                    expect.objectContaining({
                        ssid: "GuestNet",
                        password: "guest-password-1234",
                    }),
                ]),
            );
        });

        it("Add-form rejects duplicate SSID without mutating the list", async () => {
            const onSave = vi.fn().mockResolvedValue(undefined);
            const container = document.createElement("div");
            mount(container, {
                fetchSettings: async () => SAMPLE_MULTI,
                onSave,
            });
            await tick();
            const ssidInput = container.querySelector(".field-wifi-networks-add-ssid");
            const btn = container.querySelector(".field-wifi-networks-add-btn");
            const statusEl = container.querySelector(".field-wifi-networks-add-status");
            ssidInput.value = "NEBULA";  // already in the list
            btn.click();
            await tick();
            const items = container.querySelectorAll(".field-wifi-networks-item");
            expect(items).toHaveLength(3);  // unchanged
            expect(statusEl.textContent).toMatch(/already in the list/);
        });

        it("Add-form rejects an over-cap SSID (>32 chars)", async () => {
            const container = document.createElement("div");
            mount(container, {
                fetchSettings: async () => SAMPLE_MULTI,
                onSave: vi.fn(),
            });
            await tick();
            const ssidInput = container.querySelector(".field-wifi-networks-add-ssid");
            const btn = container.querySelector(".field-wifi-networks-add-btn");
            const statusEl = container.querySelector(".field-wifi-networks-add-status");
            // Force past maxlength=32 by direct assignment (jsdom does
            // enforce maxlength on `.value` setter for input[text],
            // but not always for programmatic set). Ensure our JS
            // guard fires either way.
            const tooLong = "X".repeat(40);
            ssidInput.value = tooLong;
            btn.click();
            await tick();
            expect(container.querySelectorAll(".field-wifi-networks-item")).toHaveLength(3);
            expect(statusEl.textContent).toMatch(/32/);
        });

        it("Remove button drops the entry + triggers a save", async () => {
            const onSave = vi.fn().mockResolvedValue(undefined);
            const container = document.createElement("div");
            mount(container, {
                fetchSettings: async () => SAMPLE_MULTI,
                onSave,
            });
            await tick();
            const beforeItems = container.querySelectorAll(".field-wifi-networks-item");
            expect(beforeItems).toHaveLength(3);
            // Click the remove button on the first row (NEBULA).
            beforeItems[0]
                .querySelector(".field-wifi-networks-item-remove")
                .click();
            await tick();
            const afterItems = container.querySelectorAll(".field-wifi-networks-item");
            expect(afterItems).toHaveLength(2);
            const remaining = Array.from(afterItems).map(
                (li) => li.querySelector(".field-wifi-networks-item-ssid").textContent,
            );
            expect(remaining).toEqual(["qarl", "admin"]);
            expect(onSave).toHaveBeenCalled();
            const lastPayload = onSave.mock.calls.at(-1)[0];
            expect(lastPayload.wifi_networks.map((n) => n.ssid)).not.toContain("NEBULA");
        });

        it("save payload roundtrips sentinel '<set>' for existing entries so PSKs aren't rotated", async () => {
            const onSave = vi.fn().mockResolvedValue(undefined);
            const container = document.createElement("div");
            mount(container, {
                fetchSettings: async () => SAMPLE_MULTI,
                onSave,
            });
            await tick();
            // Kick a save by adding a fresh entry, then inspect the payload.
            const ssidInput = container.querySelector(".field-wifi-networks-add-ssid");
            const pwInput = container.querySelector(".field-wifi-networks-add-password");
            const btn = container.querySelector(".field-wifi-networks-add-btn");
            ssidInput.value = "Fresh";
            pwInput.value = "fresh-password-1234";
            btn.click();
            await tick();
            expect(onSave).toHaveBeenCalled();
            const lastPayload = onSave.mock.calls.at(-1)[0];
            const nebula = lastPayload.wifi_networks.find((n) => n.ssid === "NEBULA");
            expect(nebula).toBeDefined();
            // Sentinel is preserved verbatim so the backend keeps the
            // stored PSK — this is the QA-B1 secret-redaction contract.
            expect(nebula.password).toBe("<set>");
        });
    });

    describe("device restart (recovery A4)", () => {
        // Default fetch mock: any GET (e.g. /api/system/info on mount)
        // resolves to an empty-ok JSON stub; the restart POST is what
        // each test asserts on.
        function mockFetch(restartImpl) {
            return vi.spyOn(globalThis, "fetch").mockImplementation(
                async (url, init) => {
                    if (typeof url === "string" && url.includes("/api/system/restart")) {
                        return restartImpl(url, init);
                    }
                    return {
                        ok: true,
                        status: 200,
                        json: async () => ({}),
                    };
                },
            );
        }

        it("renders a Device card with a Restart button, hidden confirm", async () => {
            const container = document.createElement("div");
            mockFetch(() => ({ ok: true, status: 202, json: async () => ({}) }));
            mount(container, { fetchSettings: async () => SAMPLE, onSave: vi.fn() });
            await tick();
            expect(container.querySelector(".settings-device")).toBeTruthy();
            expect(container.querySelector(".device-restart-btn")).toBeTruthy();
            expect(container.querySelector(".device-restart-confirm").hidden).toBe(true);
        });

        it("Restart button reveals the inline confirm; Cancel restores it", async () => {
            const container = document.createElement("div");
            mockFetch(() => ({ ok: true, status: 202, json: async () => ({}) }));
            mount(container, { fetchSettings: async () => SAMPLE, onSave: vi.fn() });
            await tick();
            const btn = container.querySelector(".device-restart-btn");
            const confirmBox = container.querySelector(".device-restart-confirm");
            btn.click();
            expect(confirmBox.hidden).toBe(false);
            expect(btn.hidden).toBe(true);
            container.querySelector(".device-restart-cancel").click();
            expect(confirmBox.hidden).toBe(true);
            expect(btn.hidden).toBe(false);
        });

        it("confirming POSTs /api/system/restart and shows the reconnect hint", async () => {
            const container = document.createElement("div");
            const fetchSpy = mockFetch(() => ({
                ok: true,
                status: 202,
                json: async () => ({ status: "restarting", message: "back soon" }),
            }));
            mount(container, { fetchSettings: async () => SAMPLE, onSave: vi.fn() });
            await tick();
            container.querySelector(".device-restart-btn").click();
            container.querySelector(".device-restart-go").click();
            await tick();
            const restartCall = fetchSpy.mock.calls.find(
                ([u]) => typeof u === "string" && u.includes("/api/system/restart"),
            );
            expect(restartCall).toBeDefined();
            expect(restartCall[1].method).toBe("POST");
            expect(container.querySelector(".device-restart-status").textContent).toMatch(
                /reconnect/i,
            );
            expect(container.querySelector(".device-restart-confirm").hidden).toBe(true);
        });

        it("surfaces a 503 as an error and restores the confirm controls", async () => {
            const container = document.createElement("div");
            mockFetch(() => ({
                ok: false,
                status: 503,
                json: async () => ({ detail: "restart unavailable: netctl socket not found" }),
            }));
            mount(container, { fetchSettings: async () => SAMPLE, onSave: vi.fn() });
            await tick();
            container.querySelector(".device-restart-btn").click();
            container.querySelector(".device-restart-go").click();
            await tick();
            const status = container.querySelector(".device-restart-status");
            expect(status.textContent).toMatch(/restart failed/i);
            expect(status.textContent).toMatch(/unavailable/i);
            // Controls are re-enabled so the operator can retry.
            expect(container.querySelector(".device-restart-go").disabled).toBe(false);
        });
    });

    describe("device factory reset (recovery A3)", () => {
        function mockFetch(resetImpl) {
            return vi.spyOn(globalThis, "fetch").mockImplementation(
                async (url, init) => {
                    if (typeof url === "string" && url.includes("/api/system/factory-reset")) {
                        return resetImpl(url, init);
                    }
                    return { ok: true, status: 200, json: async () => ({}) };
                },
            );
        }

        async function mountWith(reset) {
            const container = document.createElement("div");
            const spy = mockFetch(reset);
            mount(container, { fetchSettings: async () => SAMPLE, onSave: vi.fn() });
            await tick();
            return { container, spy };
        }

        it("renders a factory-reset control with the Erase button disabled", async () => {
            const { container } = await mountWith(() => ({ ok: true, status: 202, json: async () => ({}) }));
            expect(container.querySelector(".device-factory-btn")).toBeTruthy();
            expect(container.querySelector(".device-factory-confirm").hidden).toBe(true);
            expect(container.querySelector(".device-factory-go").disabled).toBe(true);
        });

        it("Erase stays disabled until the exact phrase is typed", async () => {
            const { container } = await mountWith(() => ({ ok: true, status: 202, json: async () => ({}) }));
            container.querySelector(".device-factory-btn").click();
            const input = container.querySelector(".device-factory-input");
            const go = container.querySelector(".device-factory-go");
            input.value = "factory";
            input.dispatchEvent(new Event("input"));
            expect(go.disabled).toBe(true);
            input.value = "factory-reset";
            input.dispatchEvent(new Event("input"));
            expect(go.disabled).toBe(false);
        });

        it("confirming POSTs with the confirm token and shows a terminal status", async () => {
            const { container, spy } = await mountWith(() => ({
                ok: true,
                status: 202,
                json: async () => ({ status: "resetting" }),
            }));
            container.querySelector(".device-factory-btn").click();
            const input = container.querySelector(".device-factory-input");
            input.value = "factory-reset";
            input.dispatchEvent(new Event("input"));
            container.querySelector(".device-factory-go").click();
            await tick();
            const call = spy.mock.calls.find(
                ([u]) => typeof u === "string" && u.includes("/api/system/factory-reset"),
            );
            expect(call).toBeDefined();
            expect(call[1].method).toBe("POST");
            expect(JSON.parse(call[1].body)).toEqual({ confirm: "factory-reset" });
            expect(container.querySelector(".device-factory-status").textContent).toMatch(
                /erasing and restarting/i,
            );
        });

        it("surfaces a failure and restores the controls (disabled until re-typed)", async () => {
            const { container } = await mountWith(() => ({
                ok: false,
                status: 503,
                json: async () => ({ detail: "factory reset unavailable: netctl socket not found" }),
            }));
            container.querySelector(".device-factory-btn").click();
            const input = container.querySelector(".device-factory-input");
            input.value = "factory-reset";
            input.dispatchEvent(new Event("input"));
            container.querySelector(".device-factory-go").click();
            await tick();
            const status = container.querySelector(".device-factory-status");
            expect(status.textContent).toMatch(/factory reset failed/i);
            // reset() cleared the input, so Erase is disabled again.
            expect(container.querySelector(".device-factory-go").disabled).toBe(true);
        });
    });
});
