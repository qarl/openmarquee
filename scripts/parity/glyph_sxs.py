#!/usr/bin/env python3
"""Phase 1c single-glyph diagnostic — localize the residual divergence
between Canvas2D-WASM and Rust renderer paths after Phase 1b's swap
left 250 max-delta on Boot despite both sides using fontdue 0.9.

Hypotheses to test (per the Phase 1c dispatch):
  (a) Subpixel positioning  -> center-of-mass differs by <1px on each axis
  (b) Gamma applied at different pipeline point  -> per-channel max-delta
      similar across R/G/B (gamma is per-channel uniform)
  (c) Premultiplied vs straight alpha mismatch  -> delta histogram peaks
      at the AA edge ring, interior pixels near-zero
  (d) Color space (sRGB linearization) applied once vs twice  -> ~26 LSB
      offset at mid-gray
  (e) Something else surfaced by the data

Single-glyph state pinned across both paths:
  Font: VT323 (Boot's font face, simplest pixel shapes available)
  Size: 32 px (Boot's font_size)
  Color: pure white (#FFFFFF)
  Background: pure black (#000000)
  Position: box.x = box.y = 0.125 (binary-representable in IEEE
    float64). 0.125 * 1920 = 240 EXACTLY, 0.125 * 1080 = 135 EXACTLY,
    so neither renderer sees fractional canvas coords. Avoids
    confounding Canvas2D's `Math.round(drawX)` integer-snap with
    Rust's GLES2 sub-pixel NDC placement (pre-review v1 used
    100/1920 which round-trips to 99.99999... in float64 — both
    mechanisms could contribute to the observed shift, making the
    1px-pad hypothesis non-falsifiable). Glyph lands at (240, 135).
  No motion, no transitions, no transforms

Outputs (qa/captures/):
  glyph-canvas2d.png  Canvas2D-WASM full-canvas render
  glyph-rust.png      Rust full-canvas render (fetched from dev Pi)
  glyph-diff.png      per-pixel abs delta * 8 amplification
  glyph-sxs.png       4px-gutter side-by-side

Run:
  scripts/parity/glyph_sxs.py
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
GLYPH_UUID = "f0000000-0000-4000-8000-000000000099"
HARNESS_HTML = REPO / "ui" / "parity-harness.html"
OUT_DIR = REPO / "qa" / "captures"

PI_TARGET = "openmarquee@openMarqueeDev"
PI_RENDERER = "/usr/local/bin/openmarquee-render"
PI_FIXTURE_ROOT = "/tmp/render-test-content"
PI_FONT_DIR = "/opt/openmarquee/ui/fonts"  # Pi's deploy-path default
PI_GLYPH_PNG = "/tmp/glyph-rust-capture.png"


async def capture_canvas2d() -> bytes:
    """Capture Canvas2D-WASM side via the parity harness's
    __parityCapture(single, item, tick=0). Returns PNG bytes.
    Mirrors boot_sxs.py."""
    from playwright.async_api import async_playwright

    item = json.loads((FIXTURE_DIR / GLYPH_UUID / "item.json").read_text())["item"]

    # Static fileserver: parity-harness imports modules relative to
    # ui/ AND wasm-renderer.js imports ../../renderer-wasm/, so the
    # docroot must be REPO (boot_sxs.py / scripts/parity/run.py both
    # use this convention).
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
                b64 = await page.evaluate(
                    "(p) => window.__parityCapture(p)",
                    {"kind": "single", "item": item, "tick": 0.0},
                )
                await context.close()
                await browser.close()
                return base64.b64decode(b64)
        finally:
            srv.shutdown()


def capture_rust() -> bytes:
    """Capture Rust side via dev Pi over SSH. Deploys the fixture,
    runs --capture-slide, fetches the PNG. The Pi's
    /usr/local/bin/openmarquee-render binary is current as of
    2026-05-14 (rebuilt during the V4L2 piece-4 work)."""
    # Deploy single-glyph fixture to the Pi's content-root staging
    # area. render_tests.sh's PI_FIXTURE_ROOT convention so we don't
    # collide with the production /var/openmarquee/content tree.
    fixture_src = FIXTURE_DIR / GLYPH_UUID
    print(f"  deploying fixture {GLYPH_UUID} to {PI_TARGET}:{PI_FIXTURE_ROOT}/",
          file=sys.stderr)
    subprocess.run(
        ["ssh", "-q", PI_TARGET, f"mkdir -p {PI_FIXTURE_ROOT}/{GLYPH_UUID}"],
        check=True,
    )
    subprocess.run(
        ["scp", "-q",
         str(fixture_src / "item.json"),
         f"{PI_TARGET}:{PI_FIXTURE_ROOT}/{GLYPH_UUID}/item.json"],
        check=True,
    )
    print(f"  running --capture-slide on Pi...", file=sys.stderr)
    cap_cmd = (
        f"{PI_RENDERER} --output hdmi"
        f" --capture-slide {GLYPH_UUID}"
        f" --capture-slide-at-tick 0.0"
        f" --content-root {PI_FIXTURE_ROOT}"
        f" --font-dir {PI_FONT_DIR}"
        f" --capture-path {PI_GLYPH_PNG}"
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
    print(f"  fetching {PI_GLYPH_PNG} back...", file=sys.stderr)
    local_path = Path("/tmp/glyph-rust-capture.png")
    subprocess.run(
        ["scp", "-q",
         f"{PI_TARGET}:{PI_GLYPH_PNG}",
         str(local_path)],
        check=True,
    )
    return local_path.read_bytes()


def compose_sxs(canvas_png: bytes, rust_png: bytes) -> bytes:
    """4px-gutter side-by-side, white gutter. boot_sxs.py style."""
    from PIL import Image
    import io
    a = Image.open(io.BytesIO(canvas_png)).convert("RGB")
    b = Image.open(io.BytesIO(rust_png)).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)
    gutter = 4
    w = a.width + gutter + b.width
    h = a.height
    out = Image.new("RGB", (w, h), (255, 255, 255))
    out.paste(a, (0, 0))
    out.paste(b, (a.width + gutter, 0))
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def compose_diff(canvas_png: bytes, rust_png: bytes) -> bytes:
    """Per-pixel abs delta * 8 amplified, RGB only."""
    from PIL import Image
    import io
    import numpy as np
    a = np.array(Image.open(io.BytesIO(canvas_png)).convert("RGB"), dtype=np.int16)
    b = np.array(Image.open(io.BytesIO(rust_png)).convert("RGB"), dtype=np.int16)
    delta = np.clip(np.abs(a - b) * 8, 0, 255).astype(np.uint8)
    out = Image.fromarray(delta, mode="RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def diagnostic_metrics(canvas_png: bytes, rust_png: bytes) -> dict:
    """Compute the Phase 1c diagnostic dimensions: per-channel max
    delta, per-quadrant mean delta, delta histogram, center-of-mass
    delta. Scoped to the glyph bitmap region (~32x40 px around the
    pinned origin at 100,100), NOT the full 1920x1080 canvas, so
    background pixels don't dilute the means."""
    from PIL import Image
    import io
    import numpy as np

    a_rgba = np.array(Image.open(io.BytesIO(canvas_png)).convert("RGBA"), dtype=np.int16)
    b_rgba = np.array(Image.open(io.BytesIO(rust_png)).convert("RGBA"), dtype=np.int16)

    # Glyph region: top-left at (240, 135) — binary-representable
    # canvas coords (0.125 * 1920 / 1080). Generous 64x64 box so we
    # capture the entire glyph footprint regardless of font metrics.
    GX, GY, GW, GH = 240, 135, 64, 64
    a_region = a_rgba[GY:GY+GH, GX:GX+GW]
    b_region = b_rgba[GY:GY+GH, GX:GX+GW]

    # Per-channel max-delta across the glyph region (R, G, B, A).
    delta = np.abs(a_region - b_region)
    per_channel_max = [int(delta[..., c].max()) for c in range(4)]
    per_channel_mean = [float(delta[..., c].mean()) for c in range(4)]

    # Per-quadrant mean-delta (TL, TR, BL, BR of the glyph region).
    half_h = GH // 2
    half_w = GW // 2
    quads = {
        "TL": delta[:half_h, :half_w].mean(),
        "TR": delta[:half_h, half_w:].mean(),
        "BL": delta[half_h:, :half_w].mean(),
        "BR": delta[half_h:, half_w:].mean(),
    }
    per_quadrant_mean = {k: float(v) for k, v in quads.items()}

    # 5-bucket histogram of |delta| values across all 4 channels in
    # the glyph region. "small-only" = compositing/gamma; "few-large"
    # = subpixel shift.
    flat = delta.flatten()
    histogram = {
        "0":     int(((flat == 0)).sum()),
        "1-2":   int(((flat >= 1) & (flat <= 2)).sum()),
        "3-10":  int(((flat >= 3) & (flat <= 10)).sum()),
        "11-50": int(((flat >= 11) & (flat <= 50)).sum()),
        "51-255": int((flat >= 51).sum()),
    }

    # Center-of-mass of each side's glyph coverage. Use luminance
    # (R since text is white-on-black; equivalent to all 3 RGB
    # channels for #FFFFFF text -- review #NIT suggested alpha, but
    # the Rust capture's alpha channel is 255 everywhere because
    # capture_fbo_to_rgba composites onto an opaque framebuffer,
    # so alpha-COM is uninformative on this side).
    def centroid(rgba_region):
        coverage = rgba_region[..., 0].astype(np.float64)
        total = coverage.sum()
        if total < 1.0:
            return (0.0, 0.0), 0.0
        ys, xs = np.indices(coverage.shape)
        cy = float((ys * coverage).sum() / total)
        cx = float((xs * coverage).sum() / total)
        return (cx, cy), float(total)

    (cx_a, cy_a), mass_a = centroid(a_region)
    (cx_b, cy_b), mass_b = centroid(b_region)
    # Sanity: if one side has dramatically less luminance, the glyph
    # likely overflowed the region on one side -- the COM delta
    # below would be meaningless. Surface a warning rather than
    # silently mis-attributing.
    mass_ratio = (
        min(mass_a, mass_b) / max(mass_a, mass_b)
        if max(mass_a, mass_b) > 0 else 1.0
    )
    if mass_ratio < 0.85:
        print(
            f"  WARN: glyph luminance differs by {(1-mass_ratio)*100:.1f}% "
            f"between sides (Canvas2D={mass_a:.0f}, Rust={mass_b:.0f}). "
            f"COM dx/dy may be unreliable; check if glyph overflows region.",
            file=sys.stderr,
        )
    com_delta = {
        "canvas2d_centroid": [cx_a, cy_a],
        "rust_centroid": [cx_b, cy_b],
        "dx": cx_a - cx_b,
        "dy": cy_a - cy_b,
        "canvas2d_luminance_sum": mass_a,
        "rust_luminance_sum": mass_b,
        "luminance_ratio_min_over_max": mass_ratio,
    }

    return {
        "region_box": [GX, GY, GW, GH],
        "per_channel_max_delta_RGBA": per_channel_max,
        "per_channel_mean_delta_RGBA": per_channel_mean,
        "per_quadrant_mean_delta": per_quadrant_mean,
        "delta_histogram": histogram,
        "center_of_mass": com_delta,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"capturing Canvas2D-WASM single glyph at (100,100)...",
          file=sys.stderr)
    canvas_png = asyncio.run(capture_canvas2d())
    (OUT_DIR / "glyph-canvas2d.png").write_bytes(canvas_png)

    print(f"capturing Rust single glyph via dev Pi...", file=sys.stderr)
    rust_png = capture_rust()
    (OUT_DIR / "glyph-rust.png").write_bytes(rust_png)

    print(f"composing sxs + diff outputs...", file=sys.stderr)
    (OUT_DIR / "glyph-sxs.png").write_bytes(compose_sxs(canvas_png, rust_png))
    (OUT_DIR / "glyph-diff.png").write_bytes(compose_diff(canvas_png, rust_png))

    metrics = diagnostic_metrics(canvas_png, rust_png)
    (OUT_DIR / "glyph-metrics.json").write_text(json.dumps(metrics, indent=2))

    print()
    print(f"  glyph-canvas2d.png: {OUT_DIR / 'glyph-canvas2d.png'}")
    print(f"  glyph-rust.png:     {OUT_DIR / 'glyph-rust.png'}")
    print(f"  glyph-sxs.png:      {OUT_DIR / 'glyph-sxs.png'}")
    print(f"  glyph-diff.png:     {OUT_DIR / 'glyph-diff.png'}")
    print(f"  glyph-metrics.json: {OUT_DIR / 'glyph-metrics.json'}")
    print()
    print(f"Diagnostic metrics (64x64 glyph region around (100,100)):")
    print(f"  per-channel max-delta (R, G, B, A):  {metrics['per_channel_max_delta_RGBA']}")
    print(f"  per-channel mean-delta (R, G, B, A): "
          f"{[f'{m:.3f}' for m in metrics['per_channel_mean_delta_RGBA']]}")
    q = metrics["per_quadrant_mean_delta"]
    print(f"  per-quadrant mean-delta:")
    print(f"    TL={q['TL']:.3f}  TR={q['TR']:.3f}  BL={q['BL']:.3f}  BR={q['BR']:.3f}")
    h = metrics["delta_histogram"]
    total = sum(h.values())
    print(f"  delta histogram (5 buckets, {total} total samples):")
    for k, v in h.items():
        pct = 100.0 * v / total if total else 0.0
        print(f"    delta {k:>6s}:  {v:>8d}  ({pct:5.2f}%)")
    com = metrics["center_of_mass"]
    print(f"  center-of-mass (local to 64x64 region):")
    print(f"    Canvas2D: ({com['canvas2d_centroid'][0]:.3f}, {com['canvas2d_centroid'][1]:.3f})")
    print(f"    Rust:     ({com['rust_centroid'][0]:.3f}, {com['rust_centroid'][1]:.3f})")
    print(f"    delta:    dx={com['dx']:+.3f}  dy={com['dy']:+.3f}")
    print()


if __name__ == "__main__":
    main()
