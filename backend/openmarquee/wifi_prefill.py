"""Read system wifi credentials for the first-run setup flow.

When a fresh openMarquee Pi boots for the first time, the first-run
form should auto-populate wifi SSID/password if the user pre-flashed
those creds via Raspberry Pi Imager (the standard out-of-box workflow
on Pi OS). This module parses /etc/wpa_supplicant/wpa_supplicant.conf,
confirms the active connection via `iwgetid -r`, and returns the
matching (ssid, psk) so api_settings can pre-fill SystemSettings on
the very first GET /api/settings.

Permission note: /etc/wpa_supplicant/wpa_supplicant.conf is typically
600 root:root or 644 root:root on Pi OS. The openmarquee service user
runs without root or sudo (see system/openmarquee-backend.service:
User=openmarquee), so this module returns None on PermissionError
rather than crashing. Operators who want this feature on a stock
install have two options:

  (a) chmod 644 /etc/wpa_supplicant/wpa_supplicant.conf on the image
      (or in a one-liner during first boot)
  (b) drop a readable copy at /var/openmarquee/wpa_supplicant.conf
      (the loader checks both paths)

When neither path is readable, this module logs once at INFO so an
operator tailing journalctl sees a hint about why the first-run
form's wifi fields are empty.

Best-effort everywhere: every failure mode (file missing, bad perms,
malformed conf, iwgetid not found, no active connection) returns
None silently so the GET /api/settings handler never breaks because
of a system-config probe. The first-run UI just shows empty fields
when prefill returns None — same as if there were no creds to find.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


def _default_iwgetid() -> str | None:
    """Production iwgetid runner. Returns the active SSID or None."""
    try:
        out = subprocess.run(
            ["iwgetid", "-r"], capture_output=True, text=True,
            timeout=2.0, check=False,
        )
    except FileNotFoundError:
        # iwgetid not installed (dev macOS, minimal container, etc.).
        return None
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("wifi_prefill: iwgetid failed: %s", e)
        return None
    ssid = out.stdout.strip()
    return ssid or None

# Locations checked in order. The /var copy is the recommended path
# for stock Pi OS installs without chmod'ing /etc.
#
# 19.1 / sweep #10 #1 (CRITICAL): Bookworm's `wpa_supplicant@wlan0.
# service` shape puts the conf at the per-interface path
# `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf`, not the bare
# `wpa_supplicant.conf`. system/README.md install instructions
# write the file at that per-interface path. Without this entry,
# wifi_prefill never finds the conf on a real Pi -- the captive-
# portal first-run flow keeps reading "no SSID configured" while
# the device IS in fact joined to a home network.
DEFAULT_WPA_CONF_PATHS: tuple[Path, ...] = (
    Path("/var/openmarquee/wpa_supplicant.conf"),
    Path("/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"),
    Path("/etc/wpa_supplicant/wpa_supplicant.conf"),
)

# `network={ ... }` blocks with newlines inside. wpa_supplicant.conf
# doesn't allow nested braces inside a network block, so a non-greedy
# match up to the next `}` is sufficient.
_NETWORK_BLOCK_RE = re.compile(r"network\s*=\s*\{([^}]*)\}", re.DOTALL)
# key=value lines inside a network block. Honors keys without leading
# whitespace and with arbitrary trailing whitespace.
_KEY_VALUE_RE = re.compile(r"^\s*(\w+)\s*=\s*(.+?)\s*$", re.MULTILINE)
# Backslash-escape unescaper for quoted-string values. Handles \" → "
# and \\ → \. Other wpa_supplicant escapes (\n \r \t \e \xHH) map
# to non-printable characters that the downstream PSK validator
# rejects anyway, so we don't bother decoding them.
_ESCAPE_RE = re.compile(r'\\([\\"])')


def read_system_wifi(
    *,
    paths: tuple[Path, ...] = DEFAULT_WPA_CONF_PATHS,
    get_active_ssid: Callable[[], str | None] = _default_iwgetid,
) -> tuple[str, str] | None:
    """Return (ssid, psk) for the SSID currently connected on wlan0,
    sourced from the first wpa_supplicant.conf we can read.

    Returns None on any failure mode: no active connection, missing
    file, permission denied, malformed conf, or no matching network.
    Never raises.

    `paths` and `get_active_ssid` are exposed for tests so they can
    bypass real subprocess execution. Production callers leave both
    at their defaults. The closure shape (instead of a tuple cmd)
    means tests don't pay any fork cost — fork-state pollution from
    other test files (Pillow / freetype / grpc / threading) was
    SIGSEGV'ing real `printf` invocations on macOS when test_api_
    flock ran first (caught 2026-05-02).
    """
    active_ssid = get_active_ssid()
    if not active_ssid:
        log.debug("wifi_prefill: no active connection (iwgetid empty)")
        return None
    permission_denied: list[Path] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except PermissionError:
            permission_denied.append(path)
            continue
        except OSError as e:
            log.debug("wifi_prefill: OS error reading %s: %s", path, e)
            continue
        match = _find_matching_network(text, active_ssid)
        if match is not None:
            log.info(
                "wifi_prefill: found creds for SSID %r in %s", active_ssid, path,
            )
            return match
    if permission_denied:
        # Surface this loudly the first time it happens — operators
        # tailing journalctl get an actionable hint instead of an
        # empty first-run form with no explanation. Logged once per
        # GET that hits this path; the prefill stops firing once the
        # ssid field is populated, so journal noise is bounded.
        log.info(
            "wifi_prefill: permission denied reading %s — "
            "first-run wifi fields will be empty. To enable: chmod 644 the "
            "file OR drop a readable copy at /var/openmarquee/wpa_supplicant.conf",
            ", ".join(str(p) for p in permission_denied),
        )
    else:
        log.debug(
            "wifi_prefill: no matching network={} block for active SSID %r",
            active_ssid,
        )
    return None


def _find_matching_network(text: str, active_ssid: str) -> tuple[str, str] | None:
    """Walk the network={} blocks in `text` and return (ssid, psk)
    for the FIRST block whose ssid matches active_ssid, skipping
    `disabled=1` blocks. Returns None if nothing matches."""
    for block_match in _NETWORK_BLOCK_RE.finditer(text):
        creds = _extract_creds(block_match.group(1))
        if creds is not None and creds[0] == active_ssid:
            return creds
    return None


def _extract_creds(block: str) -> tuple[str, str] | None:
    """Pull (ssid, psk) out of one network={} block body. Strips
    wrapping double-quotes from values. Returns None if disabled,
    missing ssid, or missing psk (open networks have no psk and we
    don't pre-fill those — the operator can leave the password field
    empty)."""
    fields: dict[str, str] = {}
    for kv in _KEY_VALUE_RE.finditer(block):
        key = kv.group(1)
        val = kv.group(2).strip()
        # Strip wrapping double-quotes (`ssid="MyNetwork"` → MyNetwork).
        # PSKs in wpa_supplicant can be 64-hex-char unquoted (a pre-
        # computed PSK from the SSID + passphrase) OR a quoted plain
        # passphrase. SystemSettings.wifi_station_password validates
        # 8-63 printable ASCII so the unquoted hex form would fail —
        # we only pre-fill the quoted-plaintext form.
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
            # wpa_supplicant.conf(5) permits backslash-escaped chars
            # inside double-quoted strings. The only escapes that can
            # appear in a valid 8-63 printable-ASCII PSK are \" and
            # \\ — the others (\n \r \t \e \xHH) map to
            # non-printables that the SystemSettings validator would
            # reject anyway. Unescape so the prefilled password
            # matches what wpa_supplicant actually negotiates.
            val = _ESCAPE_RE.sub(r"\1", val)
            quoted = True
        else:
            quoted = False
        fields[key] = val
        # Stash quoting state separately so we can reject unquoted PSKs.
        if key == "psk":
            fields["__psk_quoted"] = "1" if quoted else "0"
    if fields.get("disabled") == "1":
        return None
    ssid = fields.get("ssid")
    psk = fields.get("psk")
    if not ssid or not psk:
        return None
    if fields.get("__psk_quoted") != "1":
        # Hex-encoded PSK — can't pre-fill into a printable-ASCII
        # password field. Rare but worth handling cleanly.
        log.debug("wifi_prefill: PSK for %r is hex-encoded; skipping", ssid)
        return None
    return (ssid, psk)
