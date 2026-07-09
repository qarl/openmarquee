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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from openmarquee import identity, system_control, tailscale

# LEVER 2 lazy-imports (2026-06-24): auto_render + text_raster pull in
# Pillow (~5-8 MB RSS) at import time. The two are only referenced in
# the /perf-stats endpoint below; deferring their import to the
# handler keeps the backend's startup footprint free of Pillow on
# prod devices that never hit the dev-side perf endpoint. font_cache_
# info() returns counters tracked at module import time, so the first
# /perf-stats call IS what materializes the counters too.
from openmarquee.api import cors_headers_for_origin
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    get_auth_storage,
    get_content_storage,
    get_flock_storage,
    get_playlist_storage,
    get_schedule_storage,
    get_seed_marker_path,
    get_settings_storage,
    get_tombstone_storage,
)
from openmarquee.flock import FlockStorage
from openmarquee.perf_middleware import recent_requests
from openmarquee.playlist import PlaylistStorage
from openmarquee.schedule import ScheduleStorage
from openmarquee.settings import SettingsStorage
from openmarquee.tombstone import TombstoneStorage

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
    # 2026-07-01 (onboarding audit 4b follow-up): band the strongest
    # BSS for this SSID was seen on. Populated only from the `iw` path
    # (the airport path doesn't expose per-BSS frequency in a stable
    # column). `"2.4"` if ANY BSS was on 2.4 GHz; `"5"` if only 5 GHz
    # BSSes were seen; `None` if no `freq:` line matched. Preferring
    # 2.4 when both bands are present matches what the Pi's radio can
    # actually join (BCM43438 is 2.4 GHz only), so this field
    # doubles as the classifier for the DEGRADED card's
    # `not_found_or_5ghz` variant.
    freq_mhz: int | None = None
    band: str | None = None


