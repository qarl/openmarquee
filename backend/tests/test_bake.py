"""Smoke test for scripts/bake.py.

Phase 0 of DELETE-PIL (qarl-direct 2026-05-13): bake.py wraps the
Canvas2D parity harness via Playwright so a slide JSON becomes a
PNG without going through the soon-to-be-deleted PIL rasterizer.
The smoke is hardware-light -- runs Playwright + headless Chromium
in-process via the same pattern as scripts/parity/run.py, which
already runs in CI -- but real-Chromium-bound so we skip cleanly
when playwright isn't installed (matching test_parity_harness.py
pattern if/when that lands).
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
BAKE_SCRIPT = REPO / "scripts" / "bake.py"
FIXTURE_UUID = "f0000000-0000-4000-8000-000000000020"  # UNCAGE replica
FIXTURE_JSON = REPO / "renderer" / "tests" / "fixtures" / FIXTURE_UUID / "item.json"


def _png_dims(png_path: Path) -> tuple[int, int]:
    """Parse a PNG's IHDR chunk for width, height without leaning on
    PIL (which is the thing we're deleting)."""
    data = png_path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    # IHDR is the first chunk after the 8-byte signature; its data
    # starts at offset 16 (8 sig + 4 length + 4 'IHDR'). Width and
    # height are the first 8 bytes of the chunk data, big-endian.
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _playwright_installed() -> bool:
    try:
        import playwright.async_api  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    not _playwright_installed(),
    reason="playwright not installed (pip3 install playwright + browser)",
)
@pytest.mark.skipif(
    not FIXTURE_JSON.exists(),
    reason=f"UNCAGE fixture missing at {FIXTURE_JSON}",
)
def test_bake_uncage_fixture_produces_1080p_png(tmp_path: Path) -> None:
    """End-to-end: invoke scripts/bake.py on the UNCAGE parity fixture,
    confirm output is a 1920x1080 PNG with non-empty content. Pins
    the bake CLI contract for Phase 1 (pre-baking the FYS demo reel)
    and Phase 2 (operator-browser bakes on save) to depend on."""
    out_png = tmp_path / "uncage.png"
    result = subprocess.run(
        [sys.executable, str(BAKE_SCRIPT), str(FIXTURE_JSON),
         "--out", str(out_png), "--tick", "0.8"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"bake.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert out_png.exists(), "bake.py exit 0 but no output PNG"
    size = out_png.stat().st_size
    # 1920x1080 RGBA with reasonable content is at minimum ~50 KB
    # compressed (uniform fills); a slide with text + gradient should
    # easily exceed that. The actual UNCAGE bake at tick=0.8 lands
    # around 230 KB. 50 KB is a "definitely non-trivial content"
    # floor that catches all-black / all-transparent regressions.
    assert size > 50_000, f"bake output suspiciously small: {size} bytes"
    width, height = _png_dims(out_png)
    assert (width, height) == (1920, 1080), (
        f"bake output is {width}x{height}, expected 1920x1080"
    )
