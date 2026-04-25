// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountSchedule } from "./schedule.js";

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
