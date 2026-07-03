"""2026-07-03 (qarl handover Phase B1): make `sign_name` the single
source of truth for device identity. On rename, propagate the value
to four downstream consumers:

  1. system hostname via `hostnamectl set-hostname`
  2. Tailscale node hostname via `tailscale set --hostname`
  3. setup-AP SSID via `/etc/hostapd/hostapd.conf` rewrite + reload
  4. mDNS host-name via `/etc/avahi/avahi-daemon.conf` rewrite +
     restart

Each sub-actuator is FAIL-SOFT: subprocess errors, missing binaries,
missing config files, and non-zero returns are logged as warnings
and don't raise. The settings PUT handler never 500s because of a
name-propagation failure — the settings value is authoritative and
the actuators run in a background thread so a failed hostapd
restart doesn't wedge the API.

`sign_name` values arriving here are already DNS-safe (the field
validator on SystemSettings normalises whitespace + strips
non-safe chars). This module doesn't re-validate — anything that
reaches it has been through the model.

QA 2026-07-03 sharp-knives constraints honored:
  * NEVER crash the backend on failure.
  * NEVER touch anything but the four consumers named above.
  * `tailscale set --hostname` only fires when tailscale is on
    PATH — dev hosts without tailscaled skip it silently.
  * hostapd + avahi conf rewrites are guarded on file exists so a
    dev-host / fresh device without those files is a no-op.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_HOSTAPD_CONF = Path("/etc/hostapd/hostapd.conf")
_AVAHI_CONF = Path("/etc/avahi/avahi-daemon.conf")

# Subprocess timeouts. hostnamectl + tailscale + systemctl are all
# near-instant on a healthy device; 15s bounds a wedged runtime
# without letting it stall the background thread indefinitely.
_SUBPROCESS_TIMEOUT_S = 15.0


def apply_sign_name(name: str) -> None:
    """Propagate `name` to hostname / Tailscale / setup-AP SSID /
    mDNS host-name. Each sub-actuator is independent + fail-soft;
    a failure in one doesn't skip the others.

    Order chosen so the most-visible-to-the-operator change
    (Tailscale hostname, since qarl reaches devices via Tailscale)
    fires early. hostapd + avahi restarts are last because they
    can briefly interrupt the captive-portal AP / mDNS discovery
    respectively.
    """
    _apply_hostnamectl(name)
    _apply_tailscale_hostname(name)
    _apply_avahi_hostname(name)
    _apply_hostapd_ssid(name)


def apply_in_background(name: str) -> threading.Thread:
    """Dispatch `apply_sign_name` on a daemon thread so the settings
    PUT handler returns immediately. Matches the shape of
    `wifi_station.apply_in_background`. Returns the started thread
    so tests can join() if they need to await."""

    def _runner() -> None:
        with contextlib.suppress(Exception):
            apply_sign_name(name)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return thread


# --- individual sub-actuators, each fail-soft ---


def _apply_hostnamectl(name: str) -> None:
    """`hostnamectl set-hostname <name>` — sets the transient +
    static hostname. Skipped when hostnamectl isn't on PATH (dev
    hosts, minimal containers)."""
    hostnamectl = shutil.which("hostnamectl")
    if not hostnamectl:
        log.info("name-actuator: hostnamectl not on PATH; skipping hostname update")
        return
    try:
        result = subprocess.run(
            [hostnamectl, "set-hostname", name],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("name-actuator: hostnamectl set-hostname failed: %r", exc)
        return
    if result.returncode != 0:
        log.warning(
            "name-actuator: hostnamectl set-hostname returned rc=%d stderr=%r",
            result.returncode,
            result.stderr.strip()[:200],
        )


def _apply_tailscale_hostname(name: str) -> None:
    """`tailscale set --hostname <name>` — updates the node's tailnet
    hostname. Skipped when tailscale isn't on PATH."""
    tailscale = shutil.which("tailscale")
    if not tailscale:
        log.info("name-actuator: tailscale not on PATH; skipping tailnet hostname update")
        return
    try:
        result = subprocess.run(
            [tailscale, "set", "--hostname", name],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("name-actuator: tailscale set --hostname failed: %r", exc)
        return
    if result.returncode != 0:
        log.warning(
            "name-actuator: tailscale set --hostname returned rc=%d stderr=%r",
            result.returncode,
            result.stderr.strip()[:200],
        )


def _apply_avahi_hostname(name: str) -> None:
    """Rewrite `/etc/avahi/avahi-daemon.conf`'s `host-name=` line +
    `systemctl restart avahi-daemon`. Skipped when the file doesn't
    exist (dev hosts, fresh installs) or when systemctl isn't
    available.
    """
    if not _AVAHI_CONF.exists():
        log.info("name-actuator: %s missing; skipping mDNS hostname update", _AVAHI_CONF)
        return
    try:
        original = _AVAHI_CONF.read_text()
    except OSError as exc:
        log.warning("name-actuator: read %s failed: %r", _AVAHI_CONF, exc)
        return
    rewritten, matched = re.subn(
        r"^\s*#?\s*host-name\s*=.*$",
        f"host-name={name}",
        original,
        count=1,
        flags=re.MULTILINE,
    )
    if matched == 0:
        # File exists but has no host-name line — append one so a
        # future manual edit doesn't inherit stale state.
        rewritten = original.rstrip() + f"\nhost-name={name}\n"
    if rewritten == original:
        # Already set to the target value — no restart needed.
        return
    try:
        _AVAHI_CONF.write_text(rewritten)
    except OSError as exc:
        log.warning("name-actuator: write %s failed: %r", _AVAHI_CONF, exc)
        return
    _systemctl_restart("avahi-daemon")


def _apply_hostapd_ssid(name: str) -> None:
    """Rewrite `/etc/hostapd/hostapd.conf`'s `ssid=` line +
    `systemctl restart hostapd`. Skipped when the file doesn't
    exist. This changes the setup-AP SSID so a phone that scans
    for a NEW `<sign_name>-SETUP`-shaped network finds it; existing
    setup sessions in flight will drop briefly during the restart.
    """
    if not _HOSTAPD_CONF.exists():
        log.info("name-actuator: %s missing; skipping setup-AP SSID update", _HOSTAPD_CONF)
        return
    try:
        original = _HOSTAPD_CONF.read_text()
    except OSError as exc:
        log.warning("name-actuator: read %s failed: %r", _HOSTAPD_CONF, exc)
        return
    rewritten, matched = re.subn(
        r"^\s*#?\s*ssid\s*=.*$",
        f"ssid={name}",
        original,
        count=1,
        flags=re.MULTILINE,
    )
    if matched == 0:
        rewritten = original.rstrip() + f"\nssid={name}\n"
    if rewritten == original:
        return
    try:
        _HOSTAPD_CONF.write_text(rewritten)
    except OSError as exc:
        log.warning("name-actuator: write %s failed: %r", _HOSTAPD_CONF, exc)
        return
    _systemctl_restart("hostapd")


def _systemctl_restart(unit: str) -> None:
    """`systemctl restart <unit>` — fail-soft."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        log.info("name-actuator: systemctl not on PATH; skipping %s restart", unit)
        return
    try:
        result = subprocess.run(
            [systemctl, "restart", unit],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("name-actuator: systemctl restart %s failed: %r", unit, exc)
        return
    if result.returncode != 0:
        log.warning(
            "name-actuator: systemctl restart %s returned rc=%d stderr=%r",
            unit,
            result.returncode,
            result.stderr.strip()[:200],
        )
