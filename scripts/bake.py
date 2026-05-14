#!/usr/bin/env python3
"""Pre-bake a slide JSON to a PNG via the Canvas2D pipeline.

Wraps the parity-harness.html page (which exposes window.__parityCapture
backed by drawCanvas + stateFromItem) so a slide's stored JSON can be
turned into a flat PNG on disk -- the same bytes the editor would
produce client-side, headlessly. This is Phase 0 of the DELETE-PIL arc
(qarl-direct 2026-05-13): Canvas2D becomes the canonical static-bake
source so the Python+PIL rasterizer in seed.py / auto_render.py /
text_rerender.py can be deleted.

Usage:
  scripts/bake.py <slide.json> --out <path.png>
  scripts/bake.py <slide.json> --out <path.png> --tick 0.7

Input format: the wire-shape ContentItem -- either the full envelope
`{schema_version, updated_at, item: {...}}` (matches
renderer/tests/fixtures/<uuid>/item.json) or just the inner item dict.

Default tick=0 (the "rest" frame; motion animates per-frame at playback
in the Rust GPU compositor, but the static thumbnail/seed asset is
captured at t=0). Override via --tick for diagnostic captures of motion
mid-cycle.

Exit codes:
  0  PNG written
  1  bake failed (Playwright launch, harness load, capture, etc.)
  2  missing third-party deps (playwright)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS_HTML = REPO / "ui" / "parity-harness.html"


def _import_third_party() -> None:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError as exc:
        sys.stderr.write(
            "FAIL: bake.py needs playwright.\n"
            f"      missing: {exc.name}\n"
            "      install: pip3 install playwright "
            "&& playwright install chromium\n"
        )
        sys.exit(2)


def _load_item(slide_path: Path) -> dict:
    blob = json.loads(slide_path.read_text())
    # Accept both the wire-envelope shape (with schema_version + item)
    # and the bare item dict. Mirrors scripts/parity/run.py:load_item.
    if isinstance(blob, dict) and "item" in blob:
        return blob["item"]
    return blob


def _start_static_server(ui_root: Path) -> tuple[socketserver.TCPServer, int]:
    """Serve ui/ over localhost so the harness page can resolve its
    ES-module imports (./src/rasterize.js etc.) -- ES-modules over
    file:// trigger CORS errors in headless Chromium."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(ui_root),
    )
    server = socketserver.TCPServer(
        ("127.0.0.1", 0), handler, bind_and_activate=True
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


async def bake(slide_path: Path, out_path: Path, tick: float) -> int:
    _import_third_party()
    from playwright.async_api import async_playwright

    item = _load_item(slide_path)
    server, port = _start_static_server(HARNESS_HTML.parent)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
            )
            page = await context.new_page()
            page.on(
                "console",
                lambda msg: print(
                    f"  [browser:{msg.type}] {msg.text}", file=sys.stderr,
                ),
            )
            page.on(
                "pageerror",
                lambda err: print(f"  [browser:error] {err}", file=sys.stderr),
            )
            url = f"http://127.0.0.1:{port}/parity-harness.html"
            await page.goto(url)
            await page.wait_for_function(
                "document.getElementById('parity-status')"
                ".textContent.includes('ready')",
                timeout=10_000,
            )
            params = {"kind": "single", "item": item, "tick": tick}
            b64 = await page.evaluate(
                "(p) => window.__parityCapture(p)", params,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(base64.b64decode(b64))
            await context.close()
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "slide_json",
        type=Path,
        help="Path to a wire-shape ContentItem JSON (envelope or bare item).",
    )
    ap.add_argument(
        "--out", type=Path, required=True,
        help="Output PNG path.",
    )
    ap.add_argument(
        "--tick", type=float, default=0.0,
        help="Animation tick (seconds). Default 0 (rest frame).",
    )
    args = ap.parse_args()
    if not args.slide_json.exists():
        sys.stderr.write(f"FAIL: slide JSON not found: {args.slide_json}\n")
        return 1
    if not HARNESS_HTML.exists():
        sys.stderr.write(f"FAIL: parity harness missing: {HARNESS_HTML}\n")
        return 1
    return asyncio.run(bake(args.slide_json, args.out, args.tick))


if __name__ == "__main__":
    sys.exit(main())
