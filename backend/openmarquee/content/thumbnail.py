"""On-demand small-JPEG thumbnail generation for the /api/content/{id}/
thumbnail endpoint.

Motivation (2026-07-02, qarl handover-blocker): opening the dashboard
content view fires N concurrent `/api/content/{id}/asset` requests,
each returning a full-resolution lossless 1280x720 PNG (~1-3 MB from
`ui/src/video-upload.js`'s `canvas.toDataURL("image/png")`). On the
memory-tight Pi Zero 2 W (512 MB) ~15 tiles OOM-rebooted the sign.
This module downscales the stored asset.png to a small (256 px wide)
JPEG so the list-view tile fetches stay ~15-40 KB each.

Design:
  * Pure function `generate_thumbnail_jpeg(png_path, mtime_ns) -> bytes`.
    Test-friendly: no side effects other than the in-process LRU cache.
  * LRU cache keyed on `(str(png_path), mtime_ns)` so a re-upload
    (which updates mtime) evicts the stale bytes without any explicit
    invalidation call.
  * mtime_ns is passed IN (not read inside) so the endpoint can stat
    once + use the same nanosecond value for both the cache key and
    any downstream ETag/Last-Modified header (added later).
  * Aspect-preserving: the downscale keeps the PNG's original aspect
    ratio (tile CSS's `object-fit: cover` handles the trim).
  * JPEG quality 78 + progressive: balances size vs perceptual
    quality on a card thumbnail; captured baseline sub-40 KB in
    unit tests.
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image

# Target thumbnail width. 256 px matches the largest CSS tile size any
# view renders at 2x DPR (the dashboard's `.slide-browser-tile-thumb`
# is 128 px wide; retina Chrome asks for 2x). Going smaller would
# visibly soften on retina; going larger wastes bytes.
_THUMBNAIL_WIDTH_PX = 256
# JPEG encode params. progressive=True hides first-scan bytes into a
# low-res preview so a slow network shows the tile faster.
_JPEG_QUALITY = 78


@lru_cache(maxsize=256)
def _cached_thumbnail_bytes(path_str: str, mtime_ns: int) -> bytes:
    """LRU-cached JPEG bytes keyed on (path, mtime_ns). Public
    entrypoint below owns the tuple assembly so the cache key stays
    consistent."""
    del mtime_ns  # signature-only: cache-key participant, not read.
    path = Path(path_str)
    with Image.open(path) as img:
        # Force load before entering the resize; some Pillow decoders
        # defer + then break under the context-manager exit.
        img.load()
        orig_w, orig_h = img.size
        if orig_w <= _THUMBNAIL_WIDTH_PX:
            # Already thumbnail-sized; downscale would be a no-op but
            # still needs the JPEG re-encode below.
            resized = img
        else:
            target_h = max(1, round(_THUMBNAIL_WIDTH_PX * orig_h / orig_w))
            resized = img.resize((_THUMBNAIL_WIDTH_PX, target_h), resample=Image.Resampling.LANCZOS)
        # JPEG doesn't grok RGBA; flatten to RGB before encoding.
        if resized.mode != "RGB":
            resized = resized.convert("RGB")
        buf = io.BytesIO()
        resized.save(
            buf,
            format="JPEG",
            quality=_JPEG_QUALITY,
            progressive=True,
            optimize=True,
        )
        return buf.getvalue()


def generate_thumbnail_jpeg(png_path: Path, *, mtime_ns: int) -> bytes:
    """Return small JPEG thumbnail bytes for the given asset PNG,
    caching by (path, mtime) so a re-upload transparently invalidates
    the previous encode.

    Raises whatever Pillow raises for unreadable input (broken PNG,
    truncated write, unknown format). The caller (api.get_thumbnail)
    catches + downgrades to a 500 with a warn log.
    """
    return _cached_thumbnail_bytes(str(png_path), int(mtime_ns))


def _clear_cache_for_tests() -> None:
    """Test helper: drop the LRU cache so a within-process
    `mtime_ns` collision (writing two different fixtures in <1 ns
    on tmpfs) doesn't produce a false-positive cache hit."""
    _cached_thumbnail_bytes.cache_clear()