class WifiScanResult(BaseModel):
    networks: list[WifiNetwork]
    source: str  # "nmcli" | "iw" | "airport" | "none"


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
      1. `nmcli -t -f SSID,SIGNAL,FREQ dev wifi` (NetworkManager) — PRIMARY
         on NM-managed devices. A raw one-shot `iw` scan on an NM-managed,
         *connected* wlan0 returns only the associated BSS (1 network),
         whereas nmcli returns the full list in both the connected and
         setup-AP states (2026-07-07 fix).
      2. `iw dev wlan0 scan` (non-NM Linux fallback). Needs CAP_NET_ADMIN;
         may require the backend to run as root or be granted a capability.
      3. `airport -s` (macOS — the legacy path; Apple deprecated from
         14.4 onwards and returns empty unless run as root).
      4. None / "none" source — UI falls back to manual SSID entry.

    2026-07-02 (audit 4b close-out): on the nmcli / iw paths we also feed the
    per-SSID band table into the NetworkSupervisor's
    `record_scan_bands()` so a subsequent STA_SSID_NOT_FOUND event
    classifies as `not_found_or_5ghz` when the target SSID was only
    visible on 5 GHz. The macOS `airport` path stays scan-only —
    airport doesn't expose per-BSS frequency in a stable column, so
    the WifiNetwork.band field is None and the classifier stays at
    its default `"not_found"`.
    """
    # NetworkManager path — PRIMARY on NM-managed devices (see docstring).
    if shutil.which("nmcli"):
        try:
            out = await asyncio.to_thread(
                subprocess.run,
                ["nmcli", "-t", "-f", "SSID,SIGNAL,FREQ", "dev", "wifi"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if out.returncode == 0:
                networks = _parse_nmcli_scan(out.stdout)
                if networks:
                    _feed_scan_bands_to_supervisor(networks)
                    return WifiScanResult(networks=networks, source="nmcli")
        except Exception:
            log.exception("nmcli scan failed")

    # Linux / Pi fallback (non-NM hosts).
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
                _feed_scan_bands_to_supervisor(networks)
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


def _feed_scan_bands_to_supervisor(networks: list[WifiNetwork]) -> None:
    """2026-07-02 (audit 4b close-out): push per-SSID band data into
    the process-wide NetworkSupervisor's classifier so a subsequent
    STA_SSID_NOT_FOUND event picks up the `not_found_or_5ghz`
    variant when appropriate.

    Import + fetch inline (not via FastAPI Depends) so this stays a
    fire-and-forget side effect: if the singleton isn't wired
    (early boot, mid-test, dev host without the supervisor
    initialized) the call is a silent no-op — the /api/system/
    wifi-scan endpoint's primary contract is the JSON response, not
    the classifier update.

    Only SSIDs with a `band` classification participate; SSIDs
    with `band is None` (no `freq:` line matched) are dropped so
    they don't clobber a previously-recorded band with an
    unknowable value.
    """
    bands: dict[str, str] = {n.ssid: n.band for n in networks if n.band is not None}
    try:
        from openmarquee.dependencies import get_network_supervisor

        supervisor = get_network_supervisor()
    except Exception:
        return
    try:
        supervisor.record_scan_bands(bands)
    except Exception:
        log.exception("failed to feed wifi-scan band table to supervisor")


# --- parsers ---


def _parse_iw_scan(output: str) -> list[WifiNetwork]:
    """Pull SSID + signal + band from `iw dev wlan0 scan` output.

    Each BSS block contains a `freq: <MHz>` line, `signal: -53.00 dBm`,
    and `SSID: foo`. Dedupe on SSID: keep the strongest signal, and
    aggregate band coverage across BSSes — if ANY BSS for this SSID
    was on 2.4 GHz, `band` is "2.4"; only 5 GHz BSSes → "5"; no
    freq line matched anywhere → None.

    Band aggregation across BSSes is deliberately 2.4-preferred:
    the BCM43438 radio on the Pi Zero 2 W is 2.4 GHz only, so an
    SSID visible on both bands IS reachable (via its 2.4 GHz BSS).
    An SSID whose only BSSes are 5 GHz is the exact case the
    DEGRADED card's `not_found_or_5ghz` variant surfaces.
    """
    networks: dict[str, WifiNetwork] = {}
    current_signal: int | None = None
    current_freq_mhz: int | None = None
    for line in output.splitlines():
        freq_m = re.match(r"\s*freq:\s*(\d+)", line)
        if freq_m:
            current_freq_mhz = int(freq_m.group(1))
            continue
        sig = re.match(r"\s*signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", line)
        if sig:
            current_signal = int(float(sig.group(1)))
            continue
        ssid_m = re.match(r"\s*SSID:\s*(.*)$", line)
        if ssid_m:
            ssid = ssid_m.group(1).strip()
            if not ssid:
                # Still reset the per-BSS accumulators so a hidden-
                # SSID BSS doesn't leak its freq/signal into the
                # next block.
                current_signal = None
                current_freq_mhz = None
                continue
            _merge_scanned_bss(networks, ssid, current_signal, current_freq_mhz)
            current_signal = None
            current_freq_mhz = None
    return _sorted_networks(networks)


def _merge_scanned_bss(
    networks: dict[str, WifiNetwork],
    ssid: str,
    signal_dbm: int | None,
    freq_mhz: int | None,
) -> None:
    """Merge one scanned BSS into the SSID-keyed dict. Dedupe policy
    (shared by the `iw` and `nmcli` parsers): strongest-signal wins for
    `signal_dbm`/`freq_mhz`; `band` is 2.4-preferred so an SSID visible
    on both bands classifies as reachable (the Pi's BCM43438 is 2.4 GHz
    only)."""
    band = _band_for_freq(freq_mhz)
    existing = networks.get(ssid)
    if existing is None:
        networks[ssid] = WifiNetwork(ssid=ssid, signal_dbm=signal_dbm, freq_mhz=freq_mhz, band=band)
        return
    stronger = signal_dbm is not None and (
        existing.signal_dbm is None or signal_dbm > existing.signal_dbm
    )
    merged_band = existing.band
    if band == "2.4" or merged_band is None:
        merged_band = band or merged_band
    networks[ssid] = WifiNetwork(
        ssid=ssid,
        signal_dbm=signal_dbm if stronger else existing.signal_dbm,
        freq_mhz=freq_mhz if stronger else existing.freq_mhz,
        band=merged_band,
    )


def _sorted_networks(networks: dict[str, WifiNetwork]) -> list[WifiNetwork]:
    """Sort by signal (strongest first), SSID as a stable tie-break."""
    return sorted(
        networks.values(),
        key=lambda n: (-(n.signal_dbm or -999), n.ssid),
    )


def _split_nmcli_terse(line: str) -> list[str]:
    r"""Split an ``nmcli -t`` line on unescaped ``:`` and unescape the
    ``\:`` / ``\\`` sequences nmcli emits in terse mode (realistically
    only SSID values carry them)."""
    fields = re.split(r"(?<!\\):", line)
    return [re.sub(r"\\(.)", r"\1", f) for f in fields]


def _nmcli_quality_to_dbm(quality: str) -> int | None:
    """nmcli's ``SIGNAL`` column is a 0-100 link-quality percentage, not
    dBm. NetworkManager derives that quality from RSSI, so invert it to
    an approximate dBm (100 -> -50, 0 -> -100) — keeps the UI's dBm
    display + the strongest-signal dedupe meaningful."""
    try:
        q = int(quality)
    except (TypeError, ValueError):
        return None
    q = max(0, min(100, q))
    return round(q / 2) - 100


def _parse_nmcli_freq(freq_field: str) -> int | None:
    """nmcli ``FREQ`` is e.g. ``"2412 MHz"``; pull the leading MHz int."""
    m = re.match(r"\s*(\d+)", freq_field or "")
    return int(m.group(1)) if m else None


def _parse_nmcli_scan(output: str) -> list[WifiNetwork]:
    """Parse ``nmcli -t -f SSID,SIGNAL,FREQ dev wifi`` — one line per
    visible BSS. Deduped/sorted via the shared helpers; hidden SSIDs
    (empty field) are dropped, matching the `iw` parser."""
    networks: dict[str, WifiNetwork] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = _split_nmcli_terse(line)
        if len(fields) < 3:
            continue
        ssid = fields[0].strip()
        if not ssid:
            continue
        _merge_scanned_bss(
            networks,
            ssid,
            _nmcli_quality_to_dbm(fields[1]),
            _parse_nmcli_freq(fields[2]),
        )
    return _sorted_networks(networks)


def _band_for_freq(freq_mhz: int | None) -> str | None:
    """Return `"2.4"` for 2.4 GHz-range freqs, `"5"` for 5 GHz range,
    None otherwise. The exact channel ↔ freq mapping stays in
    network_supervisor.freq_to_channel; this helper only needs
    coarse-grained band classification for the DEGRADED-card
    `not_found_or_5ghz` variant.
    """
    if freq_mhz is None:
        return None
    if 2400 <= freq_mhz <= 2500:
        return "2.4"
    if 4900 <= freq_mhz <= 5900:
        return "5"
    return None


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


class RestartResponse(BaseModel):
    status: str  # "restarting"
    message: str


@router.post("/restart", response_model=RestartResponse, status_code=202)
async def restart_device() -> RestartResponse:
    """Recovery A4: reboot the device.

    Spec §"Settings" future surface `/api/system/restart`. The backend
    runs under NoNewPrivileges so it can't reboot itself; the reboot is
    issued by the root netctl daemon (`reboot` subcommand →
    `systemctl reboot`). `systemctl reboot` only *enqueues* the reboot
    and returns, so this 202 flushes to the operator's browser before
    systemd tears the process down.

    The netctl round-trip is a blocking socket call; run it on a worker
    thread so a concurrent poll isn't stalled. A missing daemon socket
    (dev host) or daemon error surfaces as 503 rather than a silent
    no-op.
    """
    try:
        await asyncio.to_thread(system_control.reboot_device)
    except system_control.SystemControlError as e:
        log.error("restart failed: %s", e)
        raise HTTPException(
            status_code=503,
            detail=f"restart unavailable: {e}",
        ) from e
    return RestartResponse(
        status="restarting",
        message="Device is rebooting; it will be back in about a minute.",
    )


class FactoryResetRequest(BaseModel):
    # Explicit confirmation token. Bearer auth alone gates WHO can call
    # this; the token guards against an accidental / mis-routed POST
    # triggering a destructive wipe. Must equal "factory-reset".
    confirm: str


class FactoryResetResponse(BaseModel):
    status: str  # "resetting"
    message: str


_FACTORY_RESET_CONFIRM = "factory-reset"


@router.post("/factory-reset", response_model=FactoryResetResponse, status_code=202)
async def factory_reset(
    body: FactoryResetRequest,
    settings: SettingsDep,
    flock: FlockDep,
    playlist: Annotated[PlaylistStorage, Depends(get_playlist_storage)],
    schedule: Annotated[ScheduleStorage, Depends(get_schedule_storage)],
    content: Annotated[ContentStorage, Depends(get_content_storage)],
    auth: Annotated[object, Depends(get_auth_storage)],
    tombstone: Annotated[TombstoneStorage, Depends(get_tombstone_storage)],
    seed_marker: Annotated[Path, Depends(get_seed_marker_path)],
) -> FactoryResetResponse:
    """Recovery A3 (DESTRUCTIVE): erase operator data + wifi and reboot
    into a fresh setup state. Spec §"Settings" future surface
    `/api/system/factory-reset` — "clears all content + restores
    defaults."

    Two gates: bearer auth (WHO) + an explicit `confirm` token (guards
    against an accidental fire). Device IDENTITY (hostname + AP
    SSID/passphrase) is preserved so the operator's label/QR stays valid.

    The data wipe (backend-owned files + uploaded content) runs first;
    the privileged wifi teardown + reboot go through the root netctl
    daemon. Blocking bits run on a worker thread; a missing/erroring
    daemon socket surfaces as 503 (the data is already wiped, so a manual
    reboot would still land in the fresh state).

    The wipe returns the device to its true fresh-flash state:
      - operator content/config: settings, playlist, schedule, flock,
        network-supervisor state, all uploaded content, tombstones;
      - the operator password (auth.json) — so a new operator can claim
        the device via the welcome flow (the current bearer-gated
        operator is the one triggering this, e.g. for a handover);
      - the seed marker — so default demo content re-seeds on next boot;
      - the plaintext wifi-prefill copy at /var/openmarquee (the /etc
        wpa configs + saved NM profiles are torn down daemon-side).
    Device identity (hostname, AP SSID/passphrase, firstboot .bootstrapped
    marker) is deliberately NOT in the set.
    """
    if body.confirm != _FACTORY_RESET_CONFIRM:
        raise HTTPException(
            status_code=400,
            detail=f"factory reset requires confirm={_FACTORY_RESET_CONFIRM!r}",
        )
    # Network-supervisor persisted state lives outside the storage
    # objects; wipe it too so the supervisor boots fresh into SETUP.
    from openmarquee.network_supervisor import DEFAULT_STATE_FILE

    data_files = [
        settings.path,
        playlist.path,
        schedule.path,
        flock.path,
        DEFAULT_STATE_FILE,
        auth.path,
        tombstone.path,
        seed_marker,
        # Plaintext wifi-prefill residue (a readable PSK copy the image
        # may drop here). The /etc/wpa_supplicant configs are root-owned
        # and removed daemon-side; this /var copy is backend-writable.
        Path("/var/openmarquee/wpa_supplicant.conf"),
    ]
    try:
        await asyncio.to_thread(
            system_control.factory_reset_device,
            data_files=data_files,
            content_root=content.root,
        )
    except system_control.SystemControlError as e:
        log.error("factory-reset failed: %s", e)
        raise HTTPException(
            status_code=503,
            detail=f"factory reset unavailable: {e}",
        ) from e
    return FactoryResetResponse(
        status="resetting",
        message="Factory reset in progress; the sign will erase and restart.",
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
    # LEVER 2 lazy-imports (2026-06-24): defer the Pillow-bringing
    # modules to the first /perf-stats call. Prod devices with
    # OPENMARQUEE_DISABLE_DEV=1 (and any device that simply doesn't
    # hit this endpoint) keep Pillow out of process RSS entirely.
    from openmarquee import auto_render
    from openmarquee.text_raster import font_cache_info

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


# 2026-05-25 Bundle A item 3 (security DiD): CSP report-uri sink.
#
# csp_middleware.DEFAULT_CSP_POLICY ends with `report-uri /api/system/
# csp-report`. Browsers POST a JSON body shaped like
# `{"csp-report": {"violated-directive": "...", "blocked-uri": "...",
# ...}}` (Content-Type: application/csp-report on modern browsers, or
# application/json on older ones) to that URL when CSP rejects a
# resource. The carve-out for unauth access lives in
# auth_middleware._WHITELIST_EXACT (narrow: this single endpoint).
#
# The handler does only one thing: log the body at WARNING level so
# operators see the violation in journald. NO persistence (no DB
# write, no file write -- journald rotation handles retention) and
# NO response-shape surface beyond 204. Read the body raw and json-
# parse defensively; malformed reports must not throw because the
# browser sent them, not the operator, and the operator can't fix
# a malformed CSP report client-side.
@router.post("/csp-report", status_code=204)
async def csp_report(request: Request) -> Response:
    try:
        body_bytes = await request.body()
    except Exception:
        log.warning("csp-report: failed to read request body")
        return Response(status_code=204)
    if not body_bytes:
        log.warning("csp-report: empty body")
        return Response(status_code=204)
    try:
        # Browsers may post bytes-with-BOM, oddly-cased keys, or
        # nested-under-"csp-report" envelope; just log the raw text
        # rather than trying to schema-validate (a hostile bug-class
        # is exactly what we want to surface, not silence).
        text = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        text = repr(body_bytes[:512])
    # 1024-char cap on the log line; CSP reports are usually small but
    # an attacker with the unauth POST surface could try to log-flood.
    # Truncate defensively; the directive name is always near the
    # front so the truncation doesn't hide the actionable bit.
    if len(text) > 1024:
        text = text[:1024] + "...(truncated)"
    log.warning("csp-report: %s", text)
    return Response(status_code=204)


# LEVER 2 (2026-06-24): env-gated memory snapshot for QA's
# top-allocator attribution of the ~140 MB Python footprint.
#
# Enabled when OPENMARQUEE_MEMORY_TRACE=1 was set at process start
# (app.py lifespan starts tracemalloc early so the snapshot covers
# startup allocations). 503 if not enabled — tracemalloc costs
# ~10-15% memory + CPU when active, so it's strictly opt-in.
#
# Returns the top N (default 20) allocation sites by current size,
# each carrying the source file:lineno + per-site cumulative bytes
# and block count. The output format is whatever pip-friendly QA
# needs to grep — a flat JSON array under `top` plus a summary
# `total_traced_kb` so QA can sanity-check the snapshot against
# /proc/<pid>/status's VmData.
class MemorySnapshotEntry(BaseModel):
    """One allocation site in the tracemalloc snapshot."""

    source: str
    size_kb: float
    count: int


class MemorySnapshot(BaseModel):
    """tracemalloc snapshot summary + top allocators."""

    enabled: bool
    total_traced_kb: float
    top: list[MemorySnapshotEntry]


@router.get("/memory-snapshot", response_model=MemorySnapshot)
async def memory_snapshot(limit: int = 20) -> MemorySnapshot:
    """Return the top-N tracemalloc allocators by current size.

    Requires OPENMARQUEE_MEMORY_TRACE=1 at process start — app.py's
    lifespan starts tracemalloc early so this snapshot covers
    startup allocations. Otherwise 503.

    `limit` is the row count cap; default 20, max 200 to keep
    responses bounded if QA forgets a small value mid-debug.
    """
    import tracemalloc

    if not tracemalloc.is_tracing():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "memory_trace_disabled",
                "hint": "set OPENMARQUEE_MEMORY_TRACE=1 in the systemd drop-in "
                "and restart the backend; tracemalloc starts in lifespan",
            },
        )

    capped_limit = max(1, min(int(limit), 200))
    snapshot = tracemalloc.take_snapshot()
    # Group by source file:lineno so QA sees one row per source
    # site (default tracemalloc grouping). Top-N by current size.
    stats = snapshot.statistics("lineno")[:capped_limit]

    total_bytes = sum(s.size for s in snapshot.statistics("filename"))
    return MemorySnapshot(
        enabled=True,
        total_traced_kb=total_bytes / 1024.0,
        top=[
            MemorySnapshotEntry(
                source=str(stat.traceback),
                size_kb=stat.size / 1024.0,
                count=stat.count,
            )
            for stat in stats
        ],
    )


# ============================================================
# PR3 (2026-06-27) — auth-gated onboarding-card preview endpoint.
#
# POST /api/system/render-system-card-preview lets QA drive one
# specific card state onto the sign for glass-verify WITHOUT
# driving the full supervisor state machine (which would toggle
# real wifi state on production and risk stranding the sign).
#
# Auth-gated by NOT being in auth_middleware's allowlist — every
# `/api/system/*` route except `/csp-report` requires a bearer
# token, so this inherits the same operator-only trust boundary.
# ============================================================


# PR3 finish-pass (2026-07-01): field byte-caps mirror the Rust-side
# clamps in renderer/src/system_card.rs so an oversized payload
# fails HERE (HTTP 422) with a clear error rather than being
# silently truncated on the paint side.
_MAX_SSID_LEN = 40
_MAX_PIN_LEN = 12
_MAX_QR_PAYLOAD_LEN = 256
_MAX_ADDRESS_LEN = 128
_MAX_IP_LEN = 45
_MAX_BOOT_HINT_LEN = 96
_ALLOWED_KINDS = frozenset({"SETUP", "CONNECTING", "CONNECTED", "DEGRADED", "BOOT"})
_ALLOWED_VARIANTS = frozenset({"lost", "auth_fail", "not_found", "not_found_or_5ghz"})


class RenderSystemCardPreviewRequest(BaseModel):
    """Auth-gated preview endpoint payload. Every field except `kind`
    is optional so QA can render minimal cards; the renderer's per-
    kind layout falls back to sensible defaults where needed."""

    kind: str
    ssid: str | None = None
    pin: str | None = None
    qr_payload: str | None = None
    address: str | None = None
    ip: str | None = None
    target_ssid: str | None = None
    variant: str | None = None
    ttl_ms: int | None = None
    boot_hint: str | None = None


class RenderSystemCardPreviewResponse(BaseModel):
    status: str


def _check_len(field: str, value: str | None, cap: int) -> None:
    """PR3 fix-pass S2 (2026-07-01): byte-length check to match the
    Rust-side `system_card::clamp_params` byte truncation. Prior
    version used Python's `len(str)` which counts codepoints, so a
    UTF-8 multi-byte SSID / target-SSID could pass here but hit the
    Rust byte-cap on paint.
    """
    if value is None:
        return
    if len(value.encode("utf-8")) > cap:
        raise HTTPException(
            status_code=422,
            detail=f"{field} exceeds {cap}-byte cap",
        )


@router.post(
    "/render-system-card-preview",
    response_model=RenderSystemCardPreviewResponse,
)
async def render_system_card_preview(
    body: RenderSystemCardPreviewRequest,
) -> RenderSystemCardPreviewResponse:
    """PR3 (2026-06-27) glass-verify hook: push one RenderSystemCard
    to the live renderer so QA can inspect each card state on the
    sign without driving the supervisor state machine (which would
    toggle real wifi state on production).

    Strand-safe: it does NOT touch the supervisor or the wifi
    stack — only the renderer's overlay slot. The next real
    supervisor transition (or the ttl_ms elapsing) reverts the
    overlay. Also strand-safe when the renderer is unhealthy: an
    IPC failure is caught inside the RustRenderer wrapper and
    downgraded to a warn log.
    """
    kind = body.kind.upper()
    if kind not in _ALLOWED_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"kind must be one of {sorted(_ALLOWED_KINDS)}; got {body.kind!r}",
        )
    if body.variant is not None and body.variant not in _ALLOWED_VARIANTS:
        raise HTTPException(
            status_code=422,
            detail=f"variant must be one of {sorted(_ALLOWED_VARIANTS)}; got {body.variant!r}",
        )
    _check_len("ssid", body.ssid, _MAX_SSID_LEN)
    _check_len("pin", body.pin, _MAX_PIN_LEN)
    _check_len("qr_payload", body.qr_payload, _MAX_QR_PAYLOAD_LEN)
    _check_len("address", body.address, _MAX_ADDRESS_LEN)
    _check_len("ip", body.ip, _MAX_IP_LEN)
    _check_len("target_ssid", body.target_ssid, _MAX_ADDRESS_LEN)
    _check_len("boot_hint", body.boot_hint, _MAX_BOOT_HINT_LEN)
    if body.ttl_ms is not None and (body.ttl_ms < 0 or body.ttl_ms > 3_600_000):
        raise HTTPException(
            status_code=422,
            detail="ttl_ms must be in [0, 3_600_000]",
        )

    # Build the params dict — drop None fields so the Rust side's
    # `#[serde(default)]` picks them up as absent rather than
    # explicit null.
    params: dict[str, object] = {"kind": kind}
    for name in (
        "ssid",
        "pin",
        "qr_payload",
        "address",
        "ip",
        "target_ssid",
        "variant",
        "ttl_ms",
        "boot_hint",
    ):
        val = getattr(body, name)
        if val is not None:
            params[name] = val

    # Import locally so the preview endpoint doesn't force the
    # renderer singleton to materialize on import — matches the
    # deferred-import pattern the rest of api_system.py uses.
    from openmarquee.dependencies import get_renderer

    renderer = get_renderer()
    # PR3 fix-pass B2 (2026-07-01): the RustRenderer IPC call
    # blocks on the subprocess RLock + JSON readline for up to
    # ~10s (~18s on cold-start). Running it inline freezes the
    # captive-portal HTTP the operator may be mid-onboarding on.
    # Off-load to a worker thread so the event loop keeps polling
    # concurrent requests — same pattern as the sibling /api/system
    # endpoints (fbset/iw/tailscale) above.
    #
    # PR3 fix-pass F3 (2026-07-01): under the AutoFallbackRenderer
    # the primary's `render_system_card` no longer swallows
    # subprocess errors (F1), but the swap-to-mock is also gone
    # (F1), so a dead subprocess renders SUCCESSFULLY on the mock
    # AFTER the swap in a legacy code path. The AutoFallback
    # wrapper we ship in this pass propagates errors on the card
    # path — but we also verify that the render actually landed
    # on the REAL renderer (not the mock) by checking
    # `is_in_fallback` after the call. If the primary was already
    # in fallback (or the exception path put us there), the card
    # was painted on the mock — return 502 so QA glass-verifies
    # on truthful responses.
    try:
        await asyncio.to_thread(renderer.render_system_card, params)
    except Exception as e:  # noqa: BLE001
        log.warning("render_system_card_preview: renderer.render_system_card failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"renderer render_system_card failed: {e}",
        ) from e
    if _is_in_fallback(renderer):
        raise HTTPException(
            status_code=502,
            detail=(
                "renderer is in fallback (mock) — card painted on the mock, not the real display"
            ),
        )
    return RenderSystemCardPreviewResponse(status="rendered")


@router.post(
    "/clear-system-card-preview",
    response_model=RenderSystemCardPreviewResponse,
)
async def clear_system_card_preview() -> RenderSystemCardPreviewResponse:
    """PR3 (2026-06-27) glass-verify hook: push a ClearSystemCard.
    Companion to render-system-card-preview so QA can revert to
    the playlist paint without waiting for a ttl to elapse.
    """
    from openmarquee.dependencies import get_renderer

    renderer = get_renderer()
    # PR3 fix-pass B2 (2026-07-01): same asyncio.to_thread off-load
    # as render-system-card-preview.
    try:
        await asyncio.to_thread(renderer.clear_system_card)
    except Exception as e:  # noqa: BLE001
        log.warning("clear_system_card_preview: renderer.clear_system_card failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"renderer clear_system_card failed: {e}",
        ) from e
    if _is_in_fallback(renderer):
        raise HTTPException(
            status_code=502,
            detail=(
                "renderer is in fallback (mock) — clear applied to the mock, not the real display"
            ),
        )
    return RenderSystemCardPreviewResponse(status="cleared")


def _is_in_fallback(renderer: object) -> bool:
    """PR3 fix-pass F3 (2026-07-01): true iff the renderer is
    currently painting on the MockRenderer (i.e. the primary
    Rust IPC path is down). `AutoFallbackRenderer` exposes this
    as a property; bare RustRenderer / MockRenderer do not, so
    getattr with default False keeps unit tests that inject a
    minimal renderer stub happy."""
    try:
        return bool(getattr(renderer, "is_in_fallback", False))
    except Exception:  # noqa: BLE001
        return False
