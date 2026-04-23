#!/usr/bin/env python3
"""Generate the bundled seed videos via OpenAI's Sora 2 Pro API.

Writes MP4s + paired PNG thumbnails to backend/openmarquee/seed_assets/videos/.
First-boot seed picks them up and registers each as a VideoSlide.

This is a *one-shot maintainer script*, not part of the device's runtime.
Re-run to refresh / extend the shipped set:

    export OPENAI_API_KEY=sk-...
    python3 scripts/generate-seed-videos.py

Prompts live in PRESETS so maintainers can see + edit them. Add or remove
entries to change what a freshly-flashed SD card ships with.

Costs $$ — Sora 2 Pro bills per generation (~5–10× the Sora 2 base cost,
which is itself non-trivial). Re-runs are idempotent (skip when both
{stem}.mp4 and {stem}.png are already present), so resumed runs only
fill gaps instead of paying for the full set every time.

Requires `ffmpeg` on the maintainer's PATH (used for first-frame
thumbnail extraction).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

PRESETS: list[tuple[str, str]] = [
    # (filename stem, prompt). The filename (title-cased) becomes the slide
    # name on the device; keep it short — the saved-slides list wraps ugly
    # long names. Existing bundled clips (sale, open-sign, coffee,
    # happy-hour) were operator-generated via sora.com; new entries here
    # extend the pack without re-generating those.
    (
        "grand-opening",
        "GRAND OPENING — animated gold banner unfurling across the frame "
        "with colorful confetti falling, warm celebratory spotlights, "
        "clean dark background, bold readable letters, cinematic signage "
        "style, 16:9, no clutter.",
    ),
    # Add more presets here. Examples:
    # (
    #     "thank-you",
    #     "Animated cursive THANK YOU in gold script, gentle bokeh lights, "
    #     "warm celebration atmosphere, dark background, 16:9 clean signage.",
    # ),
    # (
    #     "now-hiring",
    #     "Animated NOW HIRING in bright neon yellow on dark brick wall, "
    #     "subtle flicker, modern retail signage, 16:9.",
    # ),
]

API_BASE = "https://api.openai.com/v1"
MODEL = "sora-2-pro"
# sora-2-pro landscape sizes: 1280x720 ($0.30/s), 1792x1024 ($0.50/s),
# 1920x1080 ($0.50/s). 1080p matches the Pi Zero 2 W's H.264 decoder
# cap exactly — at the seed-asset spend level the extra $0.20/s is
# worth the headroom and clarity. (sora-2 base only supports up to
# 1280x720; pro is required for 1080p.)
SIZE = "1920x1080"
SECONDS = "8"        # supported: "4", "8", "12"
POLL_INTERVAL_SECONDS = 15
POLL_TIMEOUT_SECONDS = 900   # 15-minute ceiling per generation


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("ERROR: set OPENAI_API_KEY before running.", file=sys.stderr)
        sys.exit(2)
    return key


def _generate_one(stem: str, prompt: str, dest_mp4: Path, dest_png: Path) -> None:
    """Submit a generation, poll until completion, save MP4 + thumbnail."""
    headers = {"Authorization": f"Bearer {_api_key()}"}
    body = {
        "model": MODEL,
        "prompt": prompt,
        "size": SIZE,
        "seconds": SECONDS,
    }

    print(f"  [{stem}] POST /videos…", flush=True)
    r = httpx.post(f"{API_BASE}/videos", json=body, headers=headers, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"POST /videos {r.status_code}: {r.text[:300]}")
    job = r.json()
    vid = job["id"]
    print(f"  [{stem}] job {vid} status={job.get('status')}", flush=True)

    start = time.time()
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        r = httpx.get(f"{API_BASE}/videos/{vid}", headers=headers, timeout=30)
        job = r.json()
        elapsed = int(time.time() - start)
        status = job.get("status")
        progress = job.get("progress")
        print(f"  [{stem}] {elapsed}s status={status} progress={progress}", flush=True)
        if status == "completed":
            break
        if status in ("failed", "cancelled"):
            raise RuntimeError(f"[{stem}] generation {status}: {job}")
        if elapsed > POLL_TIMEOUT_SECONDS:
            raise RuntimeError(f"[{stem}] timeout after {POLL_TIMEOUT_SECONDS}s")

    print(f"  [{stem}] GET /videos/{{id}}/content…", flush=True)
    r = httpx.get(
        f"{API_BASE}/videos/{vid}/content", headers=headers, timeout=180
    )
    if r.status_code >= 300:
        raise RuntimeError(f"GET content {r.status_code}: {r.text[:300]}")
    dest_mp4.write_bytes(r.content)
    print(f"  [{stem}] wrote {dest_mp4.name} ({len(r.content)} bytes)")

    # Extract a first-frame thumbnail at t=2s (avoids fade-in black frames).
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(dest_mp4),
            "-ss", "00:00:02", "-vframes", "1", "-update", "1",
            str(dest_png),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"  [{stem}] wrote {dest_png.name}")


def main() -> int:
    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg not on PATH (needed for thumbnail extraction).",
              file=sys.stderr)
        return 2

    project_root = Path(__file__).resolve().parent.parent
    dest_dir = project_root / "backend/openmarquee/seed_assets/videos"
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"writing to {dest_dir}")
    for stem, prompt in PRESETS:
        mp4_path = dest_dir / f"{stem}.mp4"
        png_path = dest_dir / f"{stem}.png"
        if (
            mp4_path.exists() and mp4_path.stat().st_size > 1024
            and png_path.exists() and png_path.stat().st_size > 1024
        ):
            print(f"  [{stem}] already present; skipping")
            continue
        try:
            _generate_one(stem, prompt, mp4_path, png_path)
        except Exception as exc:
            # Best-effort: log + continue so a transient failure on one
            # preset doesn't block the rest of the pack.
            print(f"  [{stem}] FAILED: {exc}", file=sys.stderr)
            continue

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
