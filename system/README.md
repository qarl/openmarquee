# system/

Device-level OS configuration — everything the Pi needs to boot into
concurrent AP + station mode and serve the UI as its permanent interface.

These files get provisioned onto the SD card once, at image-build time
(Phase 9). For local dev on a Mac they're not used — the dev server at
`scripts/dev.sh` doesn't touch them.

## Files

| File | Destination on the Pi | Purpose |
| --- | --- | --- |
| `openmarquee-backend.service` | `/etc/systemd/system/` | systemd unit that runs the FastAPI backend on port 80 |
| `openmarquee-ap0.service` | `/etc/systemd/system/` | oneshot that creates the virtual `ap0` WiFi interface before hostapd starts |
| `openmarquee-ap0-setup.sh` | `/opt/openmarquee/system/` | invoked by the service above — `iw dev` add + IP + MAC |
| `hostapd.conf` | `/etc/hostapd/hostapd.conf` | AP config, binds to `ap0` (not `wlan0`) |
| `dnsmasq.conf` | `/etc/dnsmasq.d/openmarquee.conf` | DHCP on 10.0.0.x + DNS intercept on `ap0` only |
| `wpa_supplicant-openmarquee.conf` | `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` | template for joining a home WiFi on `wlan0` (operator-supplied creds) |
| `openmarquee-tailscale.service` | `/etc/systemd/system/` | oneshot that reads settings.json and runs `tailscale up` if enabled |
| `openmarquee-tailscale.sh` | `/opt/openmarquee/system/` | the bring-up script for the service above |

The backend service is wrapped with `ProtectSystem=strict`, a dedicated
service user, and `CAP_NET_BIND_SERVICE` so uvicorn can bind port 80
without running as root.

## Concurrent AP + station mode on a single radio

The Pi Zero 2 W's BCM43438 supports hosting a WiFi access point AND
joining another WiFi network simultaneously via two virtual interfaces
on the same physical radio:

- **`wlan0`** (STA): joins the operator's home WiFi. Managed by
  `wpa_supplicant` (or NetworkManager on Bookworm). This is what
  Tailscale + remote management ride over.
- **`ap0`** (AP): the captive-portal access point phones connect to
  during setup. Created by `openmarquee-ap0.service` via `iw dev wlan0
  interface add ap0 type __ap`. `hostapd` + `dnsmasq` bind here
  exclusively.

**The one real constraint**: both virtual interfaces share the same
physical chip, so they **must run on the same channel**. When `wlan0`
associates with a home WiFi on channel 11, the kernel silently forces
`ap0` onto channel 11 too. The `channel=6` line in `hostapd.conf` is
therefore a preference, not a guarantee — it's only honored when `wlan0`
has no current station association.

Practical impact: the captive-portal SSID broadcasts on whatever channel
the operator's home WiFi uses, and the 2.4 GHz radio's bandwidth is
shared between the two roles. Low-bandwidth UI traffic handles this
comfortably; a Pi 4 (dual-radio, 2.4 + 5 GHz) would remove the
compromise if we ever need it.

## First-time install (until Phase 9's image builder lands)

Until pi-gen bakes all of this into a flashable SD card image, these are
the manual steps to provision a fresh Pi Zero 2 W for openMarquee:

1. **Flash Raspberry Pi OS Lite (64-bit)**, enable SSH in the image
   configurator, give the Pi a recognizable hostname.

2. **Boot**, SSH in, update apt + install deps:

   ```
   sudo apt update
   sudo apt install -y hostapd dnsmasq python3-venv rsync iw
   # Tailscale repo + client (optional; only needed if operator enables it)
   curl -fsSL https://tailscale.com/install.sh | sh
   ```

3. **Create the service user + directories:**

   ```
   sudo useradd --system --no-create-home openmarquee
   sudo mkdir -p /opt/openmarquee /opt/openmarquee/system /var/openmarquee/content
   sudo chown -R openmarquee:openmarquee /var/openmarquee
   sudo python3 -m venv /opt/openmarquee/venv
   sudo chown -R openmarquee:openmarquee /opt/openmarquee
   ```

