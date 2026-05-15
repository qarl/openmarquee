#!/usr/bin/env python3
"""Cross-renderer parity test driver.

Captures the browser preview (Canvas2D / rasterize.js) via Playwright +
Chromium, then diffs each capture against the corresponding
renderer/tests/golden/<name>.png (which render_tests.sh's GREEN gate
keeps in sync with the Rust path).

See `qa/cross-renderer-parity-design.md` for the design.

Usage:
  scripts/parity_tests.sh            # capture both, diff, report.
  scripts/parity_tests.sh --bless    # save browser PNGs to
                                     # renderer/tests/parity/baseline/
                                     # so subsequent runs diff against
                                     # those instead of the Rust goldens
                                     # (use when intentionally widening
                                     # the parity gap pending a fix).

Threshold defaults (per fixture override in fixtures.json):
  ssim_min       0.95   structural similarity must exceed this.
  max_delta_max  50     max per-channel L1 must stay under this.
Mean-delta and %-pixels-with-delta>10 are reported as informational
columns -- not gating, just visibility into "lots of small drift".

Why not capture the Rust side fresh? render_tests.sh's existing gate
already enforces "checked-in goldens match current Rust output." That
means renderer/tests/golden/*.png IS the canonical Rust render for
HEAD. Driving render_tests.sh from here would just re-derive the same
PNGs over ssh -- not worth the latency at parity-test time. If a
golden gets re-blessed, parity diffs the new baseline on the next
run. The design doc captured this as a deviation from "Rust capture
stays unchanged" (it does -- we just use its persisted output).
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

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES_JSON = Path(__file__).resolve().parent / "fixtures.json"
FIXTURE_DIR = REPO / "renderer" / "tests" / "fixtures"
GOLDEN_DIR = REPO / "renderer" / "tests" / "golden"
CAPTURE_DIR = REPO / "renderer" / "tests" / "parity" / "captures"
BASELINE_DIR = REPO / "renderer" / "tests" / "parity" / "baseline"
HARNESS_HTML = REPO / "ui" / "parity-harness.html"


def _import_third_party():
    """Import scikit-image + PIL + playwright. Surface a friendly
    error if any are missing; the harness is dev-only and the deps
    are optional."""
    try:
        from PIL import Image  # noqa: F401
        from skimage.metrics import structural_similarity  # noqa: F401
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError as exc:
        sys.stderr.write(
            "FAIL: parity harness needs PIL / scikit-image / playwright.\n"
            f"      missing: {exc.name}\n"
            "      install: pip3 install Pillow scikit-image playwright "
            "&& playwright install chromium\n"
        )
        sys.exit(2)


def load_fixtures():
    spec = json.loads(FIXTURES_JSON.read_text())
    defaults = spec.get("defaults", {})
    out = []
    for f in spec["fixtures"]:
        merged = {**defaults, **f}
        out.append(merged)
    return out


def load_item(uuid: str) -> dict:
    path = FIXTURE_DIR / uuid / "item.json"
    if not path.exists():
        raise FileNotFoundError(f"fixture item.json missing: {path}")
    blob = json.loads(path.read_text())
    # Wire envelope is { schema_version, updated_at, item }. The harness
    # only needs the inner item.
    return blob["item"] if "item" in blob else blob


def _start_static_server(doc_root: Path) -> tuple[socketserver.TCPServer, int]:
    """ES-module imports from file:// hit CORS in headless Chromium.
    Serve REPO root over a localhost HTTP server on a free port so the
    harness page can resolve `./src/rasterize.js` AND the wasm-renderer
    module's `../../renderer-wasm/pkg/renderer_wasm.js` import (which
    escapes ui/ into REPO). Mirrors boot_sxs.py's docroot choice."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(doc_root),
    )
    # Bind to 0 to grab any free port.
    server = socketserver.TCPServer(("127.0.0.1", 0), handler, bind_and_activate=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


async def capture_browser(playwright, fixtures, capture_dir: Path):
    """Launch Chromium, navigate to parity-harness.html, capture each
    fixture's browser PNG into capture_dir."""
    server, port = _start_static_server(REPO)
    try:
        return await _capture_with_server(playwright, fixtures, capture_dir, port)
    finally:
        server.shutdown()
        server.server_close()


async def _capture_with_server(playwright, fixtures, capture_dir: Path, port: int):
    browser = await playwright.chromium.launch()
    context = await browser.new_context(viewport={"width": 1920, "height": 1080})
    page = await context.new_page()
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
    # The harness sets innerHTML on #parity-status when ready; wait for
    # that signal so we know the module-import + font-warmup completed.
    try:
        await page.wait_for_function(
            "document.getElementById('parity-status')"
            ".textContent.includes('ready')",
            timeout=10_000,
        )
    except Exception:
        status_text = await page.evaluate(
            "document.getElementById('parity-status')?.textContent || '(missing)'",
        )
        sys.stderr.write(f"  parity-status final state: {status_text!r}\n")
        raise
    capture_dir.mkdir(parents=True, exist_ok=True)
    captured = []
    for fx in fixtures:
        if fx["kind"] == "single":
            item = load_item(fx["uuid"])
            params = {"kind": "single", "item": item, "tick": fx["tick"]}
        elif fx["kind"] == "transition_mid":
            from_item = load_item(fx["from_uuid"])
            to_item = load_item(fx["to_uuid"])
            params = {
                "kind": "transition_mid",
                "transition": fx["transition"],
                "fromItem": from_item,
                "toItem": to_item,
                "transitionT": fx["transition_t"],
            }
        else:
            raise ValueError(f"unknown fixture kind: {fx['kind']}")
        b64 = await page.evaluate("(p) => window.__parityCapture(p)", params)
        out_path = capture_dir / f"{fx['name']}.browser.png"
        out_path.write_bytes(base64.b64decode(b64))
        captured.append((fx, out_path))
    await context.close()
    await browser.close()
    return captured


def diff(browser_png: Path, golden_png: Path) -> dict:
    """Compute SSIM + per-pixel delta metrics. Resizes the browser
    capture to the golden's size if needed -- the Rust goldens are
    1920×1080 (vc4 forced mode) and the browser canvas is 1920×1080
    by design, so this is normally a no-op but keeps the diff robust
    to harness DPR weirdness on retina hosts."""
    from PIL import Image
    import numpy as np
    from skimage.metrics import structural_similarity

    browser = Image.open(browser_png).convert("RGB")
    golden = Image.open(golden_png).convert("RGB")
    if browser.size != golden.size:
        browser = browser.resize(golden.size, Image.LANCZOS)
    b = np.asarray(browser, dtype=np.int16)
    g = np.asarray(golden, dtype=np.int16)
    delta = np.abs(b - g)  # per-pixel per-channel L1
    max_delta = int(delta.max())
    mean_delta = float(delta.mean())
    # %-pixels with ANY channel delta > 10. Matches render_diff.py's
    # tolerance window so the two harnesses share a "noticeable drift"
    # definition.
    any_channel_over_10 = (delta > 10).any(axis=2)
    pct_over_10 = float(any_channel_over_10.mean() * 100.0)
    # SSIM on luminance (channel_axis=2 means treat each channel
    # independently and average). Pinning data_range=255 so the score
    # is comparable across uint8 / float inputs.
    ssim = float(
        structural_similarity(
            np.asarray(browser),
            np.asarray(golden),
            channel_axis=2,
            data_range=255,
        )
    )
    return {
        "ssim": ssim,
        "max_delta": max_delta,
        "mean_delta": mean_delta,
        "pct_pixels_over_10": pct_over_10,
        "size": browser.size,
    }


def report(fx, golden_path: Path, metrics: dict) -> tuple[bool, str]:
    ssim_min = fx["ssim_min"]
    max_delta_max = fx["max_delta_max"]
    is_pass = (
        metrics["ssim"] >= ssim_min
        and metrics["max_delta"] <= max_delta_max
    )
    verdict = "PASS" if is_pass else "FAIL"
    summary = (
        f"{verdict}: {fx['name']:30s} "
        f"SSIM={metrics['ssim']:.4f} (>={ssim_min}) "
        f"max_delta={metrics['max_delta']:3d} (<={max_delta_max}) "
        f"mean_delta={metrics['mean_delta']:6.3f} "
        f"pixels_over_10={metrics['pct_pixels_over_10']:5.2f}% "
        f"vs golden/{golden_path.name}"
    )
    return is_pass, summary


async def main_async(args) -> int:
    _import_third_party()
    from playwright.async_api import async_playwright

    fixtures = load_fixtures()
    capture_dir = BASELINE_DIR if args.bless else CAPTURE_DIR
    async with async_playwright() as pw:
        captured = await capture_browser(pw, fixtures, capture_dir)

    if args.bless:
        print(f"BLESS: wrote {len(captured)} browser captures to {capture_dir}")
        return 0

    all_pass = True
    metrics_per_fixture = {}
    for fx, browser_png in captured:
        golden_path = GOLDEN_DIR / f"{fx['golden']}.png"
        if not golden_path.exists():
            print(f"FAIL: {fx['name']:30s} golden missing: {golden_path}")
            all_pass = False
            continue
        m = diff(browser_png, golden_path)
        is_pass, summary = report(fx, golden_path, m)
        print(summary)
        metrics_per_fixture[fx["name"]] = m
        if not is_pass:
            all_pass = False

    # Persist metrics so commit messages / drift dashboards can pull
    # them without re-running the harness. Lives alongside the
    # captures (CAPTURE_DIR on non-bless runs; --bless early-returns
    # above so we never reach here in bless mode).
    metrics_path = capture_dir / "metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics_per_fixture, indent=2))
    print(f"\nmetrics written to {metrics_path}")

    if args.report_only:
        # Soft mode: report drift to stdout + write metrics.json but
        # always exit 0 so the surrounding pipeline doesn't fail.
        return 0
    return 0 if all_pass else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--bless",
        action="store_true",
        help="Save browser captures as the new baseline (skip diff).",
    )
    # qarl-direct 2026-05-13: hard-gate is the default mode -- the
    # script exits non-zero when ANY fixture fails its threshold so
    # CI / deploy pipelines actually block on parity drift. --report-only
    # flips to soft mode (always exit 0) for the rare "I'm working
    # the diff down, don't fail my dev box yet" workflow. Keep the
    # default hard; reports without consequences breed entropy.
    ap.add_argument(
        "--report-only",
        action="store_true",
        help="Report drift but always exit 0 (soft mode). Default is "
        "hard-gate: exit non-zero on any per-fixture threshold miss.",
    )
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
