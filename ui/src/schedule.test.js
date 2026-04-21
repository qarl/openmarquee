// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountSchedule } from "./schedule.js";

afterEach(() => {
    vi.restoreAllMocks();
});

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

describe("mountSchedule", () => {
    it("renders default playlist input + add/save buttons + empty rules list on empty schedule", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({ rules: [], default_playlist_name: "default" }),
            onSave: vi.fn(),
        });
        await tick();

        expect(container.querySelector(".field-default-playlist").value).toBe("default");
        expect(container.querySelectorAll(".schedule-rule")).toHaveLength(0);
        expect(container.querySelector(".schedule-add")).not.toBeNull();
        expect(container.querySelector(".schedule-save")).not.toBeNull();
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
                        playlist_name: "lunch",
                        enabled: true,
                    },
                ],
                default_playlist_name: "default",
            }),
            onSave: vi.fn(),
        });
        await tick();

        const rules = container.querySelectorAll(".schedule-rule");
        expect(rules).toHaveLength(1);
        expect(rules[0].querySelector(".rule-name").value).toBe("Lunch");
        expect(rules[0].querySelector(".rule-start").value).toBe("11:00");
        expect(rules[0].querySelector(".rule-end").value).toBe("14:00");
        expect(rules[0].querySelector(".rule-playlist").value).toBe("lunch");

        const checkedDays = Array.from(
            rules[0].querySelectorAll(".rule-day-input:checked"),
        ).map((cb) => cb.value);
        expect(checkedDays).toEqual(["mon", "tue", "wed", "thu", "fri"]);
    });

    it("Add rule appends an empty-ish rule to the list", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({ rules: [], default_playlist_name: "default" }),
            onSave: vi.fn(),
        });
        await tick();

        container.querySelector(".schedule-add").click();
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
                        playlist_name: "a",
                        enabled: true,
                    },
                    {
                        name: "B",
                        days: ["tue"],
                        start_time: "08:00",
                        end_time: "17:00",
                        playlist_name: "b",
                        enabled: true,
                    },
                ],
                default_playlist_name: "default",
            }),
            onSave: vi.fn(),
        });
        await tick();

        container
            .querySelector(".schedule-rule .rule-remove")
            .click(); // removes the first one

        const remaining = container.querySelectorAll(".schedule-rule");
        expect(remaining).toHaveLength(1);
        expect(remaining[0].querySelector(".rule-name").value).toBe("B");
    });

    it("Save invokes onSave with the schedule payload", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "Open",
                        days: ["mon", "fri"],
                        start_time: "09:00",
                        end_time: "17:00",
                        playlist_name: "open",
                        enabled: true,
                    },
                ],
                default_playlist_name: "fallback",
            }),
            onSave,
        });
        await tick();

        container.querySelector(".schedule-save").click();
        await tick();

        expect(onSave).toHaveBeenCalledOnce();
        const payload = onSave.mock.calls[0][0];
        expect(payload.default_playlist_name).toBe("fallback");
        expect(payload.rules).toHaveLength(1);
        expect(payload.rules[0]).toMatchObject({
            name: "Open",
            days: ["mon", "fri"],
            start_time: "09:00",
            end_time: "17:00",
            playlist_name: "open",
            enabled: true,
        });
    });

    it("loads and round-trips the optional tz field", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [],
                default_playlist_name: "default",
                tz: "America/New_York",
            }),
            onSave,
        });
        await tick();

        // Initial value populated from the schedule.
        expect(container.querySelector(".field-tz").value).toBe("America/New_York");

        // User clears it; save should send tz=null.
        container.querySelector(".field-tz").value = "";
        container.querySelector(".schedule-save").click();
        await tick();
        expect(onSave.mock.calls[0][0].tz).toBeNull();
    });

    it("Save error message surfaces in the status", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({ rules: [], default_playlist_name: "default" }),
            onSave: async () => {
                throw new Error("backend rejected");
            },
        });
        await tick();

        container.querySelector(".schedule-save").click();
        await tick();
        expect(container.querySelector(".schedule-status").textContent).toContain(
            "backend rejected",
        );
    });

    it("when fetchPlaylistNames is provided, playlist fields are <select>s populated from the list", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "Lunch",
                        days: ["mon"],
                        start_time: "11:00",
                        end_time: "14:00",
                        playlist_name: "lunch",
                        enabled: true,
                    },
                ],
                default_playlist_name: "default",
            }),
            onSave: vi.fn(),
            fetchPlaylistNames: async () => ["default", "lunch", "weekend"],
        });
        await tick();

        const ruleSelect = container.querySelector(".rule-playlist");
        expect(ruleSelect.tagName).toBe("SELECT");
        const values = Array.from(ruleSelect.options).map((o) => o.value);
        expect(values).toEqual(["default", "lunch", "weekend"]);
        expect(ruleSelect.value).toBe("lunch");

        const defaultSelect = container.querySelector(".field-default-playlist");
        expect(defaultSelect.tagName).toBe("SELECT");
        expect(defaultSelect.value).toBe("default");
    });

    it("preserves an unknown playlist_name by adding a '(missing)' option", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "Stale",
                        days: ["mon"],
                        start_time: "11:00",
                        end_time: "14:00",
                        playlist_name: "deleted_playlist",
                        enabled: true,
                    },
                ],
                default_playlist_name: "default",
            }),
            onSave: vi.fn(),
            fetchPlaylistNames: async () => ["default", "lunch"],
        });
        await tick();

        const select = container.querySelector(".rule-playlist");
        const missing = Array.from(select.options).find(
            (o) => o.value === "deleted_playlist",
        );
        expect(missing).toBeDefined();
        expect(missing.textContent).toMatch(/missing/);
        expect(select.value).toBe("deleted_playlist"); // round-trip preserved
    });

    it("when fetchPlaylistNames is omitted, playlist fields stay as text inputs (back-compat)", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    {
                        name: "x",
                        days: ["mon"],
                        start_time: "11:00",
                        end_time: "14:00",
                        playlist_name: "whatever",
                        enabled: true,
                    },
                ],
                default_playlist_name: "default",
            }),
            onSave: vi.fn(),
        });
        await tick();

        const el = container.querySelector(".rule-playlist");
        expect(el.tagName).toBe("INPUT");
    });

    it("Disable all flips every rule's enabled checkbox off", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    { name: "A", days: ["mon"], start_time: "08:00", end_time: "17:00", playlist_name: "a", enabled: true },
                    { name: "B", days: ["tue"], start_time: "09:00", end_time: "18:00", playlist_name: "b", enabled: true },
                ],
                default_playlist_name: "default",
            }),
            onSave: vi.fn(),
        });
        await tick();

        container.querySelector(".schedule-disable-all").click();
        const checkboxes = container.querySelectorAll(".rule-enabled");
        expect(checkboxes).toHaveLength(2);
        for (const cb of checkboxes) {
            expect(cb.checked).toBe(false);
        }
        expect(container.querySelector(".schedule-status").textContent).toMatch(
            /Disabled 2 rules.*Save/,
        );
    });

    it("Enable all flips every rule's enabled checkbox on", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    { name: "A", days: ["mon"], start_time: "08:00", end_time: "17:00", playlist_name: "a", enabled: false },
                    { name: "B", days: ["tue"], start_time: "09:00", end_time: "18:00", playlist_name: "b", enabled: false },
                ],
                default_playlist_name: "default",
            }),
            onSave: vi.fn(),
        });
        await tick();

        container.querySelector(".schedule-enable-all").click();
        const checkboxes = container.querySelectorAll(".rule-enabled");
        for (const cb of checkboxes) {
            expect(cb.checked).toBe(true);
        }
    });

    it("Bulk toggle is DOM-only — Save still needed to persist", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountSchedule(container, {
            fetchSchedule: async () => ({
                rules: [
                    { name: "A", days: ["mon"], start_time: "08:00", end_time: "17:00", playlist_name: "a", enabled: true },
                ],
                default_playlist_name: "default",
            }),
            onSave,
        });
        await tick();

        container.querySelector(".schedule-disable-all").click();
        // No save yet.
        expect(onSave).not.toHaveBeenCalled();

        // Save picks up the disabled state.
        container.querySelector(".schedule-save").click();
        await tick();
        expect(onSave).toHaveBeenCalledOnce();
        expect(onSave.mock.calls[0][0].rules[0].enabled).toBe(false);
    });

    it("Bulk toggle with no rules shows a friendly status message", async () => {
        const container = document.createElement("div");
        mountSchedule(container, {
            fetchSchedule: async () => ({ rules: [], default_playlist_name: "default" }),
            onSave: vi.fn(),
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
                        playlist_name: "x",
                        enabled: true,
                    },
                ],
                default_playlist_name: "default",
            }),
            onSave: vi.fn(),
        });
        await tick();

        // No nested <img> should appear inside the rule name input — the
        // payload is treated as text, not markup.
        const ruleName = container.querySelector(".rule-name");
        expect(ruleName.value).toBe('<img src=x onerror="alert(1)">');
        expect(container.querySelector(".schedule-rule img")).toBeNull();
    });
});
