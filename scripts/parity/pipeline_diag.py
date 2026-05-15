#!/usr/bin/env python3
"""Phase 3b capture-pipeline diagnostic.

Phase 3a's mean improvements (font_inter -5.51, font_cinzel -4.85,
etc.) didn't move the max_delta gate -- all text-heavy fixtures
stayed at 229-231. The Phase 1c single-glyph diagnostic showed max=62
on a clean fixture, so the 229-231 ceiling on the suite is either:
  (i) a renderer divergence the diagnostic missed (sub-pixel position
      shift on multi-line text, AA-coverage threshold drift, etc.)
  (ii) a capture-pipeline artifact (Playwright readback vs Pi-side
      framebuffer readback diverging, golden staleness, etc.)
  (iii) some combination

This script captures ONE fixture (text_static = multiline_wrap) via
multiple routes and cross-compares. The pipelines:

  A. canvas2d-suite     parity-harness via Playwright, same code path
                        scripts/parity/run.py drives -- represents
                        what scripts/parity_tests.sh sees.
  B. canvas2d-diag      parity-harness via Playwright, same code path
                        scripts/parity/glyph_sxs.py drives. If A and
                        B differ, the Python driver matters (it
                        shouldn't -- the harness is the same).
  C. rust-fresh         SSH to dev Pi, --capture-slide the same uuid.
                        Represents what Rust renders RIGHT NOW.
  D. rust-golden        Checked-in renderer/tests/golden/multiline_
                        wrap.png. Represents what Rust renderer at
                        the time the golden was blessed.

Cross-comparisons:
  A vs B    -> capture pipeline equivalence. Expected 0.
  C vs D    -> golden staleness / Rust non-determinism.
  A vs C    -> real Canvas2D vs current Rust divergence.
  A vs D    -> what scripts/parity_tests.sh actually measures (229).
"""

from __future__ import annotations

import asyncio
import base64
import functools
import http.server
import json
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURE_DIR = REPO / "renderer" / "tests" / "fixtures"
FIXTURE_UUID = "f0000000-0000-4000-8000-000000000002"  # multiline_wrap
GOLDEN_PNG = REPO / "renderer" / "tests" / "golden" / "multiline_wrap.png"
HARNESS_HTML = REPO / "ui" / "parity-harness.html"
OUT_DIR = REPO / "qa" / "captures"

PI_TARGET = "openmarquee@openMarqueeDev"
PI_RENDERER = "/usr/local/bin/openmarquee-render"
PI_FIXTURE_ROOT = "/tmp/render-test-content"
PI_FONT_DIR = "/opt/openmarquee/ui/fonts"
PI_FRESH_PNG = "/tmp/text-static-rust-fresh.png"


async def capture_canvas2d(label: str) -> bytes:
    """Capture text_static via the parity harness. The harness is
    the same code path regardless of which Python driver invokes
    it, so capture_canvas2d('suite') and capture_canvas2d('diag')
    should produce byte-identical output IFF the pipeline is
    deterministic (Playwright canvas readback is at-its-best
    deterministic; flakes would be a separate bug we'd surface)."""
    from playwright.async_api import async_playwright

    item = json.loads((FIXTURE_DIR / FIXTURE_UUID / "item.json").read_text())["item"]

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
                await page.goto(f"http://127.0.0.1:{port}/ui/parity-harness.html")
                await page.wait_for_function(
                    "document.getElementById('parity-status')"
                    ".textContent.includes('ready')",
                    timeout=10_000,
                )
                b64 = await page.evaluate(
                    "(p) => window.__parityCapture(p)",
                    {"kind": "single", "item": item, "tick": 0.0},
                )
                await context.close()
                await browser.close()
                return base64.b64decode(b64)
        finally:
            srv.shutdown()


