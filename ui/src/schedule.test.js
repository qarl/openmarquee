// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountSchedule } from "./schedule.js";
import { mountSettings } from "./settings.js";

const DEFAULT_PLAYLIST_ID = "00000000-0000-4000-8000-000000000001";
const PL_LUNCH = "00000000-0000-4000-8000-000000000010";
const PL_WEEKEND = "00000000-0000-4000-8000-000000000011";
const PL_OPEN = "00000000-0000-4000-8000-000000000012";
const PL_FALLBACK = "00000000-0000-4000-8000-000000000013";
const PL_A = "00000000-0000-4000-8000-000000000014";
const PL_B = "00000000-0000-4000-8000-000000000015";
const PL_X = "00000000-0000-4000-8000-000000000016";

const DEFAULT_CHOICES = [
    { id: DEFAULT_PLAYLIST_ID, name: "default" },
    { id: PL_LUNCH, name: "lunch" },
    { id: PL_WEEKEND, name: "weekend" },
    { id: PL_OPEN, name: "open" },
    { id: PL_FALLBACK, name: "fallback" },
    { id: PL_A, name: "a-playlist" },
    { id: PL_B, name: "b-playlist" },
    { id: PL_X, name: "x-playlist" },
];

function defaultChoices() {
    return async () => DEFAULT_CHOICES;
}

afterEach(() => {
    vi.restoreAllMocks();
});

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

