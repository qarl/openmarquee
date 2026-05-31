#!/usr/bin/env bash
# Build renderer-wasm (Phase 0 fontdue WASM bridge for Canvas2D
# parity). Two-step: cargo wasm32 build → wasm-bindgen post-process
# producing an ESM-consumable artifact under renderer-wasm/pkg/.
#
# Output: renderer-wasm/pkg/
#   renderer_wasm.js          - ESM JS glue
#   renderer_wasm.d.ts        - TypeScript types
#   renderer_wasm_bg.wasm     - 97 KiB raw, ~50 KiB gzipped
#   renderer_wasm_bg.wasm.d.ts
#
# Consumed by: ui/ (Phase 1, next dispatch). The pkg/ output stays
# untracked in renderer-wasm/.gitignore; CI / release pipelines
# re-run this script.
#
# Toolchain prerequisites (one-time):
#   rustup target add wasm32-unknown-unknown
#   cargo install -f wasm-bindgen-cli --version 0.2.121
#
# wasm-bindgen-cli version MUST match the wasm-bindgen crate version
# in renderer-wasm/Cargo.toml (currently 0.2.95 declared, 0.2.121
# resolved transitively — pin to .121 to silence the version-mismatch
# warning).

set -euo pipefail

# rustup-managed cargo / wasm-bindgen aren't always on the default
# PATH (e.g. invoked from a script subshell that doesn't source
# ~/.cargo/env). Prepend it so the binaries resolve.
export PATH="$HOME/.cargo/bin:$PATH"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CRATE_DIR="$REPO/renderer-wasm"
OUT_DIR="$CRATE_DIR/pkg"

echo "==> cargo build --release --target wasm32-unknown-unknown"
cd "$CRATE_DIR"
cargo build --release --target wasm32-unknown-unknown

echo "==> wasm-bindgen --target web --out-dir $OUT_DIR"
wasm-bindgen \
    --target web \
    --out-dir "$OUT_DIR" \
    "target/wasm32-unknown-unknown/release/renderer_wasm.wasm"

# wasm-bindgen --target web emits ESM (`export` syntax) but does NOT
# create a package.json (only --target bundler does). Browsers see the
# ESM hint via the `<script type="module">` load + esbuild's bundle
# step, but Node decides ESM-vs-CJS by walking parent dirs for the
# nearest package.json with `"type": "module"` — and there are no
# package.json files anywhere upstream of renderer-wasm/pkg/ (the
# repo root has none; renderer-wasm/ is a Rust crate with only
# Cargo.toml). Without this hint, Node defaults to CommonJS for .js
# files and chokes on the ESM `export` syntax with the misleading
# "is a CommonJS module" error. Emit a minimal package.json so any
# Node-side consumer (scripts/smoke_wasm_renderer.mjs, Playwright
# specs that transitively import ui/src/wasm-renderer.js, etc.) sees
# the file as ESM. pkg/ is gitignored so this regenerates per build.
echo '{"type":"module"}' > "$OUT_DIR/package.json"

WASM_BYTES=$(wc -c < "$OUT_DIR/renderer_wasm_bg.wasm")
GZIP_BYTES=$(gzip -c "$OUT_DIR/renderer_wasm_bg.wasm" | wc -c | tr -d ' ')
echo "==> built: $WASM_BYTES bytes raw, $GZIP_BYTES bytes gzipped"
echo "==> output: $OUT_DIR"
