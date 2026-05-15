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

WASM_BYTES=$(wc -c < "$OUT_DIR/renderer_wasm_bg.wasm")
GZIP_BYTES=$(gzip -c "$OUT_DIR/renderer_wasm_bg.wasm" | wc -c | tr -d ' ')
echo "==> built: $WASM_BYTES bytes raw, $GZIP_BYTES bytes gzipped"
echo "==> output: $OUT_DIR"