def capture_rust_fresh() -> bytes:
    """Capture text_static via dev Pi --capture-slide. Fresh render
    using the current renderer binary. If this differs from the
    checked-in golden, either (a) the golden is stale or (b) Rust
    is non-deterministic for this fixture."""
    print(f"  capture rust-fresh via Pi --capture-slide...", file=sys.stderr)
    # Fixture is already deployed to /tmp/render-test-content via
    # earlier glyph_sxs runs OR via render_tests.sh; deploy again to
    # be safe (idempotent).
    fixture_src = FIXTURE_DIR / FIXTURE_UUID
    subprocess.run(
        ["ssh", "-q", PI_TARGET, f"mkdir -p {PI_FIXTURE_ROOT}/{FIXTURE_UUID}"],
        check=True,
    )
    subprocess.run(
        ["scp", "-q",
         str(fixture_src / "item.json"),
         f"{PI_TARGET}:{PI_FIXTURE_ROOT}/{FIXTURE_UUID}/item.json"],
        check=True,
    )
    cap_cmd = (
        f"{PI_RENDERER} --output hdmi"
        f" --capture-slide {FIXTURE_UUID}"
        f" --capture-slide-at-tick 0.0"
        f" --content-root {PI_FIXTURE_ROOT}"
        f" --font-dir {PI_FONT_DIR}"
        f" --capture-path {PI_FRESH_PNG}"
        f" --force-mode 1920x1080@60"
    )
    result = subprocess.run(
        ["ssh", "-q", PI_TARGET, cap_cmd],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  STDOUT: {result.stdout}", file=sys.stderr)
        print(f"  STDERR: {result.stderr}", file=sys.stderr)
        raise RuntimeError(f"Pi --capture-slide failed: rc={result.returncode}")
    local = Path(PI_FRESH_PNG)  # same path; Pi-side and local-side mirror
    subprocess.run(
        ["scp", "-q", f"{PI_TARGET}:{PI_FRESH_PNG}", str(local)],
        check=True,
    )
    return local.read_bytes()


def metrics(a_png: bytes, b_png: bytes) -> dict:
    """Per-channel max-delta + mean-delta + count of non-zero-delta
    pixels for an A-vs-B comparison."""
    from PIL import Image
    import io
    import numpy as np

    a = np.array(Image.open(io.BytesIO(a_png)).convert("RGBA"), dtype=np.int16)
    b = np.array(Image.open(io.BytesIO(b_png)).convert("RGBA"), dtype=np.int16)
    if a.shape != b.shape:
        return {
            "shape_a": list(a.shape),
            "shape_b": list(b.shape),
            "error": "shape mismatch -- captures have different dimensions",
        }
    delta = np.abs(a - b)
    per_channel_max = [int(delta[..., c].max()) for c in range(4)]
    per_channel_mean = [float(delta[..., c].mean()) for c in range(4)]
    rgb_max = max(per_channel_max[:3])
    pixels_with_any_diff = int((delta[..., :3] > 0).any(axis=-1).sum())
    total_pixels = a.shape[0] * a.shape[1]
    return {
        "per_channel_max_RGBA": per_channel_max,
        "per_channel_mean_RGBA": per_channel_mean,
        "rgb_max_delta": rgb_max,
        "pixels_with_any_diff": pixels_with_any_diff,
        "total_pixels": total_pixels,
        "pct_pixels_diff": 100.0 * pixels_with_any_diff / total_pixels,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"=== Phase 3b capture-pipeline diagnostic ===", file=sys.stderr)
    print(f"Fixture: {FIXTURE_UUID} (multiline_wrap / text_static)\n",
          file=sys.stderr)

    print(f"[A] capture via canvas2d-suite pipeline...", file=sys.stderr)
    a_png = asyncio.run(capture_canvas2d("suite"))
    (OUT_DIR / "diag-canvas2d-suite.png").write_bytes(a_png)

    print(f"[B] capture via canvas2d-diag pipeline (second invocation)...",
          file=sys.stderr)
    b_png = asyncio.run(capture_canvas2d("diag"))
    (OUT_DIR / "diag-canvas2d-diag.png").write_bytes(b_png)

    print(f"[C] capture via rust-fresh pipeline (Pi)...", file=sys.stderr)
    c_png = capture_rust_fresh()
    (OUT_DIR / "diag-rust-fresh.png").write_bytes(c_png)

    print(f"[D] read rust-golden (checked-in)...", file=sys.stderr)
    d_png = GOLDEN_PNG.read_bytes()
    # Don't re-emit; the existing golden is the canonical reference.

    print(f"\ncross-comparisons:\n", file=sys.stderr)

    pairs = [
        ("A vs B  (canvas2d suite vs canvas2d diag)", a_png, b_png),
        ("C vs D  (rust fresh   vs rust golden)",     c_png, d_png),
        ("A vs C  (canvas2d     vs rust fresh)",      a_png, c_png),
        ("A vs D  (canvas2d     vs rust golden)",     a_png, d_png),
    ]
    out_metrics = {}
    for label, x, y in pairs:
        m = metrics(x, y)
        out_metrics[label] = m
        if "error" in m:
            print(f"  {label}: SHAPE MISMATCH  {m['shape_a']} vs {m['shape_b']}")
            continue
        print(f"  {label}:")
        print(f"    per-channel max R/G/B/A:  {m['per_channel_max_RGBA']}")
        print(f"    per-channel mean R/G/B/A: "
              f"{[f'{x:.3f}' for x in m['per_channel_mean_RGBA']]}")
        print(f"    pixels with any RGB diff: {m['pixels_with_any_diff']:>10d} "
              f"({m['pct_pixels_diff']:5.2f}%)")
        print()

    (OUT_DIR / "diag-pipeline-metrics.json").write_text(
        json.dumps(out_metrics, indent=2)
    )

    # Quick verdict surface.
    rgb_max_ab = out_metrics["A vs B  (canvas2d suite vs canvas2d diag)"].get("rgb_max_delta", -1)
    rgb_max_cd = out_metrics["C vs D  (rust fresh   vs rust golden)"].get("rgb_max_delta", -1)
    rgb_max_ac = out_metrics["A vs C  (canvas2d     vs rust fresh)"].get("rgb_max_delta", -1)
    rgb_max_ad = out_metrics["A vs D  (canvas2d     vs rust golden)"].get("rgb_max_delta", -1)
    print(f"\n=== Verdict surface ===", file=sys.stderr)
    print(f"  A vs B  rgb max:  {rgb_max_ab}  "
          f"({'pipeline equivalent' if rgb_max_ab <= 2 else 'pipeline DIVERGES'})",
          file=sys.stderr)
    print(f"  C vs D  rgb max:  {rgb_max_cd}  "
          f"({'golden current' if rgb_max_cd <= 2 else 'GOLDEN STALE or RUST NONDETERMINISTIC'})",
          file=sys.stderr)
    print(f"  A vs C  rgb max:  {rgb_max_ac}  "
          f"(Canvas2D vs Rust real divergence right now)",
          file=sys.stderr)
    print(f"  A vs D  rgb max:  {rgb_max_ad}  "
          f"(what scripts/parity_tests.sh actually reports)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
