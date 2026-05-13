"""Device identity reader.

The first-boot oneshot (`system/openmarquee-firstboot.sh`) generates
a per-device MySignXXX identifier and persists it to
`/var/openmarquee/identity.json` (0644). That ID is the single
source of truth for:
  - AP SSID (in /etc/hostapd/hostapd.conf)
  - /etc/hostname (replaces cloud-init's openmarquee-<4hex>)
  - Tailscale magic-DNS name (when sign_name defaults from device_id)
  - The operator-facing "what's your sign called" string in
    welcome.html

This module exposes a single function `read_device_id()` that reads
identity.json and returns the device_id, or None if the file is
missing / unreadable / malformed. Off-device dev hosts have no
identity.json -- callers should treat None as "running off-device,
no factory identifier yet" and fall back to the OS hostname.

Path is env-overridable via OPENMARQUEE_IDENTITY_PATH so tests can
point at a fixture without touching /var.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("openmarquee.identity")

DEFAULT_IDENTITY_PATH = "/var/openmarquee/identity.json"

# MySign + 3 [A-Z0-9]. Format is set at firstboot generation time;
# pinning here guards against accidental relaxation of the contract.
DEVICE_ID_RE = re.compile(r"^MySign[A-Z0-9]{3}$")


def _identity_path() -> Path:
    return Path(os.environ.get("OPENMARQUEE_IDENTITY_PATH", DEFAULT_IDENTITY_PATH))


def read_device_id() -> str | None:
    """Return the MySignXXX device_id, or None on any error.

    Errors include: file missing (off-device dev), file unreadable,
    JSON parse failure, missing field, format violation. All log at
    debug; the caller's fallback (OS hostname) is the right behavior
    for off-device runs.
    """
    path = _identity_path()
    try:
        blob = json.loads(path.read_text())
    except FileNotFoundError:
        log.debug("identity.json not present at %s (off-device dev?)", path)
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("identity.json unreadable at %s: %s", path, exc)
        return None
    device_id = blob.get("device_id")
    if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id):
        log.warning(
            "identity.json device_id %r does not match MySignXXX format",
            device_id,
        )
        return None
    return device_id
