"""System-probe endpoints used by the Settings UI + the flock health
view to offer sensible defaults: display-dim detection (so first-boot
picks up the real framebuffer res), WiFi-scan (so the station-mode
dropdown lists what's actually reachable), and a /info endpoint that
reports the device's own model / mode / signal / uptime for the flock
self-card (Phase B.1 per docs/phase-b-flock-scope.md).

All endpoints best-effort — they shell out to tools that exist on
real hardware (fbset / iw / airport / /proc/*) and return empty-but-
well-formed payloads on dev boxes without those tools. The UI falls
back to letting the operator type values manually OR to placeholder
constants matching the Phase A SELF_PLACEHOLDER_* fallbacks in
flock.js.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from openmarquee import auto_render, identity, tailscale
from openmarquee.api import cors_headers_for_origin
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import get_flock_storage, get_settings_storage
from openmarquee.flock import FlockStorage
from openmarquee.perf_middleware import recent_requests
from openmarquee.playlist import PlaylistStorage
from openmarquee.schedule import ScheduleStorage
from openmarquee.settings import SettingsStorage
from openmarquee.text_raster import font_cache_info

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])

SettingsDep = Annotated[SettingsStorage, Depends(get_settings_storage)]
FlockDep = Annotated[FlockStorage, Depends(get_flock_storage)]


class DisplayDims(BaseModel):
    """Width/height in pixels. Both None when detection isn't available."""

    width: int | None = None
    height: int | None = None
    source: str  # "fbset" | "sysfs" | "none"


class WifiNetwork(BaseModel):
    ssid: str
    signal_dbm: int | None = None


class WifiScanResult(BaseModel):
    networks: list[WifiNetwork]
    source: str  # "iw" | "airport" | "none"


# --- display dim detection ---


