#!/usr/bin/env node
// Smoke-test the renderer-wasm Phase 0 artifact via Node ESM.
//
// Loads renderer-wasm/pkg/renderer_wasm.js, instantiates the WASM
// module against the ui/fonts/vt323.ttf TTF, calls rasterize_text on
// a Boot-fixture-shaped input, and asserts:
//
//   - the call returns a non-null Uint8Array
//   - the header decodes to non-zero width + height
//   - at least one pixel has non-zero alpha (text was rendered)
//
// Run:
//   bash scripts/build_wasm_renderer.sh    # build pkg/ first
//   node  scripts/smoke_wasm_renderer.mjs
//
// Exits 0 on success, non-zero with a diagnostic on failure.
// Same shape as backend's pytest smokes — single self-contained file,
// no test framework, designed to be the first thing that fails when
// the WASM artifact is missing or broken.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..");
const PKG = resolve(REPO, "renderer-wasm/pkg/renderer_wasm.js");
const FONT = resolve(REPO, "ui/fonts/vt323.ttf");

const die = (msg) => { console.error(`FAIL: ${msg}`); process.exit(1); };

const mod = await import(PKG).catch((e) =>
    die(`could not import ${PKG}: ${e.message}\n` +
        `did you run scripts/build_wasm_renderer.sh first?`));

// wasm-bindgen --target web requires explicit init() with a path or
// URL to the .wasm. Node ESM gets it via readFileSync.
const wasmBytes = readFileSync(resolve(REPO, "renderer-wasm/pkg/renderer_wasm_bg.wasm"));
await mod.default({ module_or_path: wasmBytes });

const fontBytes = readFileSync(FONT);
const result = mod.rasterize_text("BOOT", fontBytes, 32.0, 255, 180, 60, 255);
if (!result) die("rasterize_text returned null for 'BOOT'");

const w = new DataView(result.buffer, result.byteOffset, 4).getUint32(0, true);
const h = new DataView(result.buffer, result.byteOffset + 4, 4).getUint32(0, true);
if (w === 0 || h === 0) die(`bitmap has zero dim: ${w}x${h}`);

const pixels = result.subarray(8);
const expected = w * h * 4;
if (pixels.length !== expected) {
    die(`pixel buffer wrong size: got ${pixels.length}, expected ${expected}`);
}

let inked = 0;
for (let i = 3; i < pixels.length; i += 4) {
    if (pixels[i] > 0) inked += 1;
}
if (inked === 0) die("no inked pixels — fontdue rendered an empty bitmap");

const totalPixels = w * h;
const inkPct = (100 * inked / totalPixels).toFixed(1);
console.log(`PASS: rasterized 'BOOT' at 32 px in VT323 → ${w}×${h} bitmap, ${inked}/${totalPixels} pixels inked (${inkPct} %)`);
