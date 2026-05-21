"""FastAPI app for the openMarquee Web slide screenshot helper.

Endpoints:
  GET /healthz  -- liveness probe, no auth.
  GET /shot     -- bearer-auth'd; screenshots `url` at `w`x`h`, returns PNG.

The screenshot worker is reached through the module-level `render_screenshot`
indirection so the test suite can monkeypatch in a fake that returns canned
PNG bytes -- no real browser needed to exercise the HTTP layer.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .token import resolve_token
from .validation import InvalidWebURL, validate_web_url

# --------------------------------------------------------------------------
# Screenshot worker indirection
#
# The /shot handler calls `render_screenshot(...)` through THIS module-level
# name, NOT by importing the Playwright module directly. Production wires it
# to the real worker lazily (see `_default_render_screenshot`); tests
# monkeypatch `app.render_screenshot` with a fake. This is what makes the
# HTTP layer testable without Chromium.
# --------------------------------------------------------------------------


async def _default_render_screenshot(url: str, width: int, height: int) -> bytes:
    """Production worker: defer-import Playwright, then render.

    Importing `.screenshot` here (rather than at module top) keeps the
    `playwright` dependency off the import path of this app module, so the
    app -- and the test suite -- import cleanly on a host with no browser.
    """
    from . import screenshot as _screenshot

    return await _screenshot.render_screenshot(url, width, height)


# The swappable hook. Tests do `app.render_screenshot = fake`.
render_screenshot = _default_render_screenshot


def _map_render_error(exc: Exception) -> HTTPException:
    """Translate a screenshot-worker exception into the right HTTP error.

    The Pi treats any non-200 as "fetch failed"; the distinct codes are
    for operator-facing diagnostics / logs.
    """
    # Resolve the Playwright-backed error types lazily so this module
    # imports without `playwright` present. If the import fails, the
    # isinstance checks below are simply skipped.
    timeout_types: tuple[type, ...] = ()
    error_types: tuple[type, ...] = ()
    try:  # pragma: no cover - exercised only on a host with the worker module
        from .screenshot import ScreenshotError, ScreenshotTimeout

        timeout_types = (ScreenshotTimeout,)
        error_types = (ScreenshotError,)
    except Exception:
        pass

    if timeout_types and isinstance(exc, timeout_types):
        # Page load exceeded the budget.
        return HTTPException(status_code=504, detail=f"page load timed out: {exc}")
    if error_types and isinstance(exc, error_types):
        # Page failed to load (DNS, refused connection, crash, ...).
        return HTTPException(status_code=502, detail=f"page failed to load: {exc}")
    # Anything else is an unexpected internal fault.
    return HTTPException(status_code=500, detail=f"internal screenshot error: {exc}")


# --------------------------------------------------------------------------
# App + lifespan
# --------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Resolve the bearer token at startup; tidy the browser at shutdown.

    The shared Chromium is launched lazily on the first /shot (see
    `screenshot._ensure_browser`), so there is nothing to start here -- but
    we DO close it on shutdown if it was ever launched.
    """
    app.state.token = resolve_token()
    _print_token_banner(app.state.token)
    try:
        yield
    finally:
        # Close the browser only if the worker module was actually used.
        import sys

        worker_mod = sys.modules.get("openmarquee_web_helper.screenshot")
        if worker_mod is not None:
            await worker_mod.shutdown_browser()


def _print_token_banner(token: str) -> None:
    """Print the active bearer token prominently for the operator to copy."""
    bar = "=" * 64
    print(bar, flush=True)
    print("  openMarquee Web slide helper -- bearer token", flush=True)
    print("  Paste this into the sign's Web slide settings:", flush=True)
    print(f"    {token}", flush=True)
    print(bar, flush=True)


app = FastAPI(
    title="openMarquee Web slide helper",
    version="0.1.0",
    lifespan=_lifespan,
)

@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Remap FastAPI's default 422 for bad query params to a 400.

    The spec for /shot says bad/missing `url`/`w`/`h` -> 400 (the Pi
    treats any non-200 as "fetch failed", but 400 is the agreed code for
    a malformed request).
    """
    return JSONResponse(status_code=400, content={"detail": exc.errors()})


# `auto_error=False` so we can return our own 401 wording rather than
# FastAPI's default 403 for a missing header.
_bearer = HTTPBearer(auto_error=False)


def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Dependency: enforce `Authorization: Bearer <token>` on /shot.

    Missing or wrong token -> 401. Uses a constant-time compare to avoid
    leaking the token via response timing.
    """
    import secrets

    expected = getattr(request.app.state, "token", None)
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not expected or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. No auth -- used to check the helper is up."""
    return {"status": "ok"}


@app.get("/shot")
async def shot(
    url: str = Query(..., description="The webpage to screenshot"),
    w: int = Query(..., gt=0, le=8192, description="Viewport width in px"),
    h: int = Query(..., gt=0, le=8192, description="Viewport height in px"),
    _: None = Depends(require_token),
) -> Response:
    """Screenshot `url` at a `w`x`h` viewport; return the PNG bytes.

    400 -- bad/missing params or a disallowed URL scheme.
    401 -- missing/wrong bearer token (handled by `require_token`).
    502 -- the page failed to load.
    504 -- the page load timed out.
    500 -- an unexpected internal error.
    """
    # Scheme/host validation -- never let this become a local-file reader.
    try:
        clean_url = validate_web_url(url)
    except InvalidWebURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Call the worker through the swappable indirection.
    try:
        png_bytes = await render_screenshot(clean_url, w, h)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to a clean HTTP status
        raise _map_render_error(exc) from exc

    return Response(content=png_bytes, media_type="image/png")