@router.get("/display-dims", response_model=DisplayDims)
async def detect_display_dims() -> DisplayDims:
    """Best-effort probe for the physical display resolution.

    Order:
      1. `fbset -i` parsing (Linux framebuffer — primary Pi path).
      2. /sys/class/graphics/fb0/virtual_size (fallback on some Pi OS variants).
      3. None / "none" source (dev boxes, Mac) — UI prompts manual entry.
    """
    if shutil.which("fbset"):
        try:
            # Batch 6.2: subprocess.run is sync and blocks the event
            # loop for the duration of fbset (up to 2s on a slow Pi).
            # Run on a worker thread so concurrent /api/playback/state
            # polls stay responsive while this is in flight.
            out = await asyncio.to_thread(
                subprocess.run,
                ["fbset", "-i"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            m = re.search(r"geometry\s+(\d+)\s+(\d+)", out.stdout)
            if m:
                return DisplayDims(
                    width=int(m.group(1)),
                    height=int(m.group(2)),
                    source="fbset",
                )
        except Exception:
            log.exception("fbset probe failed")

    sysfs = Path("/sys/class/graphics/fb0/virtual_size")
    if sysfs.exists():
        try:
            w, h = sysfs.read_text().strip().split(",")
            return DisplayDims(width=int(w), height=int(h), source="sysfs")
        except Exception:
            log.exception("sysfs fb0 probe failed")

    return DisplayDims(width=None, height=None, source="none")


# --- WiFi scan ---


@router.get("/wifi-scan", response_model=WifiScanResult)
async def scan_wifi() -> WifiScanResult:
    """Best-effort scan of nearby WiFi SSIDs.

    Order:
      1. `iw dev wlan0 scan` (Pi / Linux). Needs CAP_NET_ADMIN; may
         require the backend to run as root or be granted a capability.
      2. `airport -s` (macOS — the legacy path; Apple deprecated from
         14.4 onwards and returns empty unless run as root).
      3. None / "none" source — UI falls back to manual SSID entry.
    """
    # Linux / Pi path.
    if shutil.which("iw"):
        try:
            # Batch 6.2: iw scan blocks up to 8s; offload so the
            # event loop keeps serving other requests.
            out = await asyncio.to_thread(
                subprocess.run,
                ["iw", "dev", "wlan0", "scan"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            networks = _parse_iw_scan(out.stdout)
            if networks or out.returncode == 0:
                return WifiScanResult(networks=networks, source="iw")
        except Exception:
            log.exception("iw scan failed")

    # macOS path (dev convenience).
    airport = Path(
        "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
    )
    if airport.exists():
        try:
            # Batch 6.2: airport blocks up to 5s; offload.
            out = await asyncio.to_thread(
                subprocess.run,
                [str(airport), "-s"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            networks = _parse_airport_scan(out.stdout)
            return WifiScanResult(networks=networks, source="airport")
        except Exception:
            log.exception("airport scan failed")

    return WifiScanResult(networks=[], source="none")


# --- parsers ---


def _parse_iw_scan(output: str) -> list[WifiNetwork]:
    """Pull SSIDs + signal from `iw dev wlan0 scan` output.

    Each BSS block contains `signal: -53.00 dBm` and `SSID: foo` lines.
    Dedupe on SSID, keeping the strongest signal.
    """
    networks: dict[str, WifiNetwork] = {}
    current_signal: int | None = None
    for line in output.splitlines():
        sig = re.match(r"\s*signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", line)
        if sig:
            current_signal = int(float(sig.group(1)))
            continue
        ssid_m = re.match(r"\s*SSID:\s*(.*)$", line)
        if ssid_m:
            ssid = ssid_m.group(1).strip()
            if not ssid:
                continue
            existing = networks.get(ssid)
            if existing is None or (
                current_signal is not None
                and (existing.signal_dbm is None or current_signal > existing.signal_dbm)
            ):
                networks[ssid] = WifiNetwork(ssid=ssid, signal_dbm=current_signal)
            current_signal = None
    return sorted(
        networks.values(),
        key=lambda n: (-(n.signal_dbm or -999), n.ssid),
    )


# --- /api/system/info — flock health probe payload (Phase B.1) ---


# Sentinel values that match flock.js's Phase A SELF_PLACEHOLDER_*
# constants. When the relevant /proc source isn't available (dev
# laptop, missing wireless interface, etc.) we report these so the
# UI's self-card stays meaningful instead of rendering blanks. The
# duplication with the JS-side constants is deliberate — the wire
# shape of /api/system/info is the contract; the JS-side fallbacks
# remain so the UI can render before the fetch lands.
_FALLBACK_MODEL = "Pi Zero 2 W"
_FALLBACK_SIGNAL = 100
_FALLBACK_UPTIME = "up since boot"


class SystemInfo(BaseModel):
    """Per-device health summary for the flock self-card.

    Wire shape mirrors FlockPeer.{model, mode, signal, uptime}. The
    flock UI's self-card consumes /api/system/info from the local
    device; the flock probe consumer (Phase B.3) will fetch the
    same endpoint from each peer.

    Source field documents which path produced the values so a
    mixed result (model from /proc/device-tree but signal falling
    back to sentinel because no wireless adapter) is debuggable
    without re-probing.

    Contract note (15.4): all fields are NON-NULLABLE by design.
    When a /proc reader returns None, the handler substitutes the
    matching `_FALLBACK_*` sentinel so the wire payload always
    carries a renderable value. Don't relax nullability here -- the
    UI's defensive null-guards mirror this contract; if you flip a
    field to Optional, the UI has to grow real null branching.
    """

    model: str
    mode: str
    signal: int
    uptime: str
    source: str  # "proc" | "fallback" | "mixed"
    # Rotation-applied display dims so flock peers can render each
    # other's thumbnails at the correct aspect (B1 follow-up, qarl
    # 2026-04-29). Width/height are AFTER rotation — a 1920×1080 panel
    # rotated 90° reports 1080×1920. Rotation is the raw value (0/90/
    # 180/270) so debugging tools can still see what's set on the device.
    display_width: int
    display_height: int
    display_rotation: int
    # qarl 2026-05-12: MySignXXX device identifier from
    # /var/openmarquee/identity.json (set at first boot). NULL on
    # off-device dev hosts where identity.json doesn't exist (the
    # operator-facing UI falls back to the OS hostname there).
    device_id: str | None


@router.get("/info", response_model=SystemInfo)
async def system_info(
    request: Request,
    response: Response,
    settings_storage: SettingsDep,
    flock_storage: FlockDep,
) -> SystemInfo:
    """Read /proc/* + the configured display mode and return a flock
    self-card payload. Each /proc reader is best-effort; failure
    falls back to the matching SELF_PLACEHOLDER constant.
    """
    # Batch 11.3 / sweep #5 #4: CORS allowlist-reflective. Peers in the
    # operator's flock can read this device's display dims for rendering
    # peer-card thumbnails; arbitrary cross-origin pages cannot (the
    # payload is metadata, not secret, but a wildcard ACAO is still the
    # wrong shape -- failing closed). See cors_headers_for_origin in
    # api.py for the full rationale.
    for key, value in cors_headers_for_origin(
        request.headers.get("origin", ""), flock_storage
    ).items():
        response.headers[key] = value

    settings = settings_storage.load()

    model = _read_model()
    signal = _read_signal()
    uptime_s = _read_uptime_seconds()
    sources_used: list[str] = []
    sources_fallback: list[str] = []

    if model is None:
        model = _FALLBACK_MODEL
        sources_fallback.append("model")
    else:
        sources_used.append("model")

    if signal is None:
        signal = _FALLBACK_SIGNAL
        sources_fallback.append("signal")
    else:
        sources_used.append("signal")

    if uptime_s is None:
        uptime = _FALLBACK_UPTIME
        sources_fallback.append("uptime")
    else:
        uptime = _format_uptime(uptime_s)
        sources_used.append("uptime")

    if sources_used and sources_fallback:
        source = "mixed"
    elif sources_used:
        source = "proc"
    else:
        source = "fallback"

    mode = _format_mode(settings.output_mode, settings.display_width, settings.display_height)

    rotation = int(settings.display_rotation)
    if rotation in (90, 270):
        eff_w, eff_h = settings.display_height, settings.display_width
    else:
        eff_w, eff_h = settings.display_width, settings.display_height

    return SystemInfo(
        model=model,
        mode=mode,
        signal=signal,
        uptime=uptime,
        source=source,
        display_width=eff_w,
        display_height=eff_h,
        display_rotation=rotation,
        device_id=identity.read_device_id(),
    )


# --- qarl 2026-05-12 (arc 4): Tailscale URL-auth flow ---


class TailscaleUpResponse(BaseModel):
    """Response from POST /api/system/tailscale/up.

    state:
      - "pending": auth_url populated; operator should open it
      - "authenticated": already up, no action needed
      - "error": something went wrong; see message
    """

    state: str
    auth_url: str | None
    message: str | None


class TailscaleStatusResponse(BaseModel):
    state: str
    hostname: str | None
    ipv4: str | None
    message: str | None


@router.post("/tailscale/up", response_model=TailscaleUpResponse)
async def tailscale_up() -> TailscaleUpResponse:
    """Start `tailscale up --hostname=<device_id>` without an auth-key
    and return the auth URL Tailscale prints. Operator opens the URL
    in a browser, signs in to Tailscale, and the daemon finishes
    auth. Caller polls /tailscale/status to detect the transition."""
    device_id = identity.read_device_id()
    result = await tailscale.start_up(device_id)
    return TailscaleUpResponse(
        **{
            "state": result["state"],
            "auth_url": result.get("auth_url"),
            "message": result.get("message"),
        }
    )


@router.get("/tailscale/status", response_model=TailscaleStatusResponse)
async def tailscale_status() -> TailscaleStatusResponse:
    """Read `tailscale status --json` and return a thin summary.
    UI polls this while in the URL-auth modal to detect when the
    operator's browser sign-in completes."""
    result = await tailscale.read_status()
    return TailscaleStatusResponse(
        **{
            "state": result["state"],
            "hostname": result.get("hostname"),
            "ipv4": result.get("ipv4"),
            "message": result.get("message"),
        }
    )


def _read_model() -> str | None:
    """Read the device model from /proc/device-tree/model (Pi-native)
    or /proc/cpuinfo's `Model:` line (older Pi OS, generic ARM).
    Returns None on macOS / dev boxes / unknown hardware."""
    # /proc/device-tree/model is null-terminated on Pi OS — strip nulls
    # before returning. Path-based open avoids the platform-shell
    # dependency that fbset / iw rely on.
    dt = Path("/proc/device-tree/model")
    if dt.exists():
        try:
            text = dt.read_text(errors="replace").strip("\x00").strip()
            if text:
                return text
        except Exception:
            log.exception("/proc/device-tree/model read failed")

    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        try:
            for line in cpuinfo.read_text().splitlines():
                m = re.match(r"^\s*Model\s*:\s*(.+?)\s*$", line)
                if m:
                    return m.group(1)
        except Exception:
            log.exception("/proc/cpuinfo Model parse failed")

    return None


def _read_signal() -> int | None:
    """Read WiFi signal quality as a 0-100 percentage from
    /proc/net/wireless. Returns None if no wireless interface is
    configured or the file isn't readable.

    /proc/net/wireless format (truncated):

        Inter-| sta-|   Quality        |   Discarded packets ...
         face | tus | link level noise |  nwid crypt frag retry misc | beacon
         wlan0: 0000   54.  -55.  -256        0     0    0     0    0       0

    The `link` column (first numeric after status) is a quality
    score scaled to a per-driver maximum — typically 70 on Pi's
    brcmfmac. Return as percentage of 70 since most callers want a
    portable 0-100. Drivers that go higher will clamp.
    """
    wireless = Path("/proc/net/wireless")
    if not wireless.exists():
        return None
    try:
        for line in wireless.read_text().splitlines():
            # Skip header lines (no colon-followed-by-numbers).
            m = re.match(r"^\s*(\S+):\s+\S+\s+([\d.]+)", line)
            if m and m.group(1) != "face":
                quality = float(m.group(2))
                pct = round(quality / 70 * 100)
                return max(0, min(100, pct))
    except Exception:
        log.exception("/proc/net/wireless parse failed")
    return None


def _read_uptime_seconds() -> float | None:
    """Read uptime in seconds from /proc/uptime. Returns None on
    macOS / non-Linux."""
    uptime = Path("/proc/uptime")
    if not uptime.exists():
        return None
    try:
        first = uptime.read_text().split()[0]
        return float(first)
    except Exception:
        log.exception("/proc/uptime parse failed")
        return None


def _format_uptime(seconds: float) -> str:
    """Format uptime as a two-unit truncated string matching the
    FlockPeer.uptime convention ("4d 7h", "3h 15m", "12m 5s").
    Boot-recent values (<60s) read as "Ns" only — saying "0m Ns" is
    silly."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _format_mode(output_mode: str, width: int, height: int) -> str:
    """Format the device's output mode + display dims as the slug
    convention FlockPeer.mode expects.

    - hdmi: "hdmi-{h}" — operators talk about HDMI in resolution-
      class terms (720p/1080p) rather than literal dimensions.

    Legacy LED modes (hub75 / ws281x / composite) are no longer a
    valid output_mode (settings.py coerces them to "hdmi" on load);
    the fallback path handles any unexpected mode literal by
    falling back to "{mode}-{w}x{h}" so a future format-mode slug
    has a sensible default.
    """
    if output_mode == "hdmi":
        return f"hdmi-{height}"
    return f"{output_mode}-{width}x{height}"


def _parse_airport_scan(output: str) -> list[WifiNetwork]:
    """Pull SSIDs from `airport -s` output. Columns:
        SSID BSSID RSSI CHANNEL HT CC SECURITY (auth/unicast/group)
    Header line skipped; SSID is everything up to the first BSSID-shaped
    token (colon-separated hex).
    """
    networks: dict[str, WifiNetwork] = {}
    bssid = re.compile(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}")
    for line in output.splitlines()[1:]:
        m = bssid.search(line)
        if not m:
            continue
        # strip() (not rstrip): airport pads SSID column with
        # leading whitespace for alignment. Sweep #3 / 9.1: tests
        # caught that the original rstrip-only preserved leading
        # spaces, producing UI-visible SSIDs like "       HomeNet".
        ssid = line[: m.start()].strip()
        if not ssid:
            continue
        rest = line[m.end() :].split()
        rssi: int | None = None
        if rest:
            try:
                rssi = int(rest[0])
            except ValueError:
                rssi = None
        existing = networks.get(ssid)
        if existing is None or (
            rssi is not None and (existing.signal_dbm is None or rssi > existing.signal_dbm)
        ):
            networks[ssid] = WifiNetwork(ssid=ssid, signal_dbm=rssi)
    return sorted(
        networks.values(),
        key=lambda n: (-(n.signal_dbm or -999), n.ssid),
    )


# --- perf instrumentation (Batch 6.1) ---


class PerfStats(BaseModel):
    """Snapshot of per-storage counters + font cache stats.

    Counters are class-level, so the snapshot is the cumulative call
    count since the process started. Sweep #2 baseline capture: GET
    this once at startup, run a canonical playback session, GET again,
    diff the values. The intent is to verify which list_all / load_all
    paths are actually hot in production before optimizing them.
    """

    content_storage: dict[str, int]
    playlist_storage: dict[str, int]
    flock_storage: dict[str, int]
    settings_storage: dict[str, int]
    schedule_storage: dict[str, int]
    font_cache: dict[str, int]
    # Render-path counters (Batch 8.1) -- exercised when auto-mode
    # or image-bg slides actually fire through the playback loop.
    # The synthetic-testclient baseline doesn't hit these; the
    # autorender baseline (qa/perf-baseline-autorender-2026-05-10
    # .json) does.
    #
    # Post-DELETE-PIL (slice 13): `motion` is permanently empty --
    # the Rust sidecar owns motion composition and Python no longer
    # has a per-frame composer to count against. Kept in the schema
    # so dashboards keying off this key don't 404.
    motion: dict[str, int] = {}
    auto_render: dict[str, int] = {}
    request_log: list[dict[str, object]] | None = None


@router.get("/perf-stats", response_model=PerfStats)
async def perf_stats() -> PerfStats:
    """Aggregate the per-storage class counters + font cache info."""
    cache = font_cache_info()
    return PerfStats(
        content_storage=ContentStorage.stats_snapshot(),
        playlist_storage=PlaylistStorage.stats_snapshot(),
        flock_storage=FlockStorage.stats_snapshot(),
        settings_storage=SettingsStorage.stats_snapshot(),
        schedule_storage=ScheduleStorage.stats_snapshot(),
        font_cache={
            "hits": cache.hits,
            "misses": cache.misses,
            "maxsize": cache.maxsize,
            "currsize": cache.currsize,
        },
        motion={},
        auto_render=auto_render.stats_snapshot(),
        request_log=recent_requests(),
    )
