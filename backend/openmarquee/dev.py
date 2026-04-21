"""Dev-time preview tooling.

Exposes a tiny HTML page that polls the MockRenderer's latest frame, plus a
POST /dev/play/{id} endpoint that decodes a stored content item's asset and
pushes it through the MockRenderer. End-to-end: upload via /api/content,
trigger via /dev/play/{id}, see it in the browser at /dev/preview.

Lives under its own router so production builds can drop it later if we
decide we want to. For now it's always mounted — the device is its own
captive-portal AP with no inbound internet, so the attack surface is the
same as any other endpoint.
"""

import io
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from PIL import Image, UnidentifiedImageError

from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import get_content_storage, get_mock_renderer
from openmarquee.rendering.mock import MockRenderer

router = APIRouter(prefix="/dev", tags=["dev"])

StorageDep = Annotated[ContentStorage, Depends(get_content_storage)]
RendererDep = Annotated[MockRenderer, Depends(get_mock_renderer)]

_PREVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>OpenMarquee — dev preview</title>
    <style>
        body {
            background: #111;
            color: #ccc;
            font-family: system-ui, sans-serif;
            margin: 0;
            padding: 2rem;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 1rem;
        }
        h1 { font-weight: normal; font-size: 0.9rem; opacity: 0.6; margin: 0; }
        #frame {
            background: #000;
            border: 1px solid #333;
            image-rendering: pixelated;
            max-width: 90vw;
            max-height: 70vh;
            min-width: 320px;
            min-height: 240px;
        }
        .meta { font-size: 0.75rem; opacity: 0.5; }
    </style>
</head>
<body>
    <h1>OpenMarquee — dev preview</h1>
    <img id="frame" alt="latest rendered frame">
    <div class="meta">polling every 500ms</div>
    <script>
        const img = document.getElementById("frame");
        function poll() {
            img.src = "/dev/preview/frame.png?t=" + Date.now();
        }
        setInterval(poll, 500);
        poll();
    </script>
</body>
</html>
"""


@router.get("/preview", response_class=HTMLResponse)
async def preview_page() -> str:
    return _PREVIEW_HTML


@router.get(
    "/preview/frame.png",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/png": {}}},
        404: {"description": "No frame has been rendered yet."},
    },
)
async def preview_frame(renderer: RendererDep) -> FileResponse:
    if not renderer.output_path.exists():
        raise HTTPException(status_code=404, detail="no frame rendered yet")
    return FileResponse(renderer.output_path, media_type="image/png")


@router.post("/play/{item_id}", status_code=204)
async def play_item(item_id: UUID, storage: StorageDep, renderer: RendererDep) -> None:
    """Decode a stored content item's PNG and push it through the MockRenderer.

    This is the dev stand-in for the playback engine that lands in Phase 5.
    For now: pull the asset, decode to RGB at the renderer's native dimensions
    (resizing if needed), call render_frame.

    TODO(phase-5): once the playback engine exists, this endpoint should ask
    the engine to enqueue the item rather than write to MockRenderer directly.
    Two writers stomping on the same renderer would make the preview lie.
    """
    try:
        png = storage.read_asset(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no asset for {item_id}") from exc

    try:
        image = Image.open(io.BytesIO(png)).convert("RGB")
    except UnidentifiedImageError as exc:
        raise HTTPException(status_code=422, detail=f"asset is not a valid image: {exc}") from exc

    if image.size != (renderer.width, renderer.height):
        # NEAREST, not the Pillow-9+ default of BICUBIC: LED panels are
        # pixel-perfect and bicubic ringing produces colors the panel can't
        # actually show, which would make the dev preview lie.
        image = image.resize(
            (renderer.width, renderer.height),
            resample=Image.Resampling.NEAREST,
        )
    renderer.render_frame(image.tobytes())
