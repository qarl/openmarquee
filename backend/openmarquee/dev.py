"""Dev-time preview tooling.

A tiny HTML page that polls the MockRenderer's latest frame at
/dev/preview, plus the underlying frame.png GET endpoint it pulls
from. The PlaybackLoop drives the renderer continuously now (Phase
5+), so the preview always has a current frame as long as the
playback loop is running.

Lives under its own router so production builds can drop it later if
we decide we want to. For now it's always mounted — the device is
its own captive-portal AP with no inbound internet, so the attack
surface is the same as any other endpoint.

Historical: the prior POST /dev/play/{item_id} endpoint that pushed
a single content item through the MockRenderer directly was retired
once the PlaybackLoop landed (see api_playback.py + playback.py).
The endpoint had no remaining callers — the UI never relied on it,
and operators get continuous playback via the API now.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from openmarquee.dependencies import get_mock_renderer
from openmarquee.rendering.mock import MockRenderer

router = APIRouter(prefix="/dev", tags=["dev"])

RendererDep = Annotated[MockRenderer, Depends(get_mock_renderer)]

_PREVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>openMarquee — dev preview</title>
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
    <h1>openMarquee — dev preview</h1>
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
