// Copy the @ffmpeg/core ESM assets into dist/vendor/ffmpeg-core/ so the
// ffmpeg.wasm spike page can load them from the device's own origin. These
// files are ~31 MB (mostly the wasm binary), which is exactly why the
// SD-image size matters — SYSTEM_SPEC §2.2 plans for them.
//
// Why ESM and not UMD: @ffmpeg/ffmpeg spawns a *module* worker, and the
// worker's core-load fallback uses `await import(coreURL)` when
// `importScripts` isn't available. UMD doesn't produce a module default
// export through dynamic import, so the worker would throw
// ERROR_IMPORT_FAILURE. ESM's explicit `export default createFFmpegCore`
// is the path that actually works.
//
// Run by `npm run build` after esbuild so the static dist/ is self-contained
// for deployment. The captive-portal device has no internet so vendoring
// is mandatory — no CDN fallback.

import { cpSync, mkdirSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, "..");
const src = path.join(projectRoot, "node_modules/@ffmpeg/core/dist/esm");
const dest = path.join(projectRoot, "dist/vendor/ffmpeg-core");

mkdirSync(dest, { recursive: true });

for (const file of readdirSync(src)) {
    const srcPath = path.join(src, file);
    const destPath = path.join(dest, file);
    cpSync(srcPath, destPath);
    const size = statSync(destPath).size;
    console.log(`copied ${file} (${(size / 1024 / 1024).toFixed(1)} MB)`);
}
