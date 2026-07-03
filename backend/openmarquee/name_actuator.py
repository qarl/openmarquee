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
    """Route `hostnamectl set-hostname <name>` through the netctl
    socket daemon. 2026-07-03 (QA FIX 1): the backend runs under
    NoNewPrivileges, which blocks the direct subprocess call — the
    daemon does the crossing as root.
    """
    from openmarquee.network_supervisor_actuator import _netctl_send

    class _HostnamectlError(RuntimeError):
        pass

    try:
        _netctl_send(
            "hostnamectl-set-hostname",
            (name + "\n").encode("utf-8"),
            timeout_s=_SUBPROCESS_TIMEOUT_S,
            error_cls=_HostnamectlError,
        )
    except _HostnamectlError as exc:
        log.warning(
            "name-actuator: hostnamectl set-hostname failed via netctl (%s); "
            "next sign_name-changing PUT retries",
            type(exc).__name__,
        )


def _apply_tailscale_hostname(name: str) -> None:
    """Route `tailscale set --hostname <name>` through the netctl
    socket daemon. 2026-07-03 (QA FIX 1)."""
    from openmarquee.network_supervisor_actuator import _netctl_send

    class _TailscaleError(RuntimeError):
        pass

    try:
        _netctl_send(
            "tailscale-set-hostname",
            (name + "\n").encode("utf-8"),
            timeout_s=_SUBPROCESS_TIMEOUT_S,
            error_cls=_TailscaleError,
        )
    except _TailscaleError as exc:
        log.warning(
            "name-actuator: tailscale set --hostname failed via netctl (%s); "
            "next sign_name-changing PUT retries",
            type(exc).__name__,
        )


def _apply_avahi_hostname(name: str) -> None:
    """Render the full avahi-daemon.conf with the new host-name +
    ship it via the netctl `avahi-write-and-restart` subcommand
    (2026-07-03 QA FIX 1 — the daemon writes the file as root +
    restarts avahi-daemon).

    The render step reads the current conf via the filesystem
    (which the backend user CAN do — read is unprivileged) and
    substitutes the `host-name=` line. Skipped when the source
    conf doesn't exist (dev hosts, fresh installs).
    """
    from openmarquee.network_supervisor_actuator import _netctl_send

    if not _AVAHI_CONF.exists():
        log.info("name-actuator: %s missing; skipping mDNS hostname update", _AVAHI_CONF)
        return
    try:
        original = _AVAHI_CONF.read_text()
    except OSError as exc:
        log.warning("name-actuator: read %s failed: %r", _AVAHI_CONF, exc)
        return
    rewritten = _substitute_hostname_line(original, name)
    if rewritten == original:
        return  # Already at target value; skip round-trip.

    class _AvahiError(RuntimeError):
        pass

    try:
        _netctl_send(
            "avahi-write-and-restart",
            rewritten.encode("utf-8"),
            timeout_s=_SUBPROCESS_TIMEOUT_S,
            error_cls=_AvahiError,
        )
    except _AvahiError as exc:
        log.warning(
            "name-actuator: avahi-write-and-restart failed via netctl (%s); "
            "next sign_name-changing PUT retries",
            type(exc).__name__,
        )


def _apply_hostapd_ssid(name: str) -> None:
    """Re-render the hostapd.conf with the new SSID + ship via the
    existing `hostapd-write-and-restart` netctl subcommand (2026-
    07-03 QA FIX 1 — reuse the already-sanctioned crossing rather
    than reimplement it unprivileged)."""
    from openmarquee.network_supervisor_actuator import _netctl_send

    if not _HOSTAPD_CONF.exists():
        log.info("name-actuator: %s missing; skipping setup-AP SSID update", _HOSTAPD_CONF)
        return
    try:
        original = _HOSTAPD_CONF.read_text()
    except OSError as exc:
        log.warning("name-actuator: read %s failed: %r", _HOSTAPD_CONF, exc)
        return
    rewritten = _substitute_ssid_line(original, name)
    if rewritten == original:
        return

    class _HostapdError(RuntimeError):
        pass

    try:
        _netctl_send(
            "hostapd-write-and-restart",
            rewritten.encode("utf-8"),
            timeout_s=_SUBPROCESS_TIMEOUT_S,
            error_cls=_HostapdError,
        )
    except _HostapdError as exc:
        log.warning(
            "name-actuator: hostapd-write-and-restart failed via netctl (%s); "
            "next sign_name-changing PUT retries",
            type(exc).__name__,
        )


def _substitute_hostname_line(conf_text: str, name: str) -> str:
    """Pure substitution: replace `#?host-name=…` in avahi conf.
    Appends a fresh `host-name=<name>` line when absent."""
    rewritten, matched = re.subn(
        r"^\s*#?\s*host-name\s*=.*$",
        f"host-name={name}",
        conf_text,
        count=1,
        flags=re.MULTILINE,
    )
    if matched == 0:
        rewritten = conf_text.rstrip() + f"\nhost-name={name}\n"
    return rewritten


def _substitute_ssid_line(conf_text: str, name: str) -> str:
    """Pure substitution: replace `#?ssid=…` in hostapd.conf. Same
    semantics as _substitute_hostname_line."""
    rewritten, matched = re.subn(
        r"^\s*#?\s*ssid\s*=.*$",
        f"ssid={name}",
        conf_text,
        count=1,
        flags=re.MULTILINE,
    )
    if matched == 0:
        rewritten = conf_text.rstrip() + f"\nssid={name}\n"
    return rewritten
