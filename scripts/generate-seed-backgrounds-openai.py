#!/usr/bin/env python3
"""Generate bundled seed backgrounds via OpenAI's gpt-image-1.

Higher-quality alternative to generate-seed-backgrounds.py (which uses
the free Pollinations.ai endpoint). Both scripts write to the same
destination — backend/openmarquee/seed_assets/images/ — so you
pick whichever provider matches the prompts + budget you want for a
given generation round.

This is a *one-shot maintainer script*, not part of the device's runtime.

    export OPENAI_API_KEY=sk-...
    python3 scripts/generate-seed-backgrounds-openai.py

Costs $$ — gpt-image-1 bills per image (high-quality 1536×1024 is on the
order of $0.05–0.20 per call; check OpenAI's current pricing). Re-runs
are idempotent (skip when {stem}.png is already present), so resumed
runs only fill gaps.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import httpx

PRESETS: list[tuple[str, str]] = [
    # (filename stem, prompt). The filename (title-cased) becomes the slide
    # name on the device; keep it short — the saved-slides list wraps ugly
    # long names. Stems should match generate-seed-backgrounds.py so
    # re-running either script slots bytes into the same named slide.
    # Mirrors the existing Pollinations prompt set; edit freely to refresh
    # the shipped look.
    (
        "parchment",
        "aged parchment paper texture, cream and beige, subtle torn "
        "edges, minimal, signage-friendly background, no text",
    ),
    (
        "sunset-gradient",
        "soft pastel gradient, coral and peach into warm yellow, "
        "minimal, signage-friendly background, no text",
    ),
    (
        "brick-wall",
        "weathered red brick wall, flat even lighting edge-to-edge, "
        "no vignette, no spotlight, no shadows, repeating brick pattern, "
        "signage-friendly background, no text, no focal point",
    ),
    (
        "kraft-paper",
        "kraft paper, natural brown with subtle fiber grain, minimal, "
        "signage-friendly background, no text",
    ),
    (
        "stained-glass",
        "stained glass texture, repeating small geometric panes in "
        "jewel tones, edge-to-edge flat lighting, no frame, no center "
        "medallion, no focal point, signage-friendly background, no text",
    ),
    (
        "oak-wood",
        "warm oak wood grain, subtle texture, natural tones, "
        "signage-friendly background, no text",
    ),
    (
        "midnight",
        "deep navy gradient into black, minimal, signage-friendly "
        "background, no text",
    ),
    (
        "chalkboard",
        "dark green chalkboard texture, subtle chalk dust, minimal, "
        "signage-friendly background, no text",
    ),
    (
        "marble",
        "white and gray marble texture, elegant veins, subtle, "
        "signage-friendly background, no text",
    ),
]

API_BASE = "https://api.openai.com/v1"
MODEL = "gpt-image-1"
# gpt-image-1 supported sizes: 1024x1024, 1024x1536, 1536x1024, auto.
# 1536×1024 is the widest; playback cover-fits to whatever the panel is.
SIZE = "1536x1024"
QUALITY = "high"   # "high" | "medium" | "low" — "high" is worth it for signage.
TIMEOUT_SECONDS = 180


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("ERROR: set OPENAI_API_KEY before running.", file=sys.stderr)
        sys.exit(2)
    return key


def _generate_one(stem: str, prompt: str, dest: Path) -> None:
    """Hit /v1/images/generations and write the returned PNG to `dest`."""
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL,
        "prompt": prompt,
        "size": SIZE,
        "quality": QUALITY,
        "n": 1,
    }
    r = httpx.post(
        f"{API_BASE}/images/generations",
        json=body,
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    payload = r.json()
    # gpt-image-1 returns b64_json by default (no `response_format` needed).
    b64 = payload["data"][0]["b64_json"]
    dest.write_bytes(base64.b64decode(b64))


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    dest_dir = project_root / "backend/openmarquee/seed_assets/images"
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"writing to {dest_dir}")
    for stem, prompt in PRESETS:
        out_path = dest_dir / f"{stem}.png"
        if out_path.exists() and out_path.stat().st_size > 1024:
            print(f"  [{stem}] already present; skipping")
            continue
        print(f"  [{stem}] fetching…", flush=True)
        try:
            _generate_one(stem, prompt, out_path)
        except Exception as exc:
            print(f"  [{stem}] FAILED: {exc}", file=sys.stderr)
            continue
        print(f"  [{stem}] wrote {out_path.stat().st_size} bytes")

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
