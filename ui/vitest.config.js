import { defineConfig, configDefaults } from "vitest/config";
import { resolve } from "node:path";

export default defineConfig({
    test: {
        environment: "node",
        include: ["src/**/*.test.js"],
        // macOS NFS/SMB resource-fork artifacts (._foo.test.js) +
        // AppleDouble dirs land in ui/src/ whenever qarl's Mac
        // touches the share. Vitest's default `**` glob matches
        // them as test files; the binary content blows up at the
        // transform step. Extend the default exclude list rather
        // than replace it (configDefaults.exclude already covers
        // node_modules, dist, etc.).
        exclude: [
            ...configDefaults.exclude,
            "**/._*",
            "**/.AppleDouble/**",
        ],
        setupFiles: ["./vitest.setup.js"],
    },
    resolve: {
        // Phase 1b: rasterize.js + main.js both import wasm-renderer.js
        // which top-imports the .wasm binary via esbuild's
        // --loader:.wasm=binary. vitest uses vite's resolver and has
        // no binary loader, so the production module breaks under
        // jsdom. Redirect to a test stub that preserves the API
        // surface but reports isWasmReady=false so paintLayer falls
        // back to ctx.fillText — exactly what the existing 518 tests
        // already exercise.
        alias: {
            "./wasm-renderer.js": resolve(
                __dirname, "src/wasm-renderer.test-stub.js"),
        },
    },
});
