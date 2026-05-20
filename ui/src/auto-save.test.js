// @vitest-environment jsdom
//
// Tests for attachAutoSave's debounce / state machine. Covers
// the documented surface: input/change events trigger debounced
// save; canSave gates silently; validate() bails with status
// message; in-flight coalescing re-enqueues; flush() drains
// pending+in-flight; cancel() drops without firing.
//
// State surfaces (FYS bug 6 — saving is implicit, no success/in-
// progress copy):
//   * status.dataset.state -> "saving" / "saved" / "error" / "idle"
//   * status.textContent  -> the error message on "error", "" otherwise
//   * DEFAULT_DEBOUNCE_MS = 600

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { attachAutoSave } from "./auto-save.js";

function makeForm() {
    const form = document.createElement("form");
    document.body.appendChild(form);
    return form;
}

function makeStatus() {
    const el = document.createElement("span");
    document.body.appendChild(el);
    return el;
}

beforeEach(() => {
    vi.useFakeTimers();
});

afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
});

describe("attachAutoSave -- debounce", () => {
    it("save() fires after the default debounce on input event", async () => {
        const form = makeForm();
        const save = vi.fn().mockResolvedValue(undefined);
        attachAutoSave(form, { save });

        form.dispatchEvent(new Event("input", { bubbles: true }));
        expect(save).not.toHaveBeenCalled();

        // Default debounce is 600 ms; advance just past it.
        await vi.advanceTimersByTimeAsync(650);
        expect(save).toHaveBeenCalledTimes(1);
    });

    it("rapid edits coalesce into a single save", async () => {
        const form = makeForm();
        const save = vi.fn().mockResolvedValue(undefined);
        attachAutoSave(form, { save, debounceMs: 100 });

        // Fire 5 events within the debounce window.
        for (let i = 0; i < 5; i++) {
            form.dispatchEvent(new Event("input", { bubbles: true }));
            await vi.advanceTimersByTimeAsync(20);  // total 100ms by end
        }
        expect(save).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(150);
        expect(save).toHaveBeenCalledTimes(1);
    });

    it("change event also schedules a save", async () => {
        const form = makeForm();
        const save = vi.fn().mockResolvedValue(undefined);
        attachAutoSave(form, { save, debounceMs: 50 });

        form.dispatchEvent(new Event("change", { bubbles: true }));
        await vi.advanceTimersByTimeAsync(80);
        expect(save).toHaveBeenCalledTimes(1);
    });

    it("respects custom debounceMs", async () => {
        const form = makeForm();
        const save = vi.fn().mockResolvedValue(undefined);
        attachAutoSave(form, { save, debounceMs: 1000 });

        form.dispatchEvent(new Event("input"));
        await vi.advanceTimersByTimeAsync(500);
        expect(save).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(600);
        expect(save).toHaveBeenCalledTimes(1);
    });
});

describe("attachAutoSave -- canSave / validate gates", () => {
    it("canSave=false silently suppresses (no save, no status change)", async () => {
        const form = makeForm();
        const status = makeStatus();
        const save = vi.fn().mockResolvedValue(undefined);
        attachAutoSave(form, {
            save,
            status,
            canSave: () => false,
            debounceMs: 50,
        });

        form.dispatchEvent(new Event("input"));
        await vi.advanceTimersByTimeAsync(80);
        expect(save).not.toHaveBeenCalled();
        // Status should NOT have switched to "saving" because the
        // gate fired before the network call.
        expect(status.dataset.state).toBeUndefined();
        expect(status.textContent).toBe("");
    });

    it("validate() returning a message paints error + skips save", async () => {
        const form = makeForm();
        const status = makeStatus();
        const save = vi.fn().mockResolvedValue(undefined);
        attachAutoSave(form, {
            save,
            status,
            validate: () => "Name is required",
            debounceMs: 50,
        });

        form.dispatchEvent(new Event("input"));
        await vi.advanceTimersByTimeAsync(80);
        expect(save).not.toHaveBeenCalled();
        expect(status.dataset.state).toBe("error");
        expect(status.textContent).toBe("Name is required");
    });

    it("validate() returning null lets save through", async () => {
        const form = makeForm();
        const save = vi.fn().mockResolvedValue(undefined);
        attachAutoSave(form, {
            save,
            validate: () => null,
            debounceMs: 50,
        });

        form.dispatchEvent(new Event("input"));
        await vi.advanceTimersByTimeAsync(80);
        expect(save).toHaveBeenCalledTimes(1);
    });
});

