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
import json
import logging
import re
import shutil
import socket
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

    2026-07-07 (qarl Option A): a UI rename must also clear the stored
    tailscale_hostname + sync wifi_ssid so they can't drift from the sign
    name — otherwise `openmarquee-tailscale.sh` would re-pin a stale
    tailnet name at the next boot. `_reconcile_stored_name_fields` is
    fail-soft (its own load/save guard) so it never wedges a rename.
    """
    _apply_hostnamectl(name)
    _apply_tailscale_hostname(name)
    _apply_avahi_hostname(name)
    _apply_hostapd_ssid(name)
    _reconcile_stored_name_fields(name)


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


def reconcile_names_from_hostname_at_boot() -> None:
    """One-name-everywhere boot reconcile (2026-07-07, qarl-approved:
    "enforce the sync as much as possible"). Re-derive EVERY name surface
    from the device's CURRENT hostname so an out-of-band rename (e.g.
    `hostnamectl set-hostname` directly, as on fireplaceSign ->
    JasonsSign1) never leaves any surface stale.

    Hostname = source of truth: an out-of-band rename leaves the stored
    settings value stale, whereas the hostname is what the device
    actually is (same reasoning as the boot card's hostname-derived mDNS
    URL, `mdns.mdns_url`). We SKIP hostnamectl (the hostname IS the
    source) and run the remaining sub-actuators from it:
      * Tailscale node name    — STRICT no-op when in sync (the tailnet
        name is the operator's SSH lane; never re-set it needlessly).
      * mDNS (avahi) host-name — no-op when the conf already matches.
      * setup-AP SSID (hostapd) — no-op when `ssid=` already matches.
      * stored SystemSettings (sign_name / tailscale_hostname / wifi_ssid)
        — sign_name follows, tailscale_hostname is CLEARED so Tailscale
        tracks the OS hostname, wifi_ssid follows (see
        `_reconcile_stored_name_fields`). No-op when already in sync.
    Each sub-actuator is idempotent + fail-soft, so this is safe to call
    on every boot; on a fully in-sync device it changes NOTHING.
    """
    try:
        host = socket.gethostname().strip().split(".")[0]
    except OSError:
        return
    if not host:
        return
    _apply_tailscale_hostname(host)
    _apply_avahi_hostname(host)
    _apply_hostapd_ssid(host)
    _reconcile_stored_name_fields(host)


def _reconcile_stored_name_fields(name: str) -> None:
    """Sync the STORED name-bearing settings fields to the device's current
    identity `name` (the hostname leaf at boot, or the new sign_name on a
    UI rename). qarl 2026-07-07 (Option A — "names always follow the sign
    hostname; a custom override does NOT stick through a rename"):

      * sign_name          -> `name` (follows the hostname; the #7 case).
      * tailscale_hostname -> CLEARED (None). The live rename is done by
        `tailscale set --hostname=name` (the sub-actuator above / the boot
        actuator); clearing the STORED field stops `openmarquee-tailscale.sh`
        re-pinning a stale `--hostname` at the next boot (empty => it omits
        the flag, so tailscaled keeps the OS-hostname-tracked name). Belt-
        and-braces: clearing can never point the SSH lane at a stale name,
        and it defuses a stale stored override (e.g. `fireplacesign` left on
        a renamed sign).
      * wifi_ssid          -> `name` (the setup-AP SSID; can't be empty, so
        it's synced rather than cleared). Capped at the 32-byte SSID max.

    One load + at most one save. Built through FULL model validation (NOT
    model_copy(update=), which skips the field validators) so we (a) store
    the normalised form the next load produces — a true no-op next boot —
    and (b) SKIP-not-persist a value that fails validation, which would
    quarantine settings.json to FACTORY DEFAULTS (wiping the AP passphrase,
    Tailscale key, wifi_networks) on the next load — catastrophic on a live
    handover device. Fail-soft; `storage.save` is a plain persist, so no
    reconcile loop.
    """
    try:
        from openmarquee.dependencies import get_settings_storage

        storage = get_settings_storage()
        settings = storage.load()
    except Exception:
        log.debug(
            "name-actuator: settings load failed; skipping stored-name reconcile",
            exc_info=True,
        )
        return
    desired = {
        "sign_name": name,
        "tailscale_hostname": None,  # empty => Tailscale follows the OS hostname
        "wifi_ssid": name[:32],  # IEEE 802.11 SSID max is 32 bytes
    }
    try:
        candidate = type(settings).model_validate(settings.model_dump() | desired)
    except Exception:
        log.debug(
            "name-actuator: hostname %r not valid for stored name fields; skipping reconcile",
            name,
            exc_info=True,
        )
        return
    if (
        candidate.sign_name == settings.sign_name
        and candidate.tailscale_hostname == settings.tailscale_hostname
        and candidate.wifi_ssid == settings.wifi_ssid
    ):
        return  # every stored name field already in sync
    try:
        storage.save(candidate)
    except Exception:
        log.warning(
            "name-actuator: stored-name reconcile write failed; next rename / boot retries",
            exc_info=True,
        )


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


def _current_tailscale_hostname() -> str | None:
    """The device's CURRENT Tailscale node hostname (lowercased), or None
    when unreadable — tailscale off PATH, tailscaled not up yet at boot,
    or `status` errored / returned non-JSON.

    Reads `Self.HostName` from `tailscale status --json` (the field
    `tailscale set --hostname` writes; unprivileged read). Used to make
    `_apply_tailscale_hostname` strictly idempotent so the boot-time
    reconcile never re-sets an in-sync tailnet name — that name is the
    operator's SSH lane.
    """
    if not shutil.which("tailscale"):
        return None
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    self_node = payload.get("Self")
    if not isinstance(self_node, dict):
        return None
    host = (self_node.get("HostName") or "").strip().lower()
    return host or None


def _apply_tailscale_hostname(name: str) -> None:
    """Route `tailscale set --hostname <name>` through the netctl socket
    daemon. 2026-07-03 (QA FIX 1).

    2026-07-07 (one-name-everywhere): STRICTLY idempotent — read the
    current tailnet hostname first and only `set` on genuine drift. The
    boot-time reconcile calls this every boot; the tailnet name is the
    operator's SSH lane, so re-setting it (even to the same value) risks
    a needless re-register / collision suffix. Skip when already in sync
    OR when the current value can't be read (tailscaled not up yet at
    boot / transient) — a genuine rename then retries on the next
    settings PUT or a later boot when tailscale is up. Tailscale
    lowercases node names, so the compare is case-insensitive.
    """
    current = _current_tailscale_hostname()
    if current is None:
        log.debug(
            "name-actuator: tailscale hostname unreadable; skipping set "
            "(retries on next rename / boot when tailscaled is up)"
        )
        return
    if current == name.strip().lower():
        return  # already in sync — strict no-op; never touch the SSH lane

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


_HOSTAPD_SSID_MAX_OCTETS = 32
"""IEEE 802.11 SSID field max is 32 octets. `sign_name` is already
DNS-safe ASCII (SystemSettings validator: whitespace normalized to `-`,
non-safe punctuation dropped), so char-count == octet-count and a
simple `[:32]` slice is the correct clamp."""


def _apply_hostapd_ssid(name: str) -> None:
    """Re-render the hostapd.conf with the new SSID + ship via the
    existing `hostapd-write-and-restart` netctl subcommand (2026-
    07-03 QA FIX 1 — reuse the already-sanctioned crossing rather
    than reimplement it unprivileged).

    2026-07-03 (QA HARDEN A): `sign_name` can be up to 63 chars
    (RFC 1123 hostname), but hostapd's `ssid=` line is capped at
    32 octets per 802.11. A longer value makes hostapd fail-to-
    restart and takes the recovery-AP down. Clamp the AP SSID to
    the hostapd cap; the hostname / Tailscale / mDNS consumers
    still see the full name.
    """
    from openmarquee.network_supervisor_actuator import _netctl_send

    if not _HOSTAPD_CONF.exists():
        log.info("name-actuator: %s missing; skipping setup-AP SSID update", _HOSTAPD_CONF)
        return
    try:
        original = _HOSTAPD_CONF.read_text()
    except OSError as exc:
        log.warning("name-actuator: read %s failed: %r", _HOSTAPD_CONF, exc)
        return
    ap_ssid = name[:_HOSTAPD_SSID_MAX_OCTETS]
    if ap_ssid != name:
        log.info(
            "name-actuator: sign_name %r exceeds hostapd 32-octet SSID cap; AP SSID clamped to %r",
            name,
            ap_ssid,
        )
    rewritten = _substitute_ssid_line(original, ap_ssid)
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
