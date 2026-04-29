import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(resolve(HERE, "..", "styles.css"), "utf8");

function ruleBody(selector) {
    // Match the selector at a rule boundary so `.slide-browser-tile-thumb`
    // doesn't bind to `.slide-browser-tile { ... }` first. We anchor to
    // either the start of a line or the previous closing brace.
    const re = new RegExp(
        `(?:^|[}\\n,])\\s*${selector.replace(/[.\-]/g, "\\$&")}\\s*\\{([^}]*)\\}`,
        "m",
    );
    const m = CSS.match(re);
    if (!m) throw new Error(`selector not found: ${selector}`);
    return m[1];
}

describe("thumb aspect-ratio rules use --device-aspect", () => {
    // Without this, slides/playlist/flock/pallet thumbs render at a fixed
    // 2/1 even when the operator has set display_rotation=90/270 (portrait)
    // or has a non-2:1 panel like a WS2812 strip. QA filed B1+B2 in the
    // 2026-04-29 batch when these regressed to landscape on a rotated panel.
    const SELECTORS = [
        ".slide-browser-tile-thumb",
        ".track-block-thumb-wrap",
        ".pallet-tile-thumb-wrap",
        ".om-peer-thumb",
    ];

    for (const sel of SELECTORS) {
        it(`${sel} threads --device-aspect`, () => {
            const body = ruleBody(sel);
            expect(body).toMatch(/aspect-ratio:\s*var\(--device-aspect/);
        });
    }
});
