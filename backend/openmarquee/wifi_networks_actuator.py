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


def _list_nm_wifi_connections() -> tuple[bool, list[dict[str, str]]]:
    """Return `(probe_ok, [{name, ssid, iface, psk}, ...])` for every
    nmcli wifi connection.

    2026-07-03 (QA HARDEN B): the tuple lets the import path
    distinguish "nmcli responded cleanly, no wifi profiles exist"
    (probe_ok=True, rows=[]) from "nmcli errored transiently" (probe_ok
    =False, rows=[]). Without this, `_seed_wifi_networks_from_nm`
    can flip `wifi_networks_seeded_from_nm=True` on a transient
    error and lose the inactive fallback profiles (qarl/admin) to
    a later reconcile.

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
        return False, []
    if list_result.returncode != 0:
        return False, []

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

        # 2026-07-03 (QA FIX 3): parse detail rows with the escape-
        # aware _split_terse_row, NOT `str.split(":", 1)` — an SSID
        # or PSK containing an unescaped `:` gets its second
        # fragment dropped by str.split, corrupting the value on
        # reboot import. And don't `.strip()` PSK values: a
        # legitimate PSK can start or end with whitespace + WPA2
        # accepts printable ASCII 0x20-0x7e per the field
        # validator, so trimming would silently mutate a valid
        # value.
        detail_fields: dict[str, str] = {}
        for detail_line in detail.stdout.splitlines():
            if ":" not in detail_line:
                continue
            parts = _split_terse_row(detail_line)
            if len(parts) < 2:
                continue
            key = parts[0]
            # Rejoin everything past the first colon so a field
            # value with an internal `:` round-trips exactly.
            value = ":".join(parts[1:])
            # Key names are always ASCII field paths (no whitespace);
            # safe to strip. Value MUST NOT be stripped (see comment).
            detail_fields[key.strip()] = value

        rows.append(
            {
                "name": name,
                "ssid": detail_fields.get("802-11-wireless.ssid", ""),
                "iface": detail_fields.get("connection.interface-name", ""),
                "psk": detail_fields.get("802-11-wireless-security.psk", ""),
            }
        )
    return True, rows


def import_existing_wifi_profiles(
    *,
    ap_ssid: str | None = None,
) -> tuple[bool, list[dict[str, object]]]:
    """Read NetworkManager's currently-configured wifi connection
    profiles + return `(probe_ok, [WifiNetworkEntry-shape dicts])`.
    Filters:

      * Type != wifi → skipped.
      * setup-AP (`openmarquee-SETUP*` name, `ap0` iface, or SSID
        matching `ap_ssid`) → skipped.
      * Profiles with an empty SSID (hidden network with no
        broadcast) → skipped.

    2026-07-03 (QA HARDEN B): the return-tuple lets the caller
    tell "nmcli responded successfully with zero wifi profiles"
    (probe_ok=True, entries=[]) apart from "nmcli errored /
    missing" (probe_ok=False, entries=[]). Only in the former
    case is it safe to flip `wifi_networks_seeded_from_nm=True`;
    a probe failure must NOT flip the flag or a subsequent PUT-
    triggered reconcile would delete the inactive fallback
    profiles that live in NM but aren't in settings.

    Returns dicts, not WifiNetworkEntry instances, so the caller
    can hand the result to `SystemSettings.model_validate` without
    the circular import + so a Pydantic validation error on a
    weird SSID falls through the model's error surface (not this
    helper's).

    Fails soft: nmcli missing / non-zero / malformed output surface
    as `(False, [])`.
    """
    probe_ok, rows = _list_nm_wifi_connections()
    imported: list[dict[str, object]] = []
    for row in rows:
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
    return probe_ok, imported


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

    # 2026-07-03 (QA HARDEN B v2, F5): the enumerate probe MUST have
    # succeeded before we mutate — otherwise we upsert against a blind
    # view. With existing=[] the delete loop is a no-op (safe) but the
    # upsert loop would fire _apply_add for EVERY wanted network,
    # producing duplicate openmarquee-<ssid> profiles on the device.
    # A reconcile against an unknown world is a no-op; the operator's
    # next PUT (or the next reconcile-triggering settings edit) re-tries.
    probe_ok, existing = _list_nm_wifi_connections()
    if not probe_ok:
        log.warning(
            "wifi-networks-reconcile: nmcli enumerate probe failed; "
            "skipping reconcile (would upsert against a blind view)"
        )
        return
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
        # 2026-07-03 (QA FIX 2): drop-NEBULA guard. NEVER delete a
        # profile whose GENERAL.STATE is `activated` — that's the
        # live uplink and blowing it up drops the sign off the
        # network. If the operator wants to remove an active
        # profile, they should first pick a different one (which
        # this reconcile picks up + activates), THEN the previous
        # one becomes inactive on the next tick. Fail-safe: if the
        # state probe itself fails (nmcli non-zero / timeout /
        # missing), also skip the delete — we'd rather leave a
        # stale profile than accidentally kill the uplink.
        if _is_connection_activated(name):
            log.warning(
                "wifi-networks-reconcile: skipping delete of %r — connection is "
                "ACTIVATED (drop-NEBULA guard); the operator should switch to a "
                "different network before removing this one",
                name,
            )
            continue
        _apply_delete(name)


def _is_connection_activated(con_name: str) -> bool:
    """2026-07-03 (QA FIX 2 drop-NEBULA guard): return True iff the
    NM connection's `GENERAL.STATE` is `activated`. The state probe
    is a read-only unprivileged operation (no `-s` flag, no PSK
    read), so it works under NoNewPrivileges without needing to
    round-trip through the netctl daemon.

    Fail-safe posture: on ANY probe failure (nmcli not available,
    timeout, non-zero return, unparseable output) return True so
    the delete is skipped. Rationale: it's far safer to leave a
    stale openmarquee-owned profile on disk than to accidentally
    delete the ACTIVE uplink because we couldn't determine its
    state.
    """
    try:
        result = _run_nmcli(
            "-t",
            "-f",
            "GENERAL.STATE",
            "connection",
            "show",
            con_name,
        )
    except (_NmcliNotAvailable, subprocess.TimeoutExpired, OSError):
        return True  # Fail-safe: unknown state → treat as activated.
    if result.returncode != 0:
        return True
    # nmcli's `GENERAL.STATE` line reads: `GENERAL.STATE:activated`
    # (or `activating` / `deactivating` / empty when down). Detect
    # activated OR activating so a mid-connection swap is safe too.
    for line in result.stdout.splitlines():
        parts = _split_terse_row(line)
        if len(parts) >= 2 and parts[0] == "GENERAL.STATE":
            state = parts[1].strip().lower()
            return state in {"activated", "activating"}
    # No GENERAL.STATE line → connection is defined but not active.
    return False


def _network_payload(con_name: str, network: WifiNetworkEntry) -> bytes:
    """Serialize a `WifiNetworkEntry` into the 5-line stdin payload
    the netctl bash helper reads (see `nm-connection-add-wifi` +
    `nm-connection-modify-wifi` in system/openmarquee-netctl).
    Order: con_name / ssid / password / autoconnect / priority."""
    autoconnect = "yes" if network.autoconnect else "no"
    lines = [
        con_name,
        network.ssid,
        network.password or "",
        autoconnect,
        str(network.priority),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _apply_add(con_name: str, network: WifiNetworkEntry) -> None:
    """Route the nmcli `connection add` through the netctl socket
    daemon (2026-07-03 QA FIX 1). Under NoNewPrivileges the
    backend can't call `nmcli connection add` directly — it fails
    silently to a no-op. This crossing runs the real subprocess
    as root via the socket-activated daemon.

    Fails soft: any protocol error (socket missing on a dev host,
    ERR from the daemon, timeout) logs a warning + returns. Never
    logs stderr verbatim: an nmcli config-error line can echo the
    PSK back, and journald is not a secret store."""
    from openmarquee.network_supervisor_actuator import _netctl_send

    class _AddError(RuntimeError):
        """Typed exception so _netctl_send propagates a specific
        error class; converted to a log-warn here."""

    try:
        _netctl_send(
            "nm-connection-add-wifi",
            _network_payload(con_name, network),
            timeout_s=_NMCLI_TIMEOUT_S,
            error_cls=_AddError,
        )
    except _AddError as exc:
        _log_netctl_failure("add", con_name, exc)


def _apply_modify(con_name: str, network: WifiNetworkEntry) -> None:
    """Route `nmcli connection modify` through netctl. See
    `_apply_add` for the crossing rationale.

    2026-07-03 (QA LOW): the bash side skips the
    `wifi-sec.key-mgmt wpa-psk` write when password is empty,
    which preserves WPA3-SAE profiles. The 5-line payload always
    passes SSID / autoconnect / priority so a re-save with the
    sentinel-preserved PSK still lands the non-secret changes."""
    from openmarquee.network_supervisor_actuator import _netctl_send

    class _ModifyError(RuntimeError):
        pass

    try:
        _netctl_send(
            "nm-connection-modify-wifi",
            _network_payload(con_name, network),
            timeout_s=_NMCLI_TIMEOUT_S,
            error_cls=_ModifyError,
        )
    except _ModifyError as exc:
        _log_netctl_failure("modify", con_name, exc)


def _apply_delete(con_name: str) -> None:
    """Route `nmcli connection delete` through netctl. Only called
    on profiles that already passed the ownership prefix check +
    the activated-connection guard in `apply_wifi_networks`."""
    from openmarquee.network_supervisor_actuator import _netctl_send

    class _DeleteError(RuntimeError):
        pass

    try:
        _netctl_send(
            "nm-connection-delete",
            (con_name + "\n").encode("utf-8"),
            timeout_s=_NMCLI_TIMEOUT_S,
            error_cls=_DeleteError,
        )
    except _DeleteError as exc:
        _log_netctl_failure("delete", con_name, exc)


def _log_netctl_failure(op: str, con_name: str, exc: Exception) -> None:
    """Common log line for the three netctl-routed reconcile ops.
    Only the exception's TYPE + argv-visible fields land in
    journald — never the raw exception message, which for an
    add/modify failure could echo the PSK back from nmcli."""
    log.warning(
        "wifi-networks-reconcile: %s %r failed via netctl (%s); "
        "reconcile partial, next PUT retries",
        op,
        con_name,
        type(exc).__name__,
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
