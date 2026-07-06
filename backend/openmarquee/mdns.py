"""Derive the device's mDNS identity URL for the boot identity card.

The URL is built from the LIVE system hostname — avahi publishes the
system hostname as ``<hostname>.local``, and ``name_actuator`` keeps the
hostname in sync with ``sign_name``, so ``socket.gethostname()`` is the
source of truth that tracks renames (fireplacesign -> JasonsSign1 ->
``http://jasonssign1.local``). ``OPENMARQUEE_MDNS_HOSTNAME`` overrides
for tests / containers where the OS hostname is not the sign identity.

Kept deliberately free of project imports so both ``app.py`` and
``network_supervisor.py`` (which app imports) can pull it without a
circular import.
"""

from __future__ import annotations

import os
import socket

# Last-resort host when the OS hostname is unresolvable / empty. Yields
# ``http://openmarquee.local`` — the pre-feature default, kept as a
# safety net so the card never renders a broken URL.
_FALLBACK_HOSTNAME = "openmarquee"


def mdns_hostname() -> str:
    """The device's mDNS leaf hostname (the label avahi publishes as
    ``<hostname>.local``), reduced to the leaf label and lowercased."""
    host = os.environ.get("OPENMARQUEE_MDNS_HOSTNAME") or ""
    if not host:
        try:
            host = socket.gethostname()
        except OSError:
            host = ""
    # Strip any domain suffix (some resolvers return an FQDN) and
    # lowercase: mDNS is case-insensitive but the URL should read
    # cleanly (JasonsSign1 -> jasonssign1).
    leaf = host.strip().rstrip(".").split(".")[0].strip().lower()
    return leaf or _FALLBACK_HOSTNAME


def mdns_url() -> str:
    """``http://<hostname>.local`` — the address shown on the boot
    identity card and encoded into its QR."""
    return f"http://{mdns_hostname()}.local"
