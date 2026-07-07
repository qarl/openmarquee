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

# WIFI: URI special chars that must be backslash-escaped so a value
# containing one still yields a scannable join code (de-facto MECARD-style
# WiFi QR spec). The default token_urlsafe password never contains these,
# so a normal card's QR is byte-identical to firstboot's; escaping only
# matters for an operator passphrase with e.g. a `;` or `:`.
_WIFI_QR_SPECIAL = set('\\;,:"')


def _wifi_qr_escape(value: str) -> str:
    return "".join("\\" + ch if ch in _WIFI_QR_SPECIAL else ch for ch in value)


def setup_card_credentials() -> dict[str, str]:
    """Join credentials for a SETUP / DEGRADED card:
    `{"ssid": <hostname>, "pin": <wifi_password>, "qr_payload": <WiFi QR>}`,
    omitting any key whose source is unavailable. Fail-soft: never raises,
    so a dev host / missing settings just yields the renderer's own
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
    # WiFi-join QR the phone camera reads to hop straight onto the setup
    # AP (the captive portal then auto-opens the setup page). Same
    # `WIFI:T:WPA;S:…;P:…;;` format as welcome.html / firstboot so both
    # onboarding paths encode the same thing. Only when we have BOTH the
    # network name and its password.
    ssid = creds.get("ssid")
    pin = creds.get("pin")
    if ssid and pin:
        creds["qr_payload"] = (
            f"WIFI:T:WPA;S:{_wifi_qr_escape(ssid)};P:{_wifi_qr_escape(pin)};;"
        )
    return creds