describe("mountSchedule", () => {
    it("renders default playlist select + add button + empty rules list on empty schedule", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({ rules: [], default_playlist_id: DEFAULT_PLAYLIST_ID }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        const defaultSelect = container.querySelector(".field-default-playlist");
        expect(defaultSelect.tagName).toBe("SELECT");
        expect(defaultSelect.value).toBe(DEFAULT_PLAYLIST_ID);
        expect(container.querySelectorAll(".schedule-rule")).toHaveLength(0);
        expect(container.querySelector(".schedule-add")).not.toBeNull();
        // Save button removed — auto-save handles persistence.
        expect(container.querySelector(".schedule-save")).toBeNull();
        expect(container.querySelector(".om-save-status")).not.toBeNull();
    });

    it("renders one li per existing rule with name + day checkboxes filled in", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "Lunch",
                        days: ["mon", "tue", "wed", "thu", "fri"],
                        start_time: "11:00",
                        end_time: "14:00",
                        playlist_id: PL_LUNCH,
                        enabled: true,
                    },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        const rules = container.querySelectorAll(".schedule-rule");
        expect(rules).toHaveLength(1);
        expect(rules[0].querySelector(".rule-name").value).toBe("Lunch");
        expect(rules[0].querySelector(".rule-start").value).toBe("11:00");
        expect(rules[0].querySelector(".rule-end").value).toBe("14:00");
        expect(rules[0].querySelector(".rule-playlist").value).toBe(PL_LUNCH);

        const checkedDays = Array.from(
            rules[0].querySelectorAll(".rule-day-input:checked"),
        ).map((cb) => cb.value);
        expect(checkedDays).toEqual(["mon", "tue", "wed", "thu", "fri"]);
    });

    it("Add rule appends an empty-ish rule to the list", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({ rules: [], default_playlist_id: DEFAULT_PLAYLIST_ID }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        container.querySelector(".schedule-add").click();
        await tick();
        const rules = container.querySelectorAll(".schedule-rule");
        expect(rules).toHaveLength(1);
        expect(rules[0].querySelector(".rule-name").value).toBe("New rule");
    });

    it("Remove on a rule removes it from the list", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "A",
                        days: ["mon"],
                        start_time: "08:00",
                        end_time: "17:00",
                        playlist_id: PL_A,
                        enabled: true,
                    },
                    {
                        name: "B",
                        days: ["tue"],
                        start_time: "08:00",
                        end_time: "17:00",
                        playlist_id: PL_B,
                        enabled: true,
                    },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        container
            .querySelector(".schedule-rule .rule-remove")
            .click(); // removes the first one

        const remaining = container.querySelectorAll(".schedule-rule");
        expect(remaining).toHaveLength(1);
        expect(remaining[0].querySelector(".rule-name").value).toBe("B");
    });

    it("auto-save invokes onSave with the schedule payload (id-keyed)", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        const handle = mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "Open",
                        days: ["mon", "fri"],
                        start_time: "09:00",
                        end_time: "17:00",
                        playlist_id: PL_OPEN,
                        enabled: true,
                    },
                ],
                default_playlist_id: PL_FALLBACK,
            }),
            onSave,
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        await handle.flushAutoSave();

        expect(onSave).toHaveBeenCalledOnce();
        const payload = onSave.mock.calls[0][0];
        expect(payload.default_playlist_id).toBe(PL_FALLBACK);
        expect(payload.rules).toHaveLength(1);
        expect(payload.rules[0]).toMatchObject({
            name: "Open",
            days: ["mon", "fri"],
            start_time: "09:00",
            end_time: "17:00",
            playlist_id: PL_OPEN,
            enabled: true,
        });
    });

    it("round-trips the persisted tz unchanged — UI no longer edits it", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        const handle = mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
                tz: "America/New_York",
            }),
            onSave,
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();
        expect(container.querySelector(".field-tz")).toBeNull();

        await handle.flushAutoSave();
        expect(onSave.mock.calls[0][0].tz).toBe("America/New_York");
    });

    it("round-trips unknown forward-compat fields (backend extra='allow')", async () => {
        // 15.2: backend Schedule has model_config = ConfigDict(extra="allow")
        // so a future zoned-evaluator field or downstream consumer add-on
        // round-trips through this UI. collectSchedule must preserve any
        // field on the loaded envelope that the UI doesn't explicitly
        // overwrite.
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        const handle = mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
                tz: null,
                experimental_zoned_evaluator: true,
                downstream_consumer_hint: { v: 1, source: "future-plugin" },
            }),
            onSave,
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        await handle.flushAutoSave();
        const payload = onSave.mock.calls[0][0];
        expect(payload.experimental_zoned_evaluator).toBe(true);
        expect(payload.downstream_consumer_hint).toEqual({
            v: 1,
            source: "future-plugin",
        });
        // And the explicitly-edited fields still come from the form, not
        // the loaded snapshot.
        expect(payload.rules).toEqual([]);
        expect(payload.default_playlist_id).toBe(DEFAULT_PLAYLIST_ID);
    });

    it("shows a ticking device-time display when fetchSettings is provided", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
            fetchSettings: async () => ({ timezone: "UTC" }),
        });
        await tick();
        const nowEl = container.querySelector('[data-field="now-value"]');
        expect(nowEl).not.toBeNull();
        expect(nowEl.textContent.length).toBeGreaterThan(0);
    });

    it("Auto-save error message surfaces in the status", async () => {
        const container = document.createElement("div");
        const handle = mountSchedule(container, {
            fetchSchedule: async () => ({ rules: [], default_playlist_id: DEFAULT_PLAYLIST_ID }),
            onSave: async () => {
                throw new Error("backend rejected");
            },
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        await handle.flushAutoSave();
        expect(container.querySelector(".schedule-status").textContent).toContain(
            "backend rejected",
        );
    });

    it("playlist fields are <select>s populated from fetchPlaylistChoices", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "Lunch",
                        days: ["mon"],
                        start_time: "11:00",
                        end_time: "14:00",
                        playlist_id: PL_LUNCH,
                        enabled: true,
                    },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        const ruleSelect = container.querySelector(".rule-playlist");
        expect(ruleSelect.tagName).toBe("SELECT");
        const ruleValues = Array.from(ruleSelect.options).map((o) => o.value);
        expect(ruleValues).toContain(DEFAULT_PLAYLIST_ID);
        expect(ruleValues).toContain(PL_LUNCH);
        expect(ruleSelect.value).toBe(PL_LUNCH);
        // Display name shown in the option label.
        const lunchOpt = Array.from(ruleSelect.options).find(
            (o) => o.value === PL_LUNCH,
        );
        expect(lunchOpt.textContent).toBe("lunch");

        const defaultSelect = container.querySelector(".field-default-playlist");
        expect(defaultSelect.tagName).toBe("SELECT");
        expect(defaultSelect.value).toBe(DEFAULT_PLAYLIST_ID);
    });

    it("preserves an unknown playlist_id by adding a '(missing)' option", async () => {
        const stalePlaylistId = "00000000-0000-4000-8000-0000000000ff";
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "Stale",
                        days: ["mon"],
                        start_time: "11:00",
                        end_time: "14:00",
                        playlist_id: stalePlaylistId,
                        enabled: true,
                    },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave: vi.fn(),
            fetchPlaylistChoices: async () => [
                { id: DEFAULT_PLAYLIST_ID, name: "default" },
                { id: PL_LUNCH, name: "lunch" },
            ],
        });
        await tick();

        const select = container.querySelector(".rule-playlist");
        const missing = Array.from(select.options).find(
            (o) => o.value === stalePlaylistId,
        );
        expect(missing).toBeDefined();
        expect(missing.textContent).toMatch(/missing/);
        expect(select.value).toBe(stalePlaylistId); // round-trip preserved
    });

    it("Enable/Disable all bulk buttons render with a visible border", async () => {
        // Bug B13 (qarl batch 2026-04-29): both buttons used
        // `om-btn ghost`, which strips the border and made them read as
        // text labels rather than tappable controls.
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();
        const enableBtn = container.querySelector(".schedule-enable-all");
        const disableBtn = container.querySelector(".schedule-disable-all");
        expect(enableBtn.classList.contains("ghost")).toBe(false);
        expect(disableBtn.classList.contains("ghost")).toBe(false);
    });

    it("Disable all flips every rule's enabled checkbox off", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    { name: "A", days: ["mon"], start_time: "08:00", end_time: "17:00", playlist_id: PL_A, enabled: true },
                    { name: "B", days: ["tue"], start_time: "09:00", end_time: "18:00", playlist_id: PL_B, enabled: true },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        container.querySelector(".schedule-disable-all").click();
        const checkboxes = container.querySelectorAll(".rule-enabled");
        expect(checkboxes).toHaveLength(2);
        for (const cb of checkboxes) {
            expect(cb.checked).toBe(false);
        }
    });

    it("Enable all flips every rule's enabled checkbox on", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    { name: "A", days: ["mon"], start_time: "08:00", end_time: "17:00", playlist_id: PL_A, enabled: false },
                    { name: "B", days: ["tue"], start_time: "09:00", end_time: "18:00", playlist_id: PL_B, enabled: false },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        container.querySelector(".schedule-enable-all").click();
        const checkboxes = container.querySelectorAll(".rule-enabled");
        for (const cb of checkboxes) {
            expect(cb.checked).toBe(true);
        }
    });

    it("Bulk toggle kicks auto-save with the new disabled state", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        const handle = mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    { name: "A", days: ["mon"], start_time: "08:00", end_time: "17:00", playlist_id: PL_A, enabled: true },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave,
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        container.querySelector(".schedule-disable-all").click();
        await handle.flushAutoSave();
        expect(onSave).toHaveBeenCalledOnce();
        expect(onSave.mock.calls[0][0].rules[0].enabled).toBe(false);
    });

    it("Bulk toggle with no rules shows a friendly status message", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({ rules: [], default_playlist_id: DEFAULT_PLAYLIST_ID }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        container.querySelector(".schedule-disable-all").click();
        expect(container.querySelector(".schedule-status").textContent).toMatch(
            /No rules to toggle/,
        );
    });

    it("blocks autosave + paints an error when a rule has zero days checked (regression: QA 2026-04-26 #02)", async () => {
        // The server's ScheduleRule.days has min_length=1 — posting an empty
        // array used to autosave through and bounce as a 422 with the raw
        // detail JSON dumped into the status pill, *also* losing any
        // unrelated rename in the same payload. We now refuse to fire the
        // network call and surface a per-field error instead.
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        const handle = mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "Lunch hour",
                        days: ["mon", "tue", "wed", "thu", "fri"],
                        start_time: "11:00",
                        end_time: "14:00",
                        playlist_id: PL_LUNCH,
                        enabled: true,
                    },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave,
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        // Uncheck every day on the rule — emulate the operator clearing
        // the fieldset before they've decided what they actually want.
        for (const cb of container.querySelectorAll(".rule-day-input")) {
            cb.checked = false;
        }
        container
            .querySelector(".rule-day-input")
            .dispatchEvent(new Event("change", { bubbles: true }));

        await handle.flushAutoSave();

        // No PUT fired — the form is known-invalid client-side.
        expect(onSave).not.toHaveBeenCalled();

        // Operator-readable error in the status pill, naming the rule.
        const status = container.querySelector(".schedule-status");
        expect(status.dataset.state).toBe("error");
        expect(status.textContent).toMatch(/at least one day/i);
        expect(status.textContent).toContain("Lunch hour");

        // Per-card outline so the operator can see which rule is wrong
        // without re-reading the status copy.
        const rule = container.querySelector(".schedule-rule");
        expect(rule.classList.contains("rule-invalid")).toBe(true);
        expect(rule.querySelector(".rule-days-wrap").classList.contains("invalid")).toBe(true);

        // Re-check a day → outline + error clear, autosave goes through.
        const monBox = container.querySelector('.rule-day-input[value="mon"]');
        monBox.checked = true;
        monBox.dispatchEvent(new Event("change", { bubbles: true }));
        await handle.flushAutoSave();
        expect(onSave).toHaveBeenCalledOnce();
        expect(rule.classList.contains("rule-invalid")).toBe(false);
    });

    it("blocks autosave + paints an error when start_time is malformed (regression: QA 2026-04-26 #03)", async () => {
        // The server's ScheduleRule.start_time enforces HH:MM in 00:00-23:59.
        // The HTML5 pattern attribute on the text input only fires on form
        // submit, so the autosave path used to send `99:99` straight to the
        // server, return 422, and dump the raw detail JSON into the status
        // pill. Now we check `input.checkValidity()` client-side first.
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        const handle = mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "Lunch",
                        days: ["mon"],
                        start_time: "11:00",
                        end_time: "14:00",
                        playlist_id: PL_LUNCH,
                        enabled: true,
                    },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave,
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        const startInput = container.querySelector(".rule-start");
        startInput.value = "99:99";
        startInput.dispatchEvent(new Event("input", { bubbles: true }));

        await handle.flushAutoSave();

        expect(onSave).not.toHaveBeenCalled();
        const status = container.querySelector(".schedule-status");
        expect(status.dataset.state).toBe("error");
        expect(status.textContent).toMatch(/start time/i);
        expect(status.textContent).toContain("Lunch");

        // Field- + card-level error styling.
        expect(startInput.classList.contains("input-error")).toBe(true);
        const rule = container.querySelector(".schedule-rule");
        expect(rule.classList.contains("rule-invalid")).toBe(true);

        // Fix the value → autosave goes through, styling clears.
        startInput.value = "09:30";
        startInput.dispatchEvent(new Event("input", { bubbles: true }));
        await handle.flushAutoSave();
        expect(onSave).toHaveBeenCalledOnce();
        expect(startInput.classList.contains("input-error")).toBe(false);
        expect(rule.classList.contains("rule-invalid")).toBe(false);
    });

    it("blocks autosave + paints an error when end_time is malformed, but accepts 24:00 (regression: QA 2026-04-26 #03)", async () => {
        // end_time has the same 422 problem as start_time, but with the
        // extra wrinkle that 24:00 is a valid end-of-day idiom (handled by
        // the pattern's |24:00 alternative). Both bad-input rejection and
        // 24:00 acceptance are pinned here.
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        const handle = mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "Open",
                        days: ["mon", "tue", "wed", "thu", "fri"],
                        start_time: "09:00",
                        end_time: "17:00",
                        playlist_id: PL_OPEN,
                        enabled: true,
                    },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave,
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        const endInput = container.querySelector(".rule-end");

        // Bad value rejected.
        endInput.value = "25:00";
        endInput.dispatchEvent(new Event("input", { bubbles: true }));
        await handle.flushAutoSave();
        expect(onSave).not.toHaveBeenCalled();
        expect(endInput.classList.contains("input-error")).toBe(true);

        // 24:00 (end-of-day) accepted — autosave goes through.
        endInput.value = "24:00";
        endInput.dispatchEvent(new Event("input", { bubbles: true }));
        await handle.flushAutoSave();
        expect(onSave).toHaveBeenCalledOnce();
        expect(onSave.mock.calls[0][0].rules[0].end_time).toBe("24:00");
        expect(endInput.classList.contains("input-error")).toBe(false);
    });

    it("surfaces the 'add another playlist first' hint when only the default exists, hides it otherwise (regression: QA 2026-04-26 #04)", async () => {
        // On a fresh device only the seeded `default` playlist exists.
        // Configuring a schedule rule that points at `default` is a no-op
        // (the no-rule fallback also plays default). The hint nudges the
        // operator to the Playlists page before they spend time in here.
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({ rules: [], default_playlist_id: DEFAULT_PLAYLIST_ID }),
            onSave: vi.fn(),
            fetchPlaylistChoices: async () => [
                { id: DEFAULT_PLAYLIST_ID, name: "default" },
            ],
        });
        await tick();
        const hint = container.querySelector(".schedule-empty-hint");
        expect(hint).not.toBeNull();
        expect(hint.hidden).toBe(false);
        expect(hint.textContent).toMatch(/at least two playlists/i);

        // Deep-link into the Playlists page so the operator can act
        // without hunting for the nav.
        expect(hint.querySelector("a").getAttribute("href")).toBe("#/playlists");

        // Now mount with a second playlist available — hint stays hidden.
        const container2 = document.createElement("div");
        mountSchedule(container2, {
            fetchSchedule: async () => ({ rules: [], default_playlist_id: DEFAULT_PLAYLIST_ID }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(), // 8 entries
        });
        await tick();
        const hint2 = container2.querySelector(".schedule-empty-hint");
        expect(hint2.hidden).toBe(true);
    });

    it("schedule + settings each carry a distinct data-field on their 'now' eyebrow (regression: QA 2026-04-26 #05)", async () => {
        // Both panels render a current-time eyebrow. They used to share
        // `data-field="now"` (and `data-field="now-value"` on the inner
        // span), which made `document.querySelectorAll('[data-field="now"]')`
        // return two unrelated elements. We now namespace settings's to
        // `device-now` / `device-now-value` so the global selector is
        // unambiguous and the schedule panel owns the unqualified name.
        document.body.innerHTML = "";
        const scheduleHost = document.createElement("div");
        const settingsHost = document.createElement("div");
        document.body.appendChild(scheduleHost);
        document.body.appendChild(settingsHost);
        mountSchedule(scheduleHost, {
            fetchSchedule: async () => ({ rules: [], default_playlist_id: DEFAULT_PLAYLIST_ID }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        mountSettings(settingsHost, {
            fetchSettings: async () => ({
                schema_version: 1,
                sign_name: "x", output_mode: "hdmi",
                display_width: 1920, display_height: 1080, display_rotation: 0,
                brightness: 80, gamma: 2.2,
                wifi_ap_enabled: true, wifi_ssid: "openMarquee-AAA", wifi_password: "openmarquee",
                wifi_station_enabled: false, wifi_station_ssid: null, wifi_station_password: null,
                timezone: null, tailscale_enabled: false, tailscale_hostname: null, tailscale_auth_key: null,
                ui_first_run_seen: true, flock_sync_enabled: true, ws281x_pixel_order: "row_major",
            }),
            onSave: vi.fn(),
        });
        await tick();

        // Exactly one element each at the document level.
        expect(document.querySelectorAll('[data-field="now"]')).toHaveLength(1);
        expect(document.querySelectorAll('[data-field="now-value"]')).toHaveLength(1);
        expect(document.querySelectorAll('[data-field="device-now"]')).toHaveLength(1);
        expect(document.querySelectorAll('[data-field="device-now-value"]')).toHaveLength(1);

        // And they live in the panel that named them.
        expect(scheduleHost.querySelector('[data-field="now"]')).not.toBeNull();
        expect(settingsHost.querySelector('[data-field="device-now"]')).not.toBeNull();

        document.body.innerHTML = "";
    });

    it("escapes html in pre-existing rule names so injected markup can't render", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: '<img src=x onerror="alert(1)">',
                        days: ["mon"],
                        start_time: "08:00",
                        end_time: "17:00",
                        playlist_id: PL_X,
                        enabled: true,
                    },
                ],
                default_playlist_id: DEFAULT_PLAYLIST_ID,
            }),
            onSave: vi.fn(),
            fetchPlaylistChoices: defaultChoices(),
        });
        await tick();

        const ruleName = container.querySelector(".rule-name");
        expect(ruleName.value).toBe('<img src=x onerror="alert(1)">');
        expect(container.querySelector(".schedule-rule img")).toBeNull();
    });
});
