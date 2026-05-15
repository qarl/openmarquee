#!/usr/bin/env python3
"""Boot-slide side-by-side parity capture (one-off, qarl eyeball test).

Renders the "15 · Boot" fixture
(renderer/tests/fixtures/f0000000-0000-4000-8000-000000000023/item.json)
via the Canvas2D parity harness at tick=0.6 s (mid-slide, mid-breathe
cycle for the PANEL-0 OK badge), then composes a side-by-side with
the pre-rendered Rust output at qa/captures/boot-rust.png.

Outputs (all to qa/captures/):
  - boot-canvas2d.png       Canvas2D capture at t=0.6
  - boot-sxs.png            side-by-side composite (4px gutter)
  - boot-diff.png           per-pixel abs delta * 8 (visualize subpixel drift)

Run:
  scripts/parity/boot_sxs.py
"""

from __future__ import annotations

import asyncio
import base64
import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURE_DIR = REPO / "renderer" / "tests" / "fixtures"
BOOT_UUID = "f0000000-0000-4000-8000-000000000023"
RUST_PNG = REPO / "qa" / "captures" / "boot-rust.png"
OUT_DIR = REPO / "qa" / "captures"
HARNESS_HTML = REPO / "ui" / "parity-harness.html"
TICK_SECONDS = 0.6


async def capture_canvas2d() -> bytes:
    """Spin up a Playwright Chromium against parity-harness.html,
    drive __parityCapture for the Boot fixture at tick=0.6, return
    the PNG bytes. Mirrors scripts/parity/run.py:capture_browser_pngs
    but for one fixture only."""
    from playwright.async_api import async_playwright

    item = json.loads((FIXTURE_DIR / BOOT_UUID / "item.json").read_text())["item"]

    # Static fileserver: parity-harness imports relative modules
    # (ui/src/rasterize.js etc.) so we need REPO as the docroot.
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(REPO)
    )
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as srv:
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    device_scale_factor=1.0,
                )
                page = await context.new_page()
                page.on(
                    "console",
                    lambda msg: print(
                        f"  [browser:{msg.type}] {msg.text}", file=sys.stderr
                    ),
                )
                await page.goto(f"http://127.0.0.1:{port}/ui/parity-harness.html")
                await page.wait_for_function(
                    "document.getElementById('parity-status')"
                    ".textContent.includes('ready')",
                    timeout=10_000,
                )
                params = {"kind": "single", "item": item, "tick": TICK_SECONDS}
                b64 = await page.evaluate(
                    "(p) => window.__parityCapture(p)", params
                )
                await context.close()
                await browser.close()
                return base64.b64decode(b64)
        finally:
            srv.shutdown()


def composite_and_diff(canvas_png: bytes) -> dict:
    """Save the Canvas2D capture, compose the side-by-side + diff
    image, return per-pixel-delta metrics."""
    from PIL import Image
    import numpy as np

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    canvas_path = OUT_DIR / "boot-canvas2d.png"
    canvas_path.write_bytes(canvas_png)

    c = Image.open(canvas_path).convert("RGB")
    r = Image.open(RUST_PNG).convert("RGB")
    if c.size != r.size:
        c = c.resize(r.size, Image.LANCZOS)

    # Side-by-side. 4 px gutter (gray so the seam is visible against
    # both renders' dark bgs).
    gutter = 4
    sxs = Image.new("RGB", (c.width + gutter + r.width, c.height), (128, 128, 128))
    sxs.paste(c, (0, 0))
    sxs.paste(r, (c.width + gutter, 0))
    sxs.save(OUT_DIR / "boot-sxs.png")

    # Diff: per-pixel abs delta amplified 8x so subpixel drift is
    # visible. Saturates at 255. Clamp to [0, 255] after the multiply.
    ca = np.asarray(c, dtype=np.int16)
    ra = np.asarray(r, dtype=np.int16)
    delta = np.abs(ca - ra).astype(np.int32)
    delta_vis = np.clip(delta * 8, 0, 255).astype(np.uint8)
    Image.fromarray(delta_vis, mode="RGB").save(OUT_DIR / "boot-diff.png")

    # Where are the largest deltas?  Identify the y-band containing
    # the biggest mean delta — helps qarl localize the drift visually.
    per_row_mean = delta.mean(axis=(1, 2))
    hottest_row = int(per_row_mean.argmax())
    hottest_row_mean = float(per_row_mean[hottest_row])
    return {
        "max_delta": int(delta.max()),
        "mean_delta": float(delta.mean()),
        "pct_over_10": float((delta > 10).any(axis=2).mean() * 100.0),
        "hottest_row": hottest_row,
        "hottest_row_mean": hottest_row_mean,
        "size": c.size,
    }


def main() -> int:
    if not RUST_PNG.exists():
        sys.stderr.write(f"FAIL: missing {RUST_PNG}\n")
        return 2
    print(f"capturing Canvas2D for Boot fixture at tick={TICK_SECONDS}s...")
    canvas_png = asyncio.run(capture_canvas2d())
    print("compositing + diffing vs boot-rust.png...")
    metrics = composite_and_diff(canvas_png)
    print()
    print(f"  boot-canvas2d.png:  {OUT_DIR / 'boot-canvas2d.png'}")
    print(f"  boot-rust.png:      {RUST_PNG}")
    print(f"  boot-sxs.png:       {OUT_DIR / 'boot-sxs.png'}")
    print(f"  boot-diff.png:      {OUT_DIR / 'boot-diff.png'}")
    print()
    print(f"  max_delta:      {metrics['max_delta']}")
    print(f"  mean_delta:     {metrics['mean_delta']:.3f}")
    print(f"  pct_over_10:    {metrics['pct_over_10']:.2f}%")
    print(f"  hottest row:    y={metrics['hottest_row']} "
          f"(mean delta {metrics['hottest_row_mean']:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
