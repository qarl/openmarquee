"""AI-backed background image generation for the composer.

Provider-pluggable: the default is `pollinations` (Pollinations.ai — free,
no API key required, Flux/SDXL-backed). Additional providers can be added
to `PROVIDERS` without changing the API route. Operators pick one via the
env var `OPENMARQUEE_IMAGEGEN_PROVIDER`, or per-request via the optional
`provider` field in the POST body.

Contract:
- Client POSTs a prompt (optionally a provider name) to
  `/api/backgrounds/generate`.
- The selected provider's `generate(prompt)` runs; on success we downscale
  the returned image to the device's display dimensions (letterbox-fit)
  and persist it via the existing content storage.
- `generate` can raise `BackgroundGenError`; the route maps it to 502 with
  the underlying message so operators see the real failure.
- `BackgroundProviderUnknown` maps to 400 — the request named a provider
  we don't ship.

What's explicitly NOT here: paid / API-key-gated services. openMarquee is
a free, offline-first captive portal; making the shipped composer depend
on a paid API would be out of character. Provisioning API keys for users
who want to bring their own is a separate feature the device doesn't run
today.
"""

from __future__ import annotations

import io
import logging
import os
import urllib.parse
from dataclasses import dataclass
from typing import Protocol

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0


class BackgroundGenError(RuntimeError):
    """The provider itself rejected / errored. Route → 502."""


class BackgroundProviderUnknown(LookupError):
    """Caller asked for a provider we don't ship. Route → 400."""


class ImageGenProvider(Protocol):
    name: str

    def generate(self, prompt: str) -> bytes:
        """Return raw image bytes (PNG or JPEG — PIL auto-detects)."""


@dataclass
class PollinationsProvider:
    """Pollinations.ai — free, no API key. Flux-backed.

    The prompt goes in the URL path (URL-encoded); width/height/nologo in
    query params. Response body is the raw image bytes (JPEG or PNG depending
    on the upstream model's pipeline). Typical latency is 10-20 seconds.
    """

    name: str = "pollinations"
    base_url: str = "https://image.pollinations.ai/prompt"
    generate_width: int = 1024
    generate_height: int = 1024
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS

    def generate(self, prompt: str) -> bytes:
        encoded = urllib.parse.quote(prompt, safe="")
        url = f"{self.base_url}/{encoded}"
        params = {
            "width": self.generate_width,
            "height": self.generate_height,
            # nologo=true suppresses the Pollinations watermark.
            "nologo": "true",
        }
        try:
            response = httpx.get(url, params=params, timeout=self.timeout_seconds)
        except httpx.HTTPError as exc:
            raise BackgroundGenError(f"network failure talking to pollinations.ai: {exc}") from exc
        if response.status_code != 200:
            raise BackgroundGenError(
                f"pollinations.ai {response.status_code}: {response.text[:200]}"
            )
        body = response.content
        if not body:
            raise BackgroundGenError("pollinations.ai returned an empty body")
        return body


# Registry of shipped providers. Add new entries here; the route handler
# + the UI both read this list so adding a provider is one edit.
PROVIDERS: dict[str, ImageGenProvider] = {
    "pollinations": PollinationsProvider(),
}


def default_provider_name() -> str:
    return os.environ.get("OPENMARQUEE_IMAGEGEN_PROVIDER", "pollinations")


def resolve_provider(name: str | None) -> ImageGenProvider:
    """Look a provider up by name. Raises BackgroundProviderUnknown on miss."""
    resolved = name or default_provider_name()
    try:
        return PROVIDERS[resolved]
    except KeyError as exc:
        raise BackgroundProviderUnknown(
            f"no image-gen provider named {resolved!r}. Known: {sorted(PROVIDERS)}"
        ) from exc


def downscale_to_panel(image_bytes: bytes, width: int, height: int) -> bytes:
    """Letterbox-fit any image bytes onto a `width` × `height` canvas.

    The provider returns a square (Pollinations defaults to 1024×1024); panels
    are rarely square. We preserve aspect ratio with a black-letterboxed fit
    so the shipped asset matches how any other ImageSlide would be scaled
    client-side before upload.
    """
    src = Image.open(io.BytesIO(image_bytes))
    src.load()
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    scale = min(width / src.width, height / src.height)
    new_w = max(1, round(src.width * scale))
    new_h = max(1, round(src.height * scale))
    resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
    off_x = (width - new_w) // 2
    off_y = (height - new_h) // 2
    canvas.paste(resized, (off_x, off_y))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
