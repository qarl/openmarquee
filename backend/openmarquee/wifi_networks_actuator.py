"""2026-07-03 (qarl handover Phase B1): reconcile the operator's
`SystemSettings.wifi_networks` list against NetworkManager connection
profiles.

Design constraints from QA's 2026-07-03 dispatch:
  * IMPORT/ADOPT existing NM profiles on first load so the 3 networks
    already programmed into Jason's device (`openmarquee-sign-wifi` =
    NEBULA, `openmarquee-mgmt-wifi` = qarl, `openmarquee-admin-wifi` =
    admin) show up in `wifi_networks` without qarl having to re-enter
    them. Losing the currently-working connections during import is
    NOT acceptable — the reconcile must produce identical `nmcli con
    show` output on a re-application of the imported list.
  * SHARP KNIVES — this deploys to a live handover device:
      - Every actuator FAILS SOFT: subprocess errors log a warning
        and return, never crash the backend.
      - VALIDATE before applying: ssid + password go through the
        `WifiNetworkEntry` field validators before hitting nmcli.
      - NEVER touch the setup-AP profile OR Tailscale's state:
        setup-AP is identified by `interface-name == "ap0"` or by
        matching the `wifi_ssid` setting; Tailscale doesn't surface
        as an nmcli wifi profile so the type filter already skips
        it, but we keep a defensive check.
      - NEVER delete a profile that isn't `openmarquee-*`-prefixed
        unless it was explicitly adopted by us this boot.

Ownership convention:
  * Adopt/manage: any wifi profile whose connection-name starts with
    `openmarquee-`. This covers Jason's `openmarquee-sign-wifi` +
    friends AND future auto-created profiles (naming below).
  * Skip on import + never touch: the setup-AP (name prefix
    `openmarquee-SETUP` OR interface-name `ap0` OR SSID equals
    settings.wifi_ssid).
  * Auto-created profile names: `openmarquee-<ssid>` (SSID is
    printable ASCII per the WifiNetworkEntry validator so it's
    safe as a connection-id fragment). If a profile with this name
    already exists we `nmcli con modify` it in place.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openmarquee.settings import WifiNetworkEntry

log = logging.getLogger(__name__)

# Managed connection-id prefix — every profile the reconcile owns
# starts with this. On import we adopt any wifi profile matching it
# (except the setup-AP). On reconcile we delete adopted profiles
# absent from the list — the strict prefix guards against ever
# deleting a hand-added `nmcli con` the operator wants preserved.
_MANAGED_PREFIX = "openmarquee-"

# Setup-AP profile name prefix — never touched by import or
# reconcile. hostapd owns the AP; nmcli should never have a profile
# for it on production, but a first-boot bug or a hand-added debug
# profile with this shape must be skipped defensively.
_SETUP_AP_PREFIX = "openmarquee-SETUP"

# Setup-AP interface name — same story, defensive.
_SETUP_AP_IFACE = "ap0"

# Subprocess timeouts. nmcli calls are near-instant on a healthy Pi;
# 10s covers a scan-triggered wait on a slow radio without letting a
# wedged nmcli stall the settings PUT indefinitely.
_NMCLI_TIMEOUT_S = 10.0


class _NmcliNotAvailable(RuntimeError):
    """Raised internally when `nmcli` isn't on PATH. Callers catch
    and log; the actuator turns into a no-op on dev hosts."""


def _nmcli_or_raise() -> str:
    """Locate the nmcli binary or raise `_NmcliNotAvailable`."""
    path = shutil.which("nmcli")
    if not path:
        raise _NmcliNotAvailable("nmcli binary not found on PATH")
    return path


def _run_nmcli(*args: str, timeout_s: float = _NMCLI_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Thin subprocess wrapper. Never raises on non-zero exit —
    callers inspect `.returncode`. `TimeoutExpired` propagates so
    the outer fail-soft catches it."""
    nmcli = _nmcli_or_raise()
    return subprocess.run(
        [nmcli, *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def _split_terse_row(line: str) -> list[str]:
    """Split nmcli --terse output on unescaped ':'. nmcli escapes
    embedded colons with '\\:' so a connection name or SSID like
    `Home:Router` still round-trips as one column."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == ":":
            buf.append(":")
            i += 2
            continue
        if ch == ":":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _is_setup_ap_row(name: str, iface: str | None, ssid: str | None, ap_ssid: str | None) -> bool:
    """Return True if this NM profile is the setup-AP + must be
    skipped by both import + reconcile. Multiple orthogonal
    signals so a rename OR interface-change still catches it."""
    if name.startswith(_SETUP_AP_PREFIX):
        return True
    if iface == _SETUP_AP_IFACE:
        return True
    return bool(ap_ssid and ssid == ap_ssid)


def _list_nm_wifi_connections() -> list[dict[str, str]]:
    """Return `[{name, ssid, iface}, ...]` for every nmcli wifi
    connection. Fails soft: on any subprocess error returns [] so
    the caller (import path) proceeds with an empty adopt list.

    Uses two calls because nmcli's `connection show` doesn't
    surface the SSID directly on some versions — we `connection
    show <name>` per row to get the wifi-specific fields. This
    means N+1 subprocess round-trips per import; on the Jason
    device with 4 profiles that's ~40 ms total, acceptable for a
    once-per-boot path.
    """
    try:
        list_result = _run_nmcli(
            "-t",
            "-f",
            "NAME,TYPE",
            "connection",
            "show",
        )
    except (_NmcliNotAvailable, subprocess.TimeoutExpired, OSError):
        return []
    if list_result.returncode != 0:
        return []

    rows: list[dict[str, str]] = []
    for line in list_result.stdout.splitlines():
        parts = _split_terse_row(line)
        if len(parts) < 2:
            continue
        name, conn_type = parts[0], parts[1]
        # Only wifi profiles participate; ethernet, bridge, loopback,
        # tailscale, wireguard, etc. are transparently skipped.
        if conn_type != "802-11-wireless":
            continue

        # Fetch the wifi-specific detail row for this profile.
        try:
            detail = _run_nmcli(
                "-t",
                "-s",  # -s reveals wifi-sec.psk; without it the PSK is `<hidden>`
                "-f",
                "802-11-wireless.ssid,connection.interface-name,802-11-wireless-security.psk",
                "connection",
                "show",
                name,
            )
        except (_NmcliNotAvailable, subprocess.TimeoutExpired, OSError):
            continue
        if detail.returncode != 0:
            continue

        detail_fields: dict[str, str] = {}
        for detail_line in detail.stdout.splitlines():
            if ":" in detail_line:
                key, value = detail_line.split(":", 1)
                detail_fields[key.strip()] = value.strip()

        rows.append(
            {
                "name": name,
                "ssid": detail_fields.get("802-11-wireless.ssid", ""),
                "iface": detail_fields.get("connection.interface-name", ""),
                "psk": detail_fields.get("802-11-wireless-security.psk", ""),
            }
        )
    return rows


def import_existing_wifi_profiles(
    *,
    ap_ssid: str | None = None,
) -> list[dict[str, object]]:
    """Read NetworkManager's currently-configured wifi connection
    profiles + return them as raw `WifiNetworkEntry`-shape dicts
    (ssid + password + autoconnect + priority). Filters:

      * Type != wifi → skipped.
      * setup-AP (`openmarquee-SETUP*` name, `ap0` iface, or SSID
        matching `ap_ssid`) → skipped.
      * Profiles with an empty SSID (hidden network with no
        broadcast) → skipped.

    Called from `SettingsStorage.load()` once per boot when
    `wifi_networks_seeded_from_nm` is False — first-boot on Jason's
    device adopts the 3 existing `openmarquee-*-wifi` profiles; a
    dev host without nmcli gets [] and the settings continue with
    an empty list.

    Returns dicts, not WifiNetworkEntry instances, so the caller
    can hand the result to `SystemSettings.model_validate` without
    the circular import + so a Pydantic validation error on a
    weird SSID falls through the model's error surface (not this
    helper's).

    Fails soft: nmcli missing / non-zero / malformed output all
    surface as an empty list.
    """
    imported: list[dict[str, object]] = []
    for row in _list_nm_wifi_connections():
        if _is_setup_ap_row(row["name"], row["iface"], row["ssid"], ap_ssid):
            log.info(
                "wifi-networks-import: skipping setup-AP profile name=%r iface=%r ssid=%r",
                row["name"],
                row["iface"],
                row["ssid"],
            )
            continue
        ssid = row["ssid"]
        if not ssid:
            log.info(
                "wifi-networks-import: skipping hidden/empty-SSID profile name=%r",
                row["name"],
            )
            continue
        psk = row["psk"] or None
        # Priority is not exposed as a field per profile in the
        # simple import; default to 0 and let the operator tune
        # via the UI later.
        entry = {
            "ssid": ssid,
            "password": psk,
            "autoconnect": True,
            "priority": 0,
        }
        imported.append(entry)
        log.info(
            "wifi-networks-import: adopted profile name=%r ssid=%r (psk %s)",
            row["name"],
            ssid,
            "present" if psk else "absent",
        )
    return imported


def apply_wifi_networks(
    networks: list[WifiNetworkEntry],
    *,
    ap_ssid: str | None = None,
) -> None:
    """Reconcile the currently-configured NM wifi profiles against
    the operator's `wifi_networks` list.

    Algorithm:
      1. Enumerate every wifi profile on the device (filtered as in
         import_existing_wifi_profiles).
      2. For each entry in `networks`:
           * find a profile whose SSID matches — if found,
             `nmcli con modify` (update PSK, autoconnect, priority).
           * else, `nmcli con add type wifi con-name
             openmarquee-<ssid> ssid <ssid> …`.
      3. For each existing profile whose connection-name starts
         with `openmarquee-` AND whose SSID isn't in `networks`
         AND that isn't the setup-AP: `nmcli con delete`. Only
         profiles matching the prefix are ever deleted so hand-
         added `nmcli con` profiles from the operator stay intact.

    Fails soft: every subprocess error is logged + suppressed so a
    partial reconcile leaves the device in a mixed but never-worse
    state.
    """
    try:
        _nmcli_or_raise()
    except _NmcliNotAvailable:
        log.info("wifi-networks-reconcile: nmcli not available; skipping")
        return

    existing = _list_nm_wifi_connections()
    existing_by_ssid = {row["ssid"]: row for row in existing if row["ssid"]}
    wanted_ssids = {n.ssid for n in networks}

    # 1 + 2. Upsert each wanted network.
    for network in networks:
        match = existing_by_ssid.get(network.ssid)
        con_name = match["name"] if match else f"{_MANAGED_PREFIX}{network.ssid}"
        if match is None:
            _apply_add(con_name, network)
        _apply_modify(con_name, network)

    # 3. Delete openmarquee-owned profiles absent from the list.
    for row in existing:
        name = row["name"]
        if not name.startswith(_MANAGED_PREFIX):
            continue  # Never delete something we don't own.
        if _is_setup_ap_row(name, row["iface"], row["ssid"], ap_ssid):
            continue  # Setup-AP is off-limits.
        if row["ssid"] in wanted_ssids:
            continue  # Kept — matched by an entry above.
        _apply_delete(name)


def _apply_add(con_name: str, network: WifiNetworkEntry) -> None:
    """`nmcli con add type wifi con-name … ssid … wifi-sec.key-mgmt
    wpa-psk wifi-sec.psk … connection.autoconnect … connection.
    autoconnect-priority …`. Fails soft."""
    args = [
        "connection",
        "add",
        "type",
        "wifi",
        "con-name",
        con_name,
        "ssid",
        network.ssid,
        "wifi-sec.key-mgmt",
        "wpa-psk",
        "connection.autoconnect",
        "yes" if network.autoconnect else "no",
        "connection.autoconnect-priority",
        str(network.priority),
    ]
    if network.password:
        args += ["wifi-sec.psk", network.password]
    try:
        result = _run_nmcli(*args)
    except (_NmcliNotAvailable, subprocess.TimeoutExpired, OSError) as exc:
        log.warning("wifi-networks-reconcile: add %r failed: %r", con_name, exc)
        return
    if result.returncode != 0:
        # Never log stderr verbatim — nmcli may echo the PSK back on
        # a config-error line. Only log the return code + name so
        # secrets stay out of the journal.
        log.warning(
            "wifi-networks-reconcile: add %r returned rc=%d",
            con_name,
            result.returncode,
        )


def _apply_modify(con_name: str, network: WifiNetworkEntry) -> None:
    """`nmcli con modify <name> …` — updates PSK, autoconnect,
    priority in place. Fails soft."""
    args = [
        "connection",
        "modify",
        con_name,
        "802-11-wireless.ssid",
        network.ssid,
        "connection.autoconnect",
        "yes" if network.autoconnect else "no",
        "connection.autoconnect-priority",
        str(network.priority),
    ]
    if network.password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", network.password]
    try:
        result = _run_nmcli(*args)
    except (_NmcliNotAvailable, subprocess.TimeoutExpired, OSError) as exc:
        log.warning("wifi-networks-reconcile: modify %r failed: %r", con_name, exc)
        return
    if result.returncode != 0:
        log.warning(
            "wifi-networks-reconcile: modify %r returned rc=%d",
            con_name,
            result.returncode,
        )


def _apply_delete(con_name: str) -> None:
    """`nmcli con delete <name>` — only ever called on profiles
    matching the openmarquee-managed prefix. Fails soft."""
    try:
        result = _run_nmcli("connection", "delete", con_name)
    except (_NmcliNotAvailable, subprocess.TimeoutExpired, OSError) as exc:
        log.warning("wifi-networks-reconcile: delete %r failed: %r", con_name, exc)
        return
    if result.returncode != 0:
        log.warning(
            "wifi-networks-reconcile: delete %r returned rc=%d",
            con_name,
            result.returncode,
        )


def apply_in_background(
    networks: list[WifiNetworkEntry],
    *,
    ap_ssid: str | None = None,
) -> threading.Thread:
    """Dispatch `apply_wifi_networks` on a daemon thread so the
    settings PUT handler can return immediately. Matches the shape
    of `wifi_station.apply_in_background`. Returns the started
    thread so tests can join() if they need to await.
    """

    def _runner() -> None:
        with contextlib.suppress(Exception):
            apply_wifi_networks(list(networks), ap_ssid=ap_ssid)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return thread
