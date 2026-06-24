"""FastAPI application that runs on the device."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from openmarquee import __version__
from openmarquee._tailscale_self import get_self_fqdn
from openmarquee.api import router as content_router
from openmarquee.api_auth import router as auth_router
from openmarquee.api_backgrounds import router as backgrounds_router
from openmarquee.api_flock import router as flock_router
from openmarquee.api_live import router as live_router
from openmarquee.api_network_supervisor import router as network_supervisor_router
from openmarquee.api_perf import router as perf_router
from openmarquee.api_playback import router as playback_router
from openmarquee.api_playlist import router as playlist_router
from openmarquee.api_schedule import router as schedule_router
from openmarquee.api_settings import router as settings_router
from openmarquee.api_system import router as system_router
from openmarquee.auth_middleware import AuthMiddleware
from openmarquee.content.migrations import migrate_050608_bg_to_000000
from openmarquee.csp_middleware import CSPMiddleware
from openmarquee.dependencies import (
    get_auth_storage,
    get_content_storage,
    get_demo_video_path,
    get_live_manager,
    get_playback_loop,
    get_playlist_storage,
    get_pull_worker,
    get_renderer,
    get_schedule_storage,
    get_seed_marker_path,
    get_settings_storage,
)
from openmarquee.dev import router as dev_router
from openmarquee.fqdn_redirect_middleware import FqdnRedirectMiddleware
from openmarquee.perf_middleware import PerfMiddleware

# LEVER 2 lazy-imports (2026-06-24): defer openmarquee.seed to first
# lifespan invocation. The seed module pulls in PIL (Image, ImageDraw,
# ImageFont) at top level to generate the first-boot starter slides;
# Pillow is ~5-8 MB RSS at import time and the seed path runs at most
# once per device. On every boot after the first, seed_if_needed
# logs + stamps a marker so the lifespan call is a fast no-op — but
# the PIL import would still fire if seed.py loaded at module level.
# The deferred import keeps Pillow out of the steady-state RSS.

log = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Set the root logger format + level for the device process.

    16.2 / sweep #8 A1: replace the bare uvicorn default (no
    timestamp, no module name, opaque "INFO:") with a timestamped
    structured-ish line that survives journalctl tailing. Level
    comes from OPENMARQUEE_LOG_LEVEL env (default INFO) so an
    operator can flip to DEBUG without code edit. Noisy third-party
    libs get explicitly silenced -- aiortc + httpx are chattiest
    at INFO and aren't useful for openMarquee-side debugging.
    """
    level_name = os.environ.get("OPENMARQUEE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
        level=level,
        # If a previous uvicorn entry installed handlers (it does),
        # force=True replaces them so our format wins.
        force=True,
    )
    # 16.3 / sweep #8 A4: tag every log record with the per-request
    # correlation id (or "-" when emitted outside a request, set by
    # the ContextVar's default). Attach the filter to the root
    # logger's handlers so all subloggers inherit it. Order matters:
    # the filter has to be installed BEFORE any log call runs against
    # the new format, otherwise the formatter would raise KeyError
    # on the missing request_id attribute.
    from openmarquee.perf_middleware import RequestIdLogFilter

    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestIdLogFilter())
    # Silence chatty deps at INFO; their WARNING+ still surfaces.
    for noisy in ("aiortc", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup: first-boot seed + auto-start playback. Shutdown: stop."""
    # LEVER 2 (2026-06-24): env-gated tracemalloc capture. Start as
    # early in the lifespan as possible so the snapshot includes
    # later startup allocations (seeded content, playlist load,
    # renderer Open, etc.). The /api/system/memory-snapshot endpoint
    # dumps the top allocators by size for QA to attribute the
    # ~140 MB Python footprint by source-line area.
    #
    # tracemalloc itself costs ~10-15% memory + CPU when active, so
    # it's strictly opt-in via the env var. Leave it OFF on prod
    # unless actively diagnosing.
    if os.environ.get("OPENMARQUEE_MEMORY_TRACE") == "1":
        try:
            import tracemalloc

            tracemalloc.start(25)  # 25-frame stacks; deep enough for FastAPI / Pydantic chains
            log.info("memory-trace: tracemalloc.start(25) — /api/system/memory-snapshot enabled")
        except Exception:
            log.exception("memory-trace: tracemalloc.start failed; /api/system/memory-snapshot will return 503")

    # r67 (2026-06-05): runtime asset verification. Logs CRITICAL if
    # the COLRv1 emoji font is absent / 0-byte at the canonical
    # /opt/openmarquee/ui/fonts/ path — otherwise the renderer's COLR
    # dispatch silently falls back to FontMissing MSDF tofu and the
    # service keeps reporting healthy. Skipped on dev environments
    # where /opt/openmarquee/ doesn't exist. Disable in test fixtures
    # via OPENMARQUEE_DISABLE_ASSET_CHECK=1 (mirrors DISABLE_SEED /
    # DISABLE_AUTOSTART pattern). Does NOT block startup.
    if os.environ.get("OPENMARQUEE_DISABLE_ASSET_CHECK") != "1":
        try:
            from openmarquee.runtime_assets import (
                check_runtime_assets,
                log_runtime_asset_issues,
            )

            log_runtime_asset_issues(check_runtime_assets())
        except Exception:
            log.exception("startup runtime asset check failed")

    # First-boot seed: if no marker file + storage is empty, create a few
    # starter ImageSlides so the operator has something to hit Play on
    # immediately. seed_if_needed logs + stamps a marker so this is a
    # no-op on every boot after the first. Tests pin
    # OPENMARQUEE_DISABLE_SEED=1 to keep the lifespan path from populating
    # a test-fixture's tmp_path with surprise content.
    if os.environ.get("OPENMARQUEE_DISABLE_SEED") != "1":
        try:
            # LEVER 2 lazy-import (2026-06-24): defer seed → PIL chain.
            from openmarquee.seed import seed_if_needed

            settings = get_settings_storage().load()
            seed_if_needed(
                storage=get_content_storage(),
                playlist_storage=get_playlist_storage(),
                marker_path=get_seed_marker_path(),
                width=settings.display_width,
                height=settings.display_height,
                demo_video_path=get_demo_video_path(),
                schedule_storage=get_schedule_storage(),
            )
        except Exception:
            # Seeding is nice-to-have; never block startup on it.
            log.exception("startup seed failed")

    # One-shot, env-gated content migration: rewrite text-slide
    # `background_color == "#050608"` to `"#000000"`. The seed defaults
    # baked #050608 (an aesthetic near-black) pre-2026-05-24; that value
    # pre-dated the Bug 7 Broadcast-RGB-Full fix which now makes the
    # off-by-three visible on full-range HDMI. Operator opts in by
    # setting OPENMARQUEE_MIGRATE_050608_BG=1 on the next boot of a
    # device whose content was seeded pre-fix, then drops the env var
    # once the migration has run.
    if os.environ.get("OPENMARQUEE_MIGRATE_050608_BG") == "1":
        try:
            n = migrate_050608_bg_to_000000(get_content_storage())
            log.info("startup: migrated %d item(s) #050608 -> #000000", n)
        except Exception:
            log.exception("startup #050608 migration failed")

    # Prune playlist refs that no longer resolve to stored content. Catches
    # the "dev-wiped content/ but left playlist.json intact" class of bug
    # before the pallet renders dangling tiles or the playback loop hits
    # a FileNotFoundError mid-slide.
    try:
        valid_ids = {item.id for item in get_content_storage().list_all()}
        pruned = get_playlist_storage().prune_dangling_refs(valid_ids)
        if pruned:
            log.warning("startup: pruned %d dangling playlist item_id(s)", pruned)
    except Exception:
        log.exception("startup playlist prune failed")

    # Open the production renderer if it's a context manager (RustRenderer
    # spawns the openmarquee-render subprocess, negotiates the HDMI mode,
    # and opens the IPC handshake here -- failure should surface at
    # startup, not on the first render_frame call). MockRenderer is a
    # plain class with no __enter__; check before calling. On failure
    # we fall back to mock so the service still serves the UI even with
    # a misconfigured display.
    renderer = get_renderer()
    if hasattr(renderer, "__enter__"):
        try:
            renderer.__enter__()
        except Exception:
            log.exception("startup: renderer __enter__ failed; degrading to mock")
            # Force the singleton to swap to mock for the rest of the
            # process lifetime so the playback loop has a working target.
            # The next get_playback_loop() resolution will go through
            # _real_renderer_singleton again, see env=mock, and bind
            # the existing _mock_renderer_singleton.
            from openmarquee.dependencies import (
                _playback_loop_singleton,
                _real_renderer_singleton,
            )

            _real_renderer_singleton.cache_clear()
            _playback_loop_singleton.cache_clear()
            os.environ["OPENMARQUEE_RENDERER"] = "mock"

    # "Hardware always running" — the device's real playback loop starts
    # at boot and runs until shutdown. The UI's inline preview is a
    # parallel client-side simulator, not a control surface for the loop.
    # Tests can opt out via OPENMARQUEE_DISABLE_AUTOSTART=1 so fixtures
    # that spin a backend don't have an extra asyncio task running.
    if os.environ.get("OPENMARQUEE_DISABLE_AUTOSTART") != "1":
        try:
            await get_playback_loop().start()
        except Exception:
            log.exception("startup playback autostart failed")

    # Flock pull worker — periodic reliability backstop that reconciles
    # against sync=True peers even when pushes get dropped. Tests can
    # opt out via OPENMARQUEE_DISABLE_PULL_WORKER=1 so fixtures that
    # spin a backend don't have an extra asyncio task racing the assertions.
    if os.environ.get("OPENMARQUEE_DISABLE_PULL_WORKER") != "1":
        try:
            await get_pull_worker().start()
        except Exception:
            log.exception("startup pull worker autostart failed")

    # r64 (2026-06-05): perf telemetry CMA sampler. Reads /proc/meminfo
    # every 5 s into a 1-minute rolling window so `GET /api/perf-stats`
    # can serve `cma_used_max_mb_1m` + `cma_used_avg_mb_1m` without
    # a per-request syscall. Cheap: ~sub-ms /proc read + dict update.
    # Disabled in test fixtures alongside DISABLE_AUTOSTART since they
    # don't run the full lifespan worker set.
    cma_sampler_handle: asyncio.Task[None] | None = None
    if os.environ.get("OPENMARQUEE_DISABLE_AUTOSTART") != "1":
        try:
            from openmarquee.perf_stats import cma_sampler_task

            cma_sampler_handle = asyncio.create_task(cma_sampler_task(), name="cma-sampler")
        except Exception:
            log.exception("startup CMA sampler autostart failed")

    # P1.2-A (2026-06-10) onboarding rework: network supervisor
    # observe-only loop. Polls wpa_supplicant control socket events
    # + periodically queries `iw dev wlan0 info` for STA freq. Drives
    # the state machine + records every channel-follow DECISION (no
    # subprocess actuation in P1.2-A). Tests opt out via
    # OPENMARQUEE_DISABLE_NETWORK_SUPERVISOR=1 (also opts out under
    # the umbrella DISABLE_AUTOSTART flag).
    network_supervisor_handle: asyncio.Task[None] | None = None
    network_takeover_handle: asyncio.Task[None] | None = None
    if (
        os.environ.get("OPENMARQUEE_DISABLE_AUTOSTART") != "1"
        and os.environ.get("OPENMARQUEE_DISABLE_NETWORK_SUPERVISOR") != "1"
    ):
        try:
            from openmarquee.dependencies import get_network_supervisor
            from openmarquee.network_supervisor_loop import supervisor_observe_loop

            supervisor = get_network_supervisor()
            await supervisor.lifespan_start()
            network_supervisor_handle = asyncio.create_task(
                supervisor_observe_loop(supervisor),
                name="network-supervisor-observe",
            )
            # P1.2-B (2026-06-10): take-over evaluation. Env-gated by
            # OPENMARQUEE_NETWORK_TAKEOVER_ENABLED=1 (default OFF
            # so existing dev / FYS deployments are unchanged). The
            # task runs ONCE: evaluates preconditions (Tailscale,
            # creds, NM dir, idempotency); if all met, executes the
            # flip + arms the rollback watchdog, then exits. Observe
            # loop continues independently.
            if os.environ.get("OPENMARQUEE_DISABLE_NETWORK_TAKEOVER") != "1":
                from openmarquee.network_supervisor_takeover import (
                    run_takeover_attempt_if_eligible,
                )

                network_takeover_handle = asyncio.create_task(
                    run_takeover_attempt_if_eligible(
                        supervisor=supervisor,
                        settings_storage=get_settings_storage(),
                    ),
                    name="network-supervisor-takeover",
                )
        except Exception:
            log.exception("startup network supervisor autostart failed")
    yield
    # Tear down any active live session BEFORE the playback loop stops
    # so the session's close() can resume() the loop cleanly even though
    # the loop is about to exit. Order matters less now that resume() is
    # a no-op against a stopped loop, but the explicit shutdown order is
    # cheaper than reasoning about the race later.
    with suppress(Exception):
        await get_live_manager().stop_all()
    await get_playback_loop().stop()
    with suppress(Exception):
        await get_pull_worker().stop()
    # r64: stop the CMA sampler. Cancellation is idempotent; if it
    # already exited (CancelledError raise during sleep), .cancel()
    # is a no-op.
    if cma_sampler_handle is not None:
        cma_sampler_handle.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await cma_sampler_handle
    # P1.2-A: cancel the network supervisor observe loop. The loop
    # catches CancelledError + closes the wpa_supplicant socket
    # cleanly before propagating, so this await both joins the task
    # and flushes the socket teardown.
    if network_supervisor_handle is not None:
        network_supervisor_handle.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await network_supervisor_handle
        # P1.2-B: cancel the take-over evaluator if it's still
        # running. The orchestrator's watchdog (if armed) is held
        # by the supervisor singleton; the watchdog continues to
        # fire from its asyncio.Task scope, decoupled from this
        # one-shot evaluator task.
        if network_takeover_handle is not None:
            network_takeover_handle.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await network_takeover_handle
        try:
            from openmarquee.dependencies import get_network_supervisor

            await get_network_supervisor().lifespan_stop()
        except Exception:
            log.exception("shutdown network supervisor lifespan_stop failed")
    # Close the renderer last -- after the playback loop has stopped
    # writing frames -- so we don't free its buffers mid-render.
    renderer = get_renderer()
    if hasattr(renderer, "__exit__"):
        with suppress(Exception):
            renderer.__exit__(None, None, None)


app = FastAPI(title="openMarquee", version=__version__, lifespan=lifespan)

# Middleware stack note: Starlette's add_middleware inserts at the
# front of user_middleware, then build_middleware_stack wraps in
# reversed() order -- net effect is the LAST add_middleware call
# becomes the OUTERMOST wrapper. With the order below the runtime
# stack is (outer -> inner):
#
#   CSP -> Perf -> Fqdn -> Auth -> routes
#
# CSP outer-most so it stamps headers on every response, including
# Auth's 401s + Fqdn's 301s (proves out via
# test_csp_header_present_on_401_when_auth_enabled).
#
# Perf NEXT OUT so it times + records EVERY HTTP request including
# Auth-rejected 401s + Fqdn-redirected 301s ("are we 401-ing a
# lot?" / "how often is the FQDN short-name hit?" observability).
# 2026-05-24: re-positioned from innermost; earlier placement
# inside Auth meant 401s never reached the perf ring, which
# undermined the in-memory-ring's purpose for high-loss
# diagnostics.
#
# Fqdn wraps Auth so the 301 is timestamped by Perf but the
# bearer-token compare doesn't run on a request we're about to
# throw away.
#
# Auth innermost — fails closed for any non-whitelisted route.

# Batch 20.1 / phase A.1: bearer-token gate. AuthMiddleware fails
# closed: requests not on the whitelist need a valid token. Pass a
# callable resolver -- the middleware looks up the storage
# per-request so the singleton's lru_cache can be cleared between
# tests (tests point OPENMARQUEE_AUTH_PATH at a tmp dir).
app.add_middleware(AuthMiddleware, auth_storage_resolver=get_auth_storage)

# HTTPS Phase 1: 301-redirect non-FQDN requests to the canonical
# Tailscale HTTPS URL. Sits above Auth so we don't waste a bearer-
# token compare on a request we're about to throw away. Skip-list
# inside the middleware passes loopback / RFC1918 / CGNAT / FQDN /
# settings-disabled / Tailscale-down through unmodified. Resolvers
# are injected so tests fake the FQDN + settings without touching
# real subprocess / settings files.
app.add_middleware(
    FqdnRedirectMiddleware,
    fqdn_resolver=get_self_fqdn,
    settings_resolver=lambda: get_settings_storage().load(),
)

# Perf middleware -- timestamps each HTTP request, logs slow ones,
# and pushes records into the in-memory ring exposed at
# /api/system/perf-stats. Positioned OUTSIDE Auth + Fqdn so the
# ring records EVERY request the device sees (auth-rejected 401s,
# fqdn-redirected 301s, healthz 200s) -- the in-memory ring's
# value for high-loss diagnostics depends on it seeing everything.
app.add_middleware(PerfMiddleware)

# Content-Security-Policy header stamper. Sits outside Auth/Fqdn so
# the header is added to EVERY response those middlewares can return
# (AuthMiddleware's 401s, Fqdn's 301s, healthz 200s) -- proves out
# via test_csp_header_present_on_401_when_auth_enabled. The only
# layer above CSP is BodyCapMiddleware (below) whose 413 is a JSON
# error response, so CSP-stamping it would add no defensive value.
# Set OPENMARQUEE_CSP_REPORT_ONLY=1 to emit the report-only variant
# during policy tuning -- production default is enforce.
app.add_middleware(
    CSPMiddleware,
    report_only=os.environ.get("OPENMARQUEE_CSP_REPORT_ONLY") == "1",
)

# 2026-05-25 Bundle B2 piece 3: ASGI body-cap. OUTERMOST middleware
# (added LAST per Starlette's reversed-stack semantics) so a 413
# fires BEFORE any other middleware reads the body. Closes the gap
# B1's Pydantic Field(max_length=...) left open -- max_length
# rejects at validation time but Starlette buffers the full body
# first. This gate rejects at Content-Length read so a hostile
# multi-MB POST never allocates the body buffer at all.
from openmarquee._body_cap_middleware import BodyCapMiddleware  # noqa: E402

app.add_middleware(BodyCapMiddleware)


@app.exception_handler(RequestValidationError)
async def _request_validation_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Override FastAPI's default 422 handler to drop the echoed input.

    FastAPI's default emits `detail = jsonable_encoder(exc.errors())`,
    which includes a verbatim copy of every offending field's value.
    For an over-cap prompt at /api/backgrounds/generate that's a
    multi-KB response body for what's morally "input too long" (QA
    explore-bg-gen 2026-04-26 → verify-d9d6efb-bg-gen-cap.md). Mirrors
    the api.py::_validation_error_422 helper's `include_input=False`
    behavior, applied uniformly to every endpoint's request-body
    validation.

    FastAPI's `RequestValidationError` subclasses pydantic's
    `ValidationError` but overrides `.errors()` to a Starlette-style
    list-of-dicts that doesn't accept `include_input=False` (unlike
    raw pydantic). So we filter the `input` key out manually and pass
    through `jsonable_encoder` for the same JSON-safety guarantees
    the default handler gives (UUIDs in `input` paths, exceptions in
    `ctx`).
    """
    sanitised = [{k: v for k, v in err.items() if k != "input"} for err in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(sanitised)},
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Used by the dev script and (later) by systemd."""
    return {"status": "alive", "version": __version__}


app.include_router(auth_router)
app.include_router(content_router)
app.include_router(playlist_router)
app.include_router(schedule_router)
app.include_router(settings_router)
app.include_router(backgrounds_router)
app.include_router(playback_router)
app.include_router(live_router)
app.include_router(system_router)
# r64 (2026-06-05): GET /api/perf-stats -- cheap in-memory perf
# telemetry snapshot. No auth (read-only, no PII, Tailscale-gated).
app.include_router(perf_router)
app.include_router(flock_router)
# P1.2-A (2026-06-10) onboarding rework: GET /api/network-supervisor/state
# exposes the supervisor's state machine + diagnostics ring buffer for
# QA observability during the observe-only soak. No mutations from this
# surface in P1.2-A.
app.include_router(network_supervisor_router)

# Dev tooling (preview page, manual play endpoint) is mounted by default
# because the device is its own captive-portal AP with no inbound internet.
# The SD-card image builder (Phase 9) sets OPENMARQUEE_DISABLE_DEV=1 to
# strip it from production images.
if os.environ.get("OPENMARQUEE_DISABLE_DEV") != "1":
    app.include_router(dev_router)


def _resolve_ui_dir() -> Path:
    """Where the UI static files live. backend/openmarquee/app.py → ../../ui/.

    TODO(phase-5/packaging): this path only resolves in a checkout.
    Once we `pip install` this package onto the Pi the sibling `ui/`
    won't exist in site-packages. Ship `ui/dist/` + `index.html` as
    package data and resolve via `importlib.resources`, or bake the
    path into the systemd unit's OPENMARQUEE_UI_DIR.
    """
    override = os.environ.get("OPENMARQUEE_UI_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "ui"


# UI mount goes LAST so registered routes (/healthz, /api/*, /dev/*) take
# precedence over the static fallback. `html=True` makes "/" serve index.html.
app.mount("/", StaticFiles(directory=_resolve_ui_dir(), html=True), name="ui")
