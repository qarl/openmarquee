"""System-probe endpoints used by the Settings UI to offer sensible
defaults: display-dim detection (so first-boot picks up the real
framebuffer res) and WiFi-scan (so the station-mode dropdown lists
what's actually reachable).

Both endpoints best-effort — they shell out to tools that exist on
real hardware (fbset / iw / airport) and return empty-but-well-formed
payloads on dev boxes without those tools. The UI falls back to
letting the operator type values manually.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])


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
            out = subprocess.run(
                ["fbset", "-i"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            m = re.search(r'geometry\s+(\d+)\s+(\d+)', out.stdout)
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
            out = subprocess.run(
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
        "/System/Library/PrivateFrameworks/Apple80211.framework/"
        "Versions/Current/Resources/airport"
    )
    if airport.exists():
        try:
            out = subprocess.run(
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
            if (
                existing is None
                or (
                    current_signal is not None
                    and (
                        existing.signal_dbm is None
                        or current_signal > existing.signal_dbm
                    )
                )
            ):
                networks[ssid] = WifiNetwork(ssid=ssid, signal_dbm=current_signal)
            current_signal = None
    return sorted(
        networks.values(),
        key=lambda n: (-(n.signal_dbm or -999), n.ssid),
    )


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
        ssid = line[: m.start()].rstrip()
        if not ssid:
            continue
        rest = line[m.end():].split()
        rssi: int | None = None
        if rest:
            try:
                rssi = int(rest[0])
            except ValueError:
                rssi = None
        existing = networks.get(ssid)
        if existing is None or (
            rssi is not None
            and (existing.signal_dbm is None or rssi > existing.signal_dbm)
        ):
            networks[ssid] = WifiNetwork(ssid=ssid, signal_dbm=rssi)
    return sorted(
        networks.values(),
        key=lambda n: (-(n.signal_dbm or -999), n.ssid),
    )
