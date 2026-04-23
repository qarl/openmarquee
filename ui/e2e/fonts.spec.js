// Golden-master screenshots for the bundled display fonts.
//
// For every font declared in `FONT_FAMILIES` (editor.js), drive the text-
// slide editor, pick that font, wait for the `@font-face` TTF to finish
// loading, and compare the canvas against a reference image stored under
// `fonts.spec.js-snapshots/`. If a regression ever swaps a font silently
// for a serif fallback (the exact bug we hit during implementation), this
// catches it at CI time instead of on a demo.
//
// First run generates the goldens via `--update-snapshots`; committed
// alongside the spec so every checkout compares against the same bytes.

import { expect, test } from "@playwright/test";
import { FONT_FAMILIES } from "../src/editor.js";

// Only the bundled @font-face families — the three system generics have
// no deterministic rendering across machines (they resolve to whatever
// the OS has installed).
const BUNDLED_FONTS = FONT_FAMILIES
    .filter((f) => !["sans-serif", "serif", "monospace"].includes(f.value))
    .map((f) => [f.value, f.weight]);

const SAMPLE_TEXT = "The Quick Sign";

test.describe("bundled font rendering", () => {
    for (const [family, weight] of BUNDLED_FONTS) {
        const slug = family.toLowerCase().replace(/\s+/g, "-");
        test(`renders ${family} on the text-slide canvas`, async ({ page }) => {
            await page.goto("/#/slides/text");
            await page.waitForSelector(".field-font-family");

            await page.fill(".field-text", SAMPLE_TEXT);
            await page.selectOption(".field-font-family", family);

            // Ensure the @font-face TTF is fully loaded before snapshot.
            await page.evaluate(
                async ({ f, w }) => {
                    await document.fonts.load(`${w} 40px "${f}"`);
                },
                { f: family, w: weight },
            );
            // Re-dispatch an input event on the text field so the canvas
            // redraws with the now-loaded font (selectOption doesn't
            // trigger the text listener; dispatching on text does).
            await page.evaluate(() => {
                document
                    .querySelector(".field-text")
                    .dispatchEvent(new Event("input", { bubbles: true }));
            });
            await page.waitForTimeout(100);

            const canvas = page.locator(".editor-canvas").first();
            await expect(canvas).toHaveScreenshot(`font-${slug}.png`, {
                // A few pixels of hinting / antialiasing wiggle is fine;
                // a fallback to serif blows past this threshold trivially.
                maxDiffPixelRatio: 0.005,
            });
        });
    }
});
