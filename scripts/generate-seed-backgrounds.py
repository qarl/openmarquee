#!/usr/bin/env python3
"""Generate the bundled seed backgrounds via Pollinations.ai.

Writes JPEGs to backend/openmarquee/seed_assets/backgrounds/. First-boot
seed picks those up and registers each as an ImageSlide.

This is a *one-shot maintainer script*, not part of the device's runtime.
Re-run to refresh the shipped set:

    python3 scripts/generate-seed-backgrounds.py

Prompts live here so maintainers can see + edit them. Add or remove
entries to change what a freshly-flashed SD card ships with.
"""

from __future__ import annotations

import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PRESETS: list[tuple[str, str]] = [
    # (filename stem, prompt). The filename (title-cased) becomes the
    # slide name on the device; keep it short — the saved-slides list
    # wraps ugly long names.
    (
        "parchment",
        "aged parchment paper texture, cream and beige, subtle torn edges, "
        "minimal, signage-friendly background",
    ),
    (
        "sunset-gradient",
        "soft pastel gradient, coral and peach into warm yellow, minimal, "
        "signage-friendly background",
    ),
    (
        "brick-wall",
        "weathered red brick wall, warm lighting, subtle texture, "
        "signage-friendly background",
    ),
    (
        "kraft-paper",
        "kraft paper, natural brown with subtle fiber grain, minimal, "
        "signage-friendly background",
    ),
    (
        "stained-glass",
        "art nouveau stained glass pattern, jewel tones, elegant geometric, "
        "signage-friendly background",
    ),
    (
        "oak-wood",
        "warm oak wood grain, subtle texture, natural tones, "
        "signage-friendly background",
    ),
    (
        "midnight",
        "deep navy gradient into black, minimal, signage-friendly background",
    ),
    (
        "chalkboard",
        "dark green chalkboard texture, subtle chalk dust, minimal, "
        "signage-friendly background",
    ),
    (
        "teal-pastel",
        "soft pastel gradient, mint teal and rose, minimal, "
        "signage-friendly background",
    ),
    (
        "marble",
        "white and gray marble texture, elegant veins, subtle, "
        "signage-friendly background",
    ),
]

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
# 4K = the cap on what a Pi Zero 2 W can comfortably resize per slide
# entry, and the same target the text-slide rasterizer uses. Storing
# at 4K means panel resolution changes are a non-event — playback
# cover-fits down to whatever the device is.
WIDTH = 3840
HEIGHT = 2160
TIMEOUT_SECONDS = 180


def _fetch_with_retry(url: str, attempts: int = 4) -> bytes | None:
    """Pollinations occasionally 429s or 502s under load — back off and retry."""
    import urllib.error

    delay = 10.0
    for i in range(attempts):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "openmarquee-seed-generator/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 502, 503, 504) and i < attempts - 1:
                print(
                    f"    upstream {exc.code}; retrying in {delay:.0f}s…",
                    flush=True,
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return None


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    dest = project_root / "backend/openmarquee/seed_assets/backgrounds"
    dest.mkdir(parents=True, exist_ok=True)

    print(f"writing to {dest}")
    for stem, prompt in PRESETS:
        out_path = dest / f"{stem}.png"
        if out_path.exists() and out_path.stat().st_size > 1024:
            # Idempotent: skip what we already have so resumed runs just
            # fill gaps instead of re-paying the generation latency.
            print(f"  [{stem}] already present; skipping")
            continue
        url = (
            f"{POLLINATIONS_BASE}/{urllib.parse.quote(prompt, safe='')}"
            f"?width={WIDTH}&height={HEIGHT}&nologo=true&format=png"
        )
        print(f"  [{stem}] fetching…", flush=True)
        t0 = time.time()
        try:
            body = _fetch_with_retry(url)
        except Exception as exc:
            print(f"  [{stem}] FAILED: {exc}", file=sys.stderr)
            continue
        if not body or len(body) < 1024:
            print(
                f"  [{stem}] suspiciously small ({len(body) if body else 0} bytes); "
                f"upstream probably errored — skipping",
                file=sys.stderr,
            )
            continue
        out_path.write_bytes(body)
        print(f"  [{stem}] {len(body)} bytes in {time.time() - t0:.1f}s")

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
