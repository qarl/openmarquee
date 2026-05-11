"""Concurrency tests for autosave round-tripping (sweep #3 #11).

The UI's auto-save fires PUT /api/content/text-slides/{id} on a
600ms debounce. Two operators editing the same slide (or one
operator's UI firing two saves in quick succession) can race --
both PUTs reach the server within the same event-loop tick. The
load-bearing property: the final on-disk state matches ONE of the
two payloads, not a half-merged hybrid.

ContentStorage.save uses atomic file rename (tmp.write + replace),
so two concurrent saves both complete cleanly; the second-to-
replace wins. This test pins that behavior + asserts no partial
state is visible.
"""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import get_content_storage, get_playlist_storage
from openmarquee.playlist import PlaylistStorage


def _real_png() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.mark.asyncio
async def test_concurrent_text_slide_puts_final_state_matches_one_payload(
    tmp_path: Path,
):
    """Two PUTs to the same content_id with different `name` values
    fired via asyncio.gather. The final on-disk state must equal
    exactly one of the two payloads -- never a half-merged hybrid
    where, e.g., the name is from payload A but text_layers is from
    payload B."""
    content_storage = ContentStorage(tmp_path / "content")
    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    app.dependency_overrides[get_content_storage] = lambda: content_storage
    app.dependency_overrides[get_playlist_storage] = lambda: playlist_storage
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            # Create the slide first.
            png = _real_png()
            create_resp = await client.post(
                "/api/content/text-slides",
                json={
                    "name": "v0",
                    "duration_ms": 5000,
                    "text_layers": [{"text": "original",
                                     "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
                    "png_base64": png,
                    "background_color": "#000000",
                },
            )
            assert create_resp.status_code in (200, 201)
            slide_id = create_resp.json()["id"]

            # Two concurrent PUTs with distinct names + text bodies.
            payload_a = {
                "name": "from-A",
                "duration_ms": 5000,
                "text_layers": [{"text": "AAAA",
                                 "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
                "background_color": "#000000",
                "png_base64": png,
            }
            payload_b = {
                "name": "from-B",
                "duration_ms": 5000,
                "text_layers": [{"text": "BBBB",
                                 "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
                "background_color": "#000000",
                "png_base64": png,
            }
            results = await asyncio.gather(
                client.put(f"/api/content/text-slides/{slide_id}",
                           json=payload_a),
                client.put(f"/api/content/text-slides/{slide_id}",
                           json=payload_b),
            )
            for r in results:
                assert r.status_code == 200

            # Read the final state. Must match ONE of A or B
            # consistently across name + text_layers[0].text -- a
            # half-merged state (name from one, text from the
            # other) would mean save_text_slide isn't fully atomic.
            final = (await client.get(
                f"/api/content/{slide_id}",
            )).json()
            final_name = final["name"]
            final_text = final["text_layers"][0]["text"]
            assert final_name in ("from-A", "from-B")
            if final_name == "from-A":
                assert final_text == "AAAA"
            else:
                assert final_text == "BBBB"
    finally:
        app.dependency_overrides.clear()
