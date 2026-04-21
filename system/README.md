# system/

Device-level OS configuration — everything the Pi needs to boot into AP
mode and serve the UI as its permanent interface.

These files get provisioned onto the SD card once, at image-build time
(Phase 9). For local dev on a Mac they're not used — the dev server
at `scripts/dev.sh` doesn't touch them.

## Files

| File | Destination on the Pi | Purpose |
| --- | --- | --- |
| `openmarquee-backend.service` | `/etc/systemd/system/` | systemd unit that runs the FastAPI backend on port 80 |
| `hostapd.conf` | `/etc/hostapd/hostapd.conf` | WiFi access point config (SSID, WPA2, channel) |
| `dnsmasq.conf` | `/etc/dnsmasq.d/openmarquee.conf` | DHCP on 10.0.0.x + DNS intercept so any URL redirects to the UI |

The backend service is wrapped with `ProtectSystem=strict`, a dedicated
service user, and `CAP_NET_BIND_SERVICE` so uvicorn can bind port 80
without running as root.

## First-time install (until Phase 9's image builder lands)

Until pi-gen bakes all of this into a flashable SD card image, these are
the manual steps to provision a fresh Pi Zero 2 W for OpenMarquee:

1. **Flash Raspberry Pi OS Lite (64-bit)**, enable SSH in the image
   configurator, give the Pi a recognizable hostname.

2. **Boot**, SSH in, update apt + install deps:

   ```
   sudo apt update
   sudo apt install -y hostapd dnsmasq python3-venv rsync
   ```

3. **Create the service user + directories:**

   ```
   sudo useradd --system --no-create-home openmarquee
   sudo mkdir -p /opt/openmarquee /var/openmarquee/content
   sudo chown -R openmarquee:openmarquee /var/openmarquee
   sudo python3 -m venv /opt/openmarquee/venv
   sudo chown -R openmarquee:openmarquee /opt/openmarquee
   ```

4. **Copy configs + unit:**

   ```
   # From your dev machine:
   scp system/openmarquee-backend.service pi@<ip>:/tmp/
   scp system/hostapd.conf pi@<ip>:/tmp/
   scp system/dnsmasq.conf pi@<ip>:/tmp/

   # On the Pi:
   sudo mv /tmp/openmarquee-backend.service /etc/systemd/system/
   sudo mv /tmp/hostapd.conf /etc/hostapd/hostapd.conf
   sudo mv /tmp/dnsmasq.conf /etc/dnsmasq.d/openmarquee.conf
   sudo systemctl daemon-reload
   sudo systemctl unmask hostapd  # usually masked by default on Pi OS
   ```

5. **First deploy from the dev machine:**

   ```
   bash scripts/deploy.sh pi@<ip>
   ```

   `deploy.sh` rsyncs `backend/` and built `ui/` assets into `/opt/openmarquee/`,
   `pip install -e`s the backend into the venv, and restarts the systemd unit.

6. **Enable services:**

   ```
   sudo systemctl enable --now hostapd dnsmasq openmarquee-backend
   ```

7. **Verify:**

   - The Pi should now broadcast `OpenMarquee-SETUP` (2.4 GHz).
   - Connect a phone → captive portal should pop with the setup UI.
   - If it doesn't, `sudo journalctl -u openmarquee-backend -f` on the Pi.

## Phase 7 open items

The configs here are *setup-mode* defaults. Phase 7 (WiFi AP + captive
portal) adds:

- **SSID rotation:** a oneshot unit rewrites `hostapd.conf`'s `ssid=` line
  to `OpenMarquee-XXXX` where `XXXX` is the last four hex chars of the
  board's MAC address, so every device has a unique-looking network.
- **Password rotation:** same idea, but for `wpa_passphrase=`. The welcome
  screen (`ui/welcome.html`) reads the final SSID + password and encodes
  them into the QR code via `src/welcome.js`.
- **Captive-portal HTTP responder:** the OS-specific probes
  (`captive.apple.com/hotspot-detect.html`, Android's
  `connectivitycheck.gstatic.com/generate_204`, Microsoft's
  `msftconnecttest.com`) need 302-to-`/` handlers so the phone's
  background detector pops the portal automatically rather than the user
  having to open a browser and type something.

## Phase 9 open items

- pi-gen recipe that bakes all of the above into an SD card image, so
  end-users just `rpi-imager` our `.img.zst` and it Just Works.
- First-boot self-provisioning: on initial power-up, a oneshot unit
  rotates the AP password, writes it into `/var/openmarquee/wifi.json`,
  templates it into `welcome.html`, and then disables itself.
