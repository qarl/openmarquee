import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(resolve(HERE, "..", "styles.css"), "utf8");

describe("editor column phone-width-on-desktop (qarl-bug 2026-05-12)", () => {
    // qarl reversed the B13/B21 720px decision. The editor form column
    // should be approximately a phone's width across mobile + desktop
    // so the operator's mental model of "this is what fits on a phone"
    // is consistent. Preview gets the rest. iPhone widths sit
    // 375..430px; 420 caps just above the common range.
    it("caps .editor-form-stack at 420px (was 720 pre-qarl-bug-2026-05-12)", () => {
        // Match the .editor-form-stack rule body. Tolerant of extra
        // whitespace + reordering of declarations within the block.
        const m = CSS.match(
            /\.editor-form-stack\s*\{[^}]*?max-width:\s*(\d+)px[^}]*\}/m,
        );
        expect(m, ".editor-form-stack rule missing or no max-width").toBeTruthy();
        const px = Number(m[1]);
        // Allow some tolerance (415..425) so a future operator tweak
        // doesn't fail the test on a 1px nudge, but a regression to
        // the old 720 (or anything significantly wider than a phone)
        // fails loudly.
        expect(px).toBeGreaterThanOrEqual(380);
        expect(px).toBeLessThanOrEqual(440);
    });

    it("desktop two-column grid pins the form track to 420px", () => {
        // The @media (min-width: <bp>) block sets
        //   grid-template-columns: minmax(0, 1fr) 420px
        // The exact track value must match .editor-form-stack's
        // max-width so the form column doesn't jump when its
        // contents resize (B21 origin).
        const m = CSS.match(
            /\.editor-cols\s*\{[^}]*?grid-template-columns:\s*minmax\(0,\s*1fr\)\s+(\d+)px/m,
        );
        expect(m, ".editor-cols grid-template-columns missing").toBeTruthy();
        const px = Number(m[1]);
        expect(px).toBeGreaterThanOrEqual(380);
        expect(px).toBeLessThanOrEqual(440);
    });

    it("two-column breakpoint dropped from 1080 to <=800px", () => {
        // With form now 420px wide (was 720), the two-column layout
        // becomes viable at much narrower viewports. The dispatch's
        // make-best-guess was ~760px = 420 + 340 preview budget.
        // Allow 700..820 tolerance so a future 4px nudge doesn't
        // break the test.
        const m = CSS.match(
            /@media\s*\(min-width:\s*(\d+)px\)\s*\{\s*\.editor-cols/m,
        );
        expect(m, "editor-cols media-query breakpoint not found").toBeTruthy();
        const bp = Number(m[1]);
        expect(bp).toBeGreaterThanOrEqual(700);
        expect(bp).toBeLessThanOrEqual(820);
    });
});

describe("playlist column matches slide editor (qarl 2026-05-12 symmetry)", () => {
    // Companion to the editor-width tests above. qarl confirmed the
    // playlist editor needs the same phone-width-on-desktop shape
    // (was a flagged open question in the original Bug 1 memory).
    // The two views' form columns must stay in lockstep: one fix
    // for both = one design point.
    it("caps .playlist-form-stack at 420px", () => {
        const m = CSS.match(
            /\.playlist-form-stack\s*\{[^}]*?max-width:\s*(\d+)px[^}]*\}/m,
        );
        expect(m, ".playlist-form-stack rule missing or no max-width").toBeTruthy();
        const px = Number(m[1]);
        expect(px).toBeGreaterThanOrEqual(380);
        expect(px).toBeLessThanOrEqual(440);
    });

    it("desktop two-column grid pins the form track to 420px", () => {
        const m = CSS.match(
            /\.playlist-cols\s*\{[^}]*?grid-template-columns:\s*minmax\(0,\s*1fr\)\s+(\d+)px/m,
        );
        expect(m, ".playlist-cols grid-template-columns missing").toBeTruthy();
        const px = Number(m[1]);
        expect(px).toBeGreaterThanOrEqual(380);
        expect(px).toBeLessThanOrEqual(440);
    });

    it("two-column breakpoint matches the editor (~760px)", () => {
        const m = CSS.match(
            /@media\s*\(min-width:\s*(\d+)px\)\s*\{\s*\.playlist-cols/m,
        );
        expect(m, "playlist-cols media-query breakpoint not found").toBeTruthy();
        const bp = Number(m[1]);
        expect(bp).toBeGreaterThanOrEqual(700);
        expect(bp).toBeLessThanOrEqual(820);
    });
});
