#!/usr/bin/env python3
"""Phase 3j Canvas2D-side quad/rect probe.

Loads parity-harness.html in headless Chromium, monkey-patches
CanvasRenderingContext2D.drawImage to log every call with the
exact args paintLayer passes (drawX, drawY, drawW, drawH +
source image dims). Drives the parity_font_inter fixture so we
get exactly the rect the parity harness produces for that
fixture. Emits qa/captures/quad-rect-canvas2d-2026-05-15.json.

Output is paired with renderer/examples/advance_probe.rs's
phase3j_rust_quad_parity_font_inter block to byte-compare the
two rectangles at canvas-pixel coords.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURE_DIR = REPO / "renderer" / "tests" / "fixtures"
OUT_PATH = REPO / "qa" / "captures" / "quad-rect-canvas2d-2026-05-15.json"


def _start_server(doc_root: Path):
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(doc_root),
    )
    server = socketserver.TCPServer(("127.0.0.1", 0), handler, bind_and_activate=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def load_item(uuid: str) -> dict:
    path = FIXTURE_DIR / uuid / "item.json"
    blob = json.loads(path.read_text())
    return blob["item"] if "item" in blob else blob


async def main():
    from playwright.async_api import async_playwright

    server, port = _start_server(REPO)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(viewport={"width": 1920, "height": 1080})
            page = await ctx.new_page()
            page.on(
                "console",
                lambda msg: print(f"  [browser:{msg.type}] {msg.text}", file=sys.stderr),
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
            # Inject the drawImage spy + measureText spy BEFORE the
            # fixture render so we capture both: the bg fill (if any),
            # the glyph blit, and the ctx.measureText('INTER') result
            # that drives yScale/effectiveSizePx.
            await page.evaluate(
                """() => {
                    window.__drawImageCaptures = [];
                    window.__measureTextCaptures = [];
                    const proto = CanvasRenderingContext2D.prototype;
                    const origDraw = proto.drawImage;
                    proto.drawImage = function(...args) {
                        try {
                            const src = args[0];
                            const srcW = src && src.width ? src.width : null;
                            const srcH = src && src.height ? src.height : null;
                            window.__drawImageCaptures.push({
                                argc: args.length,
                                src_w: srcW,
                                src_h: srcH,
                                args_after_src: args.slice(1).map(a =>
                                    typeof a === 'number' ? a : String(a)
                                ),
                                font: this.font,
                            });
                        } catch (e) {
                            window.__drawImageCaptures.push({error: String(e)});
                        }
                        return origDraw.apply(this, args);
                    };
                    const origMT = proto.measureText;
                    proto.measureText = function(text) {
                        const m = origMT.call(this, text);
                        try {
                            window.__measureTextCaptures.push({
                                text: text,
                                font: this.font,
                                width: m.width,
                                actualBoundingBoxAscent: m.actualBoundingBoxAscent,
                                actualBoundingBoxDescent: m.actualBoundingBoxDescent,
                                fontBoundingBoxAscent: m.fontBoundingBoxAscent,
                                fontBoundingBoxDescent: m.fontBoundingBoxDescent,
                            });
                        } catch (e) {
                            window.__measureTextCaptures.push({error: String(e)});
                        }
                        return m;
                    };
                }"""
            )
            item = load_item("f0000000-0000-4000-8000-000000000015")  # parity_font_inter
            params = {"kind": "single", "item": item, "tick": 0.0}
            await page.evaluate(
                f"window.__parityCapture({json.dumps(params)})"
            )
            caps = await page.evaluate("window.__drawImageCaptures")
            mt_caps = await page.evaluate("window.__measureTextCaptures")
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()

    # Filter to the glyph blit (3- or 9-arg form on a small bitmap
    # source). The fixture has solid bg so it shouldn't paint via
    # drawImage; only the WASM glyph rasterization should appear.
    out = {
        "fixture": "parity_font_inter",
        "fixture_uuid": "f0000000-0000-4000-8000-000000000015",
        "viewport": {"w": 1920, "h": 1080},
        "drawImage_calls": caps,
        "measureText_calls": mt_caps,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"wrote {OUT_PATH}")
    for i, c in enumerate(caps):
        print(f"  call[{i}]: argc={c.get('argc')} src={c.get('src_w')}x{c.get('src_h')} args={c.get('args_after_src')}")


if __name__ == "__main__":
    asyncio.run(main())
