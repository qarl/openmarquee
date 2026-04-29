import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(resolve(HERE, "..", "styles.css"), "utf8");

function ruleBody(selector) {
    const re = new RegExp(
        `(?:^|[}\\n,])\\s*${selector.replace(/[.\-]/g, "\\$&")}\\s*\\{([^}]*)\\}`,
        "m",
    );
    const m = CSS.match(re);
    if (!m) throw new Error(`selector not found: ${selector}`);
    return m[1];
}

describe("preview-window normalization (B3)", () => {
    // qarl 2026-04-29: stream-preview-wrap is the canonical preview size.
    // Slide editor canvas + playlist inline-preview must match its width
    // so the three previews read as the same chrome.
    // Each panel's preview is wrapped in a chrome element (border + radius
    // + LED bg). The wrap owns the size + chrome so the inner canvas just
    // paints. Editor's wrap is `.preview-wrap`; inline-preview's is
    // `.inline-preview-stage`; stream's is `.stream-preview-wrap`.
    const SELECTORS = [
        ".stream-preview-wrap",
        ".preview-wrap",
        ".inline-preview-stage",
    ];

    for (const sel of SELECTORS) {
        it(`${sel} caps width at 26rem`, () => {
            const body = ruleBody(sel);
            expect(body).toMatch(/max-width:\s*26rem/);
        });
    }
});