4. **Copy configs + units + scripts:**

   ```
   # From your dev machine:
   scp system/openmarquee-backend.service pi@<ip>:/tmp/
   scp system/openmarquee-ap0.service pi@<ip>:/tmp/
   scp system/openmarquee-ap0-setup.sh pi@<ip>:/tmp/
   scp system/openmarquee-tailscale.service pi@<ip>:/tmp/
   scp system/openmarquee-tailscale.sh pi@<ip>:/tmp/
   scp system/hostapd.conf pi@<ip>:/tmp/
   scp system/dnsmasq.conf pi@<ip>:/tmp/
   scp system/wpa_supplicant-openmarquee.conf pi@<ip>:/tmp/

   # On the Pi:
   sudo mv /tmp/*.service /etc/systemd/system/
   sudo mv /tmp/hostapd.conf /etc/hostapd/hostapd.conf
   sudo mv /tmp/dnsmasq.conf /etc/dnsmasq.d/openmarquee.conf
   sudo mv /tmp/wpa_supplicant-openmarquee.conf \
       /etc/wpa_supplicant/wpa_supplicant-wlan0.conf
   sudo mkdir -p /opt/openmarquee/system
   sudo mv /tmp/openmarquee-ap0-setup.sh /tmp/openmarquee-tailscale.sh \
       /opt/openmarquee/system/
   sudo chmod +x /opt/openmarquee/system/*.sh
   sudo systemctl daemon-reload
   sudo systemctl unmask hostapd  # usually masked by default on Pi OS
   ```

5. **First deploy from the dev machine:**

   ```
   bash scripts/deploy.sh pi@<ip>
   ```

   `deploy.sh` rsyncs `backend/` and built `ui/` assets into
   `/opt/openmarquee/`, `pip install -e`s the backend into the venv,
   and restarts the systemd unit.

6. **Enable services:**

   ```
   sudo systemctl enable --now \
       openmarquee-ap0 \
       hostapd \
       dnsmasq \
       openmarquee-backend
   # Tailscale is opt-in per-device — enable only if settings have it on:
   sudo systemctl enable openmarquee-tailscale
   ```

7. **Verify:**

   - The Pi should now broadcast `openMarquee-SETUP` (2.4 GHz) on `ap0`.
   - Connect a phone → captive portal should pop with the setup UI.
   - `ip addr show ap0` shows `10.0.0.1/24`; `ip addr show wlan0` shows
     whatever the home WiFi handed out (or "no carrier" if wlan0 isn't
     yet associated — that's fine, the AP still works standalone).
   - If something doesn't come up, follow the logs:
     `sudo journalctl -u openmarquee-ap0 -u hostapd -u dnsmasq -u openmarquee-backend -f`.

## Phase 7 open items

The configs here are *setup-mode* defaults. Phase 7 (WiFi AP + captive
portal) finishes:

- **SSID rotation:** a oneshot unit rewrites `hostapd.conf`'s `ssid=`
  line to `openMarquee-XXXX` where `XXXX` is the last four hex chars of
  the board's MAC address, so every device has a unique-looking
  network.
- **Password rotation:** same idea, but for `wpa_passphrase=`. The
  welcome screen (`ui/welcome.html`) reads the final SSID + password
  and encodes them into the QR code via `src/welcome.js`.
- **Settings → wpa_supplicant template-out:** when the operator enters
  home-WiFi credentials in the Settings UI, a oneshot templates them
  into `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` and `systemctl
  restart wpa_supplicant@wlan0`. Until that lands, operators populate
  the file manually.
- **Captive-portal HTTP responder:** the OS-specific probes
  (`captive.apple.com/hotspot-detect.html`, Android's
  `connectivitycheck.gstatic.com/generate_204`, Microsoft's
  `msftconnecttest.com`) need 302-to-`/` handlers so the phone's
  background detector pops the portal automatically rather than the
  user having to open a browser and type something.
- **Tailscale reachability test:** once `tailscale up` returns, probe
  `tailscale status` + surface the node's magic-DNS hostname in the UI
  so the operator can verify they can reach the sign remotely.

## Phase 9 open items

- pi-gen recipe that bakes all of the above into an SD card image, so
  end-users just `rpi-imager` our `.img.zst` and it Just Works.
- First-boot self-provisioning: on initial power-up, a oneshot unit
  rotates the AP password, writes it into `/var/openmarquee/wifi.json`,
  templates it into `welcome.html`, and then disables itself.
