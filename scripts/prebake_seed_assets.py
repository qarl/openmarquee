#!/usr/bin/env python3
"""Pre-bake the FREE YOUR SIGN demo reel via Canvas2D.

Phase 1 of DELETE-PIL (qarl-direct 2026-05-13): bake each
_DemoFrame from seed.py into a PNG and commit them as static
assets under backend/openmarquee/seed_assets/demo_reel/. Seed-time
now reads these PNGs from disk instead of calling _draw_text_into
+ render_pattern, which means the Python+PIL rasterizer can be
deleted in phase 3.

Re-run any time the demo reel spec in seed.py:_DEMO_REEL changes:
  python3 scripts/prebake_seed_assets.py
Output paths land under backend/openmarquee/seed_assets/demo_reel/
named NN.png where NN is the zero-padded slot index.

Implementation reuses scripts/bake.py's pattern (Playwright +
parity-harness.html via window.__parityCapture) but batches all 19
captures into a single browser session so the script completes in
~10s instead of ~60s for 19 cold-start launches.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS_HTML = REPO / "ui" / "parity-harness.html"
OUT_DIR = REPO / "backend" / "openmarquee" / "seed_assets" / "demo_reel"

# Add backend/ to sys.path so we can import openmarquee.seed.
sys.path.insert(0, str(REPO / "backend"))


def _slide_to_wire_item(frame, slot_idx: int) -> dict:
    """Convert a seed._DemoFrame to the wire-shape ContentItem dict
    parity-harness.html consumes via __parityCapture(kind='single')."""
    text_layers = []
    for layer in frame.layers:
        text_layers.append({
            "text": layer.text,
            "name": "",
            "font_family": layer.font_family,
            "font_size_px": layer.font_size_px,
            "font_size_pct": layer.font_size_pct,
            "weight": None,
            "text_color": layer.text_color,
            "text_align": layer.text_align,
            "outline": False,
            "opacity": layer.opacity,
            "anchor": "center",
            "visible": True,
            "locked": False,
            "motion": layer.motion,
            "motion_intensity": layer.motion_intensity,
            "motion_phase": layer.motion_phase,
            "motion_speed": layer.motion_speed,
            "blend": layer.blend,
            "auto_mode": None,
            "auto_format": None,
            "box": {
                "x": layer.box[0], "y": layer.box[1],
                "w": layer.box[2], "h": layer.box[3],
            },
        })
    item = {
        "type": "text_slide",
        # Deterministic-but-fake UUID per slot for parity-harness use.
        # Real slide IDs are minted at seed-time from a separate
        # generator; this UUID is only used by the harness's internal
        # bookkeeping and doesn't leak to the stored content.
        "id": f"f0000000-0000-4000-8000-deadbeef{slot_idx:04x}",
        "name": frame.name,
        "duration_ms": frame.duration_ms,
        "text_layers": text_layers,
        "background_color": frame.background_color,
        "background_pattern": None,
        "transition": frame.transition_out,
        "transition_ms": frame.transition_ms,
    }
    if frame.background_pattern is not None:
        item["background_pattern"] = {
            "pattern": frame.background_pattern.pattern,
            "color_a": frame.background_pattern.color_a,
            "color_b": frame.background_pattern.color_b,
            "density": frame.background_pattern.density,
        }
    return item


def _start_static_server(ui_root: Path) -> tuple[socketserver.TCPServer, int]:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(ui_root),
    )
    server = socketserver.TCPServer(
        ("127.0.0.1", 0), handler, bind_and_activate=True,
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


async def main() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        sys.stderr.write(
            "FAIL: prebake needs playwright.\n"
            f"      missing: {exc.name}\n"
            "      install: pip3 install playwright "
            "&& playwright install chromium\n"
        )
        return 2
    from openmarquee.seed import _DEMO_REEL  # type: ignore

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Serve the repo ROOT (not ui/) so the harness's wasm-renderer.js
    # import of ../../renderer-wasm/pkg/* resolves. Serving ui/ alone
    # 404s the WASM module and the harness never reaches its 'ready'
    # state (10s wait_for_function timeout). Same fix as bake.py:75-80.
    server, port = _start_static_server(REPO)
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
            url = f"http://127.0.0.1:{port}/ui/parity-harness.html"
            await page.goto(url)
            await page.wait_for_function(
                "document.getElementById('parity-status')"
                ".textContent.includes('ready')",
                timeout=10_000,
            )
            for slot_idx, frame in enumerate(_DEMO_REEL):
                item = _slide_to_wire_item(frame, slot_idx)
                # tick=0 -> static "rest" frame. Motion animates at
                # playback in the device's Rust compositor; the
                # static asset is just the t=0 snapshot.
                params = {"kind": "single", "item": item, "tick": 0.0}
                b64 = await page.evaluate(
                    "(p) => window.__parityCapture(p)", params,
                )
                out_path = OUT_DIR / f"{slot_idx:02d}.png"
                out_path.write_bytes(base64.b64decode(b64))
                print(f"  baked: {out_path.name}  ({frame.name})")
            await context.close()
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print(f"PASS: pre-baked {len(_DEMO_REEL)} slides to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
