// Test stub for ui/src/wasm-renderer.js. vitest's resolve.alias
// redirects all `import "./wasm-renderer.js"` to THIS file under
// the test runner so jsdom doesn't choke on the .wasm import
// (vitest has no binary loader; esbuild does, but esbuild is the
// production bundler — vitest uses vite's resolver).
//
// The stub preserves the API surface so the paintLayer test paths
// can verify wire-shape correctness:
//   - initWasmRenderer / registerFont resolve immediately
//   - isWasmReady returns false by default so paintLayer falls
//     back to ctx.fillText (the path the existing 518 tests
//     expect; we want zero regression)
//   - Tests that WANT to exercise the WASM branch can monkey-patch
//     isWasmReady / isFontRegistered / rasterizeText to opt in
//
// This keeps the existing fillText-based test mocks valid while
// the production path uses the real wasm-renderer.js + esbuild's
// --loader:.wasm=binary.

export function initWasmRenderer() {
    return Promise.resolve();
}

export function isWasmReady() {
    return false;
}

export function registerFont(_name, _urlOrBytes) {
    return Promise.resolve(true);
}

export function isFontRegistered(_name) {
    return false;
}

export function rasterizeText(_text, _font, _size, _color) {
    return null;
}