describe("attachAutoSave -- status transitions", () => {
    it("cycles data-state 'saving' -> 'saved' with NO visible copy (FYS bug 6)", async () => {
        const form = makeForm();
        const status = makeStatus();
        let resolveSave;
        const save = vi.fn(() => new Promise(r => { resolveSave = r; }));
        attachAutoSave(form, { save, status, debounceMs: 50 });

        form.dispatchEvent(new Event("input"));
        await vi.advanceTimersByTimeAsync(80);
        // saving is implicit: data-state cycles, but no "Saving…" copy.
        expect(status.dataset.state).toBe("saving");
        expect(status.textContent).toBe("");

        resolveSave();
        await vi.advanceTimersByTimeAsync(0);
        // success: data-state="saved" but no "Saved" confirmation copy.
        expect(status.dataset.state).toBe("saved");
        expect(status.textContent).toBe("");
    });

    it("error path paints \"Couldn't save · <msg>\" with message", async () => {
        const form = makeForm();
        const status = makeStatus();
        const save = vi.fn().mockRejectedValue(new Error("network down"));
        attachAutoSave(form, { save, status, debounceMs: 50 });

        form.dispatchEvent(new Event("input"));
        await vi.advanceTimersByTimeAsync(80);
        // Let the rejected promise settle.
        await vi.advanceTimersByTimeAsync(0);
        expect(status.dataset.state).toBe("error");
        expect(status.textContent).toBe("Couldn't save · network down");
    });
});

describe("attachAutoSave -- coalescing in-flight saves", () => {
    it("event during in-flight save re-enqueues a single follow-up", async () => {
        const form = makeForm();
        let resolveFirst;
        const save = vi.fn()
            .mockImplementationOnce(() => new Promise(r => { resolveFirst = r; }))
            .mockResolvedValueOnce(undefined);
        attachAutoSave(form, { save, debounceMs: 50 });

        // Fire first save -> in-flight.
        form.dispatchEvent(new Event("input"));
        await vi.advanceTimersByTimeAsync(80);
        expect(save).toHaveBeenCalledTimes(1);

        // While in-flight, fire ANOTHER input. Should be coalesced
        // into a single follow-up (not multiple).
        form.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("input"));
        // Resolve the first save; finally-block should re-schedule one follow-up.
        resolveFirst();
        await vi.advanceTimersByTimeAsync(80);
        // 1 first + 1 follow-up = 2 total (NOT 4).
        expect(save).toHaveBeenCalledTimes(2);
    });
});

describe("attachAutoSave -- handle methods", () => {
    it("cancel() drops pending timer without firing save", async () => {
        const form = makeForm();
        const save = vi.fn().mockResolvedValue(undefined);
        const handle = attachAutoSave(form, { save, debounceMs: 100 });

        form.dispatchEvent(new Event("input"));
        await vi.advanceTimersByTimeAsync(50);  // halfway through debounce
        handle.cancel();
        await vi.advanceTimersByTimeAsync(200);
        expect(save).not.toHaveBeenCalled();
    });

    it("kick() schedules a save without an event", async () => {
        // null form path: caller drives saves via handle.kick() only.
        const save = vi.fn().mockResolvedValue(undefined);
        const handle = attachAutoSave(null, { save, debounceMs: 50 });

        handle.kick();
        await vi.advanceTimersByTimeAsync(80);
        expect(save).toHaveBeenCalledTimes(1);
    });

    it("flush() drains pending save immediately", async () => {
        const form = makeForm();
        const save = vi.fn().mockResolvedValue(undefined);
        const handle = attachAutoSave(form, { save, debounceMs: 1000 });

        form.dispatchEvent(new Event("input"));
        // Before debounce fires, flush.
        const flushPromise = handle.flush();
        await vi.advanceTimersByTimeAsync(0);
        await flushPromise;
        expect(save).toHaveBeenCalledTimes(1);
    });

    it("flush() drains follow-up coalesced during in-flight save", async () => {
        const form = makeForm();
        let resolveFirst;
        const save = vi.fn()
            .mockImplementationOnce(() => new Promise(r => { resolveFirst = r; }))
            .mockResolvedValueOnce(undefined);
        const handle = attachAutoSave(form, { save, debounceMs: 50 });

        form.dispatchEvent(new Event("input"));
        await vi.advanceTimersByTimeAsync(80);
        // First save in-flight. Fire a second event during in-flight
        // and let its debounce expire so attempt() runs again and sets
        // pending=true (the coalesce path the flush docstring calls
        // out).
        form.dispatchEvent(new Event("input"));
        await vi.advanceTimersByTimeAsync(80);
        expect(save).toHaveBeenCalledTimes(1);  // first still in-flight

        const flushPromise = handle.flush();
        // Resolve the first save so flush's await inFlight unblocks;
        // the finally re-schedules and the loop drains it.
        resolveFirst();
        await vi.advanceTimersByTimeAsync(200);
        await flushPromise;
        expect(save).toHaveBeenCalledTimes(2);
    });
});
