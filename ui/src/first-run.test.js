// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountFirstRunWelcome } from "./first-run.js";

afterEach(() => vi.restoreAllMocks());

describe("mountFirstRunWelcome", () => {
    it("renders the classic welcome with sign-name footer + LIVE marker", () => {
        const container = document.createElement("div");
        mountFirstRunWelcome(container, {
            signName: "SignA7F",
            onContinue: vi.fn(),
        });
        // Hero text + the operator's sign-name appear.
        expect(container.textContent).toMatch(/Your sign is on\./);
        expect(container.textContent).toMatch(/SignA7F/);
        // Twin marquee strips for the marquee/scoreboard motif.
        expect(container.querySelectorAll(".om-marquee-strip")).toHaveLength(2);
        // CTA + LIVE pill + connected-to footer.
        expect(container.querySelector(".first-run-continue")).toBeTruthy();
        expect(container.textContent).toMatch(/● LIVE/);
        expect(container.textContent).toMatch(/SLIDE 01 \/ 01/);
    });

    it("calls onContinue when the CTA is tapped + disables the button while pending", async () => {
        const container = document.createElement("div");
        let resolveOnContinue;
        const onContinue = vi.fn(
            () => new Promise((r) => (resolveOnContinue = r)),
        );
        mountFirstRunWelcome(container, { signName: "SignABC", onContinue });

        const btn = container.querySelector(".first-run-continue");
        btn.click();
        // Sync part of the handler runs first — button disables.
        await Promise.resolve();
        expect(btn.disabled).toBe(true);
        expect(onContinue).toHaveBeenCalledTimes(1);

        // Resolve the promise → button STAYS disabled (the location.reload()
        // would have happened in the real flow). Here we just confirm the
        // handler ran and didn't throw.
        resolveOnContinue();
        await Promise.resolve();
        await Promise.resolve();
    });

    it("re-enables the button when onContinue rejects (operator retries)", async () => {
        const container = document.createElement("div");
        const onContinue = vi.fn().mockRejectedValue(new Error("network down"));
        mountFirstRunWelcome(container, { signName: "SignXYZ", onContinue });

        const btn = container.querySelector(".first-run-continue");
        btn.click();
        // Wait for the rejection to flow through.
        await new Promise((r) => setTimeout(r, 0));
        expect(btn.disabled).toBe(false);
    });
});
