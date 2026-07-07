"""Setup-card join credentials — the SSID + password the SETUP system
card shows so someone physically at the sign knows how to join the
setup AP.

Sourced from the CURRENT device identity, NOT the firstboot-baked
welcome.html values (which go stale on an out-of-band rename): SSID =
the hostname (== the reconciled hostapd `ssid=`, the mDNS URL, the
Tailscale name — one source of truth), PIN = the live
`SystemSettings.wifi_password` (the AP WPA2 passphrase the welcome QR
also encodes). Kept free of project imports at module level (lazy
settings import) so both the network supervisor and the app lifespan can
pull it without an import cycle.

2026-07-07: added because the SETUP card was rendering the layout's
hardcoded `openMarquee-Setup` / `----` fallbacks (the supervisor never
threaded ssid/pin), so a fresh onboarding showed a wrong network + fake
PIN — broken card-driven onboarding.
"""

from __future__ import annotations

import socket


def setup_card_credentials() -> dict[str, str]:
    """`{"ssid": <hostname>, "pin": <wifi_password>}` for a SETUP card,
    omitting either key when unavailable. Fail-soft: never raises, so a
    dev host / missing settings just yields the renderer's own
    fallbacks instead of wedging the card path."""
    creds: dict[str, str] = {}
    try:
        host = socket.gethostname().strip().split(".")[0]
        if host:
            creds["ssid"] = host
    except OSError:
        pass
    try:
        from openmarquee.dependencies import get_settings_storage

        password = get_settings_storage().load().wifi_password
        if password:
            creds["pin"] = password
    except Exception:
        pass
    return creds
