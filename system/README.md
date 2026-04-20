# system

Device-level OS configuration — everything the Pi needs to boot into AP mode and serve the UI as its permanent interface.

- `hostapd` — WiFi access point config.
- `dnsmasq` — DHCP, plus DNS intercept so any hostname the phone requests redirects to the UI.
- `systemd/` — service units for the backend, the AP, and the captive-portal redirect.
- Captive-portal glue — HTTP 302 responder for the OS-specific probes (Apple's `captive.apple.com`, Android's `connectivitycheck.gstatic.com`, etc.) so the phone pops the portal automatically.

These configs are applied to a minimal Raspberry Pi OS image. SD-card image build scripts will live in a sibling directory once we get there.
