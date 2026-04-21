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
