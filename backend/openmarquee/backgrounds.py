"""OpenAI-powered background image generation for the composer.

Contract:

- Client POSTs a prompt to /api/backgrounds/generate.
- If OPENAI_API_KEY is unset, the route returns 503 with a clear message
  — this keeps the feature *discoverable* without making the captive
  portal unusable for operators who don't have / don't want an API key.
- If the key is set, we call the Images API, downscale the returned PNG
  to the device's display dimensions (same as any other ImageSlide), and
  persist it via the existing content storage.

The actual HTTP call is isolated in `generate_png_via_openai` so tests
can monkeypatch it without a network round-trip. Timeout is generous
(60s) because the model can take a while on first-token and the
captive-portal operator has no other work to do while they wait.
"""

from __future__ import annotations

import base64
import io
import logging

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
# gpt-image-1 is the current Images API model. Size 1024x1024 is the
# smallest square it offers; we scale down to the device's display
# dimensions on save, so generating at higher resolution just gives us
# more headroom for crisp results on HDMI while costing roughly the
# same as smaller requests.
OPENAI_MODEL = "gpt-image-1"
OPENAI_SIZE = "1024x1024"
OPENAI_TIMEOUT_SECONDS = 60.0


class OpenAIError(RuntimeError):
    """Signals the Images API itself rejected or errored on the request.

    Separate from `OpenAINotConfigured` — this one means we *tried* and
    the API said no; the route maps it to 502 with the detail from
    OpenAI so operators see the actual problem (content policy, quota,
    etc.) rather than a generic 500.
    """


class OpenAINotConfigured(RuntimeError):
    """OPENAI_API_KEY isn't set. The route maps this to 503 — the
    feature exists, it's just not turned on for this device."""


def generate_png_via_openai(prompt: str, api_key: str) -> bytes:
    """Call OpenAI Images and return the raw PNG bytes.

    Raises `OpenAIError` on any non-2xx or connectivity failure. Isolated
    here so tests can monkeypatch `backgrounds.generate_png_via_openai`
    to bypass the real HTTP call.
    """
    payload = {
        "model": OPENAI_MODEL,
        "prompt": prompt,
        "size": OPENAI_SIZE,
        "n": 1,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = httpx.post(
            OPENAI_IMAGES_URL,
            json=payload,
            headers=headers,
            timeout=OPENAI_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise OpenAIError(f"network failure talking to OpenAI: {exc}") from exc

    if response.status_code != 200:
        # Surface the API's own error detail so operators know if it's a
        # content-policy rejection, quota exhausted, wrong key, etc.
        try:
            body = response.json()
            detail = body.get("error", {}).get("message") or str(body)
        except Exception:
            detail = response.text
        raise OpenAIError(f"OpenAI {response.status_code}: {detail}")

    try:
        body = response.json()
        b64 = body["data"][0]["b64_json"]
    except (KeyError, IndexError, ValueError) as exc:
        raise OpenAIError(f"unexpected OpenAI response shape: {exc}") from exc

    try:
        return base64.b64decode(b64, validate=True)
    except ValueError as exc:
        raise OpenAIError(f"OpenAI returned invalid base64: {exc}") from exc


def downscale_to_panel(png: bytes, width: int, height: int) -> bytes:
    """Letterbox-fit the generated image onto a `width` × `height` canvas.

    The model returns 1024×1024 (square); panels are rarely square. We
    preserve aspect ratio with a black-letterboxed fit so the shipped
    asset matches how any other ImageSlide would be scaled client-side
    before upload.
    """
    src = Image.open(io.BytesIO(png))
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
