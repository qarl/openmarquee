# system/

Device-level OS configuration — everything the Pi needs to boot into
concurrent AP + station mode and serve the UI as its permanent interface.

These files get provisioned onto the SD card once, at image-build time
(Phase 9). For local dev on a Mac they're not used — the dev server at
`scripts/dev.sh` doesn't touch them.

**Last refreshed 2026-05-14** to fold in the AP/NM coexistence
fixes (commit `68727de`, task #99), the wifi station-mode nmcli
applier (`6ecd1a2` + `0575572` polish), and the per-device
firstboot rotation (`openmarquee-firstboot.service`). The
operationally-current "what shipped" view also lives in
`docs/phase-7-as-built-2026-05-14.md`; this README is the
device-OS / manual-bring-up reference.

## Files

| File | Destination on the Pi | Purpose |
| --- | --- | --- |
| `openmarquee-backend.service` | `/etc/systemd/system/` | systemd unit that runs the FastAPI backend on port 80 |
| `openmarquee-ap0.service` | `/etc/systemd/system/` | oneshot that creates the virtual `ap0` WiFi interface before hostapd starts; orders `Before=NetworkManager.service` so the `iw dev` add wins the race against NM associating `wlan0` (task #99) |
| `openmarquee-ap0-setup.sh` | `/opt/openmarquee/system/` | invoked by the service above — `iw dev` add + IP + MAC |
| `openmarquee-firstboot.service` | `/etc/systemd/system/` | one-time oneshot that runs on the first boot of a fresh SD card; rotates per-device AP SSID + passphrase into `hostapd.conf`, writes `wifi.json`, templates `welcome.html`. Idempotent + self-disabling via `/var/openmarquee/.bootstrapped` |
| `openmarquee-firstboot.sh` | `/opt/openmarquee/system/` | the bring-up script for the firstboot service above |
| `hostapd.conf` | `/etc/hostapd/hostapd.conf` | AP config, binds to `ap0` (not `wlan0`). Ships with `ssid=openMarquee-SETUP` as cold-boot default; firstboot rotates to `MySignXXX` (operator-visible value) |
| `dnsmasq.conf` | `/etc/dnsmasq.d/openmarquee.conf` | DHCP on 10.0.0.x + DNS intercept on `ap0` only |
| `wpa_supplicant-openmarquee.conf` | `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` | legacy station-mode template (kept for fallback / pre-trixie boards). Pi OS Lite trixie uses NetworkManager + nmcli instead — see Station-mode applier below. |
| `openmarquee-sudoers` | `/etc/sudoers.d/openmarquee` | minimal NOPASSWD grants for the `openmarquee` user: two narrow `nmcli` subcommands needed by the wifi-station applier. Read-only nmcli queries don't need sudo (the user is in the `netdev` group) |
| `openmarquee-tailscale.service` | `/etc/systemd/system/` | oneshot that reads settings.json and runs `tailscale up` if enabled |
| `openmarquee-tailscale.sh` | `/opt/openmarquee/system/` | the bring-up script for the service above |

The backend service is wrapped with `ProtectSystem=strict`, a dedicated
service user, and `CAP_NET_BIND_SERVICE` so uvicorn can bind port 80
without running as root.

## On-disk state layout (`/var/openmarquee/`)

The backend writes all mutable state under `/var/openmarquee/`. Paths are
pinned via `Environment="OPENMARQUEE_*_PATH=..."` in
`openmarquee-backend.service` so dev / cwd / refactor can't relocate
them silently.

| Path | Carries |
| --- | --- |
| `content/` | All ContentItem dirs (`<UUID>/item.json` + `<UUID>/asset.png` or `<UUID>/asset.mp4`) |
| `playlist.json` | PlaylistCollection (v4 UUID-keyed) |
| `schedules.json` | Schedule rules + default_playlist_id |
| `settings.json` | SystemSettings (AP password, station password, Tailscale auth key — 0600 only) |
| `auth.json` | argon2id password hash + token_version for the operator-set captive-portal password (0600 only). Forgot-password recovery: physical SD access, `sudo rm /var/openmarquee/auth.json`, restart backend — first-boot welcome flow re-prompts. |
| `flock.json` | FlockStorage peer list + sync flags |
| `tombstones.json` | TombstoneLog for delete-replication across the flock |
| `seeded.json` | Marker stamping that first-boot seed already ran |
| `preview.png` | Latest dev-preview snapshot from the playback loop |
| `wifi.json` | Captive-portal AP password as currently broadcast (written by first-boot rotation) |

## Concurrent AP + station mode on a single radio

The Pi Zero 2 W's BCM43438 supports hosting a WiFi access point AND
joining another WiFi network simultaneously via two virtual interfaces
on the same physical radio:

- **`wlan0`** (STA): joins the operator's home WiFi. Managed by
  **NetworkManager** on Pi OS Lite trixie (the legacy
  `wpa_supplicant@wlan0` path is kept as a fallback config but the
  shipping station-mode applier uses `nmcli` — see below). This is
  what Tailscale + remote management ride over.
- **`ap0`** (AP): the captive-portal access point phones connect to
  during setup. Created by `openmarquee-ap0.service` via `iw dev wlan0
  interface add ap0 type __ap`. `hostapd` + `dnsmasq` bind here
  exclusively.

### AP + NetworkManager coexistence (task #99, `68727de`)

Pi OS Lite trixie's default network manager is NetworkManager.
The captive-portal AP and the NM-managed station can coexist on
the same physical radio, but it requires careful systemd
ordering — three fixes landed 2026-05-14:

1. **`openmarquee-ap0.service` declares
   `Before=hostapd.service NetworkManager.service
   NetworkManager-wait-online.service`.** Without this, NM can
   begin associating `wlan0` while we're still trying to add the
   `ap0` virtual interface. `brcmfmac` (the BCM43438 driver)
   sometimes accepts and sometimes refuses depending on the
   exact `wlan0` state — the race is real and reproducible on
   factory-fresh boards. Ordering ap0 setup strictly before NM
   eliminates it. After ap0 is up, NM resumes normal management
   of `wlan0`; both vifs coexist on the same channel (see
   constraint note below).
2. **`scripts/install.sh` unmasks `hostapd.service` and
   `dnsmasq.service`.** trixie ships both masked by default
   (assuming NM will own the radio); `systemctl enable` is a
   no-op against a masked unit, so without the unmask the AP
   silently never starts. The install script runs
   `systemctl unmask hostapd.service dnsmasq.service` before
   the enable.
3. **`scripts/install.sh` defensive `chmod +x` on
   `system/*.sh`.** Repo-side `e8545bd` flipped the git mode of
   the helper scripts to `100755`; the install script keeps the
   defensive chmod as belt-and-suspenders in case a future
   regression re-commits a script as `644`.

The wifi station applier (`backend/openmarquee/wifi_station.py`,
nmcli-based) works through `wlan0` concurrently with `ap0`
hosting the SoftAP — they share a connection-manager surface
(`nmcli` for station, raw `iw` + `hostapd` for AP) without
fighting because each touches a separate vif.

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
   # Both masked by default on Pi OS Lite trixie (NetworkManager is
   # the assumed radio manager). scripts/install.sh does this for
   # the bundle path; manual provisioning needs it explicitly.
   sudo systemctl unmask hostapd.service dnsmasq.service
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

   - The Pi should now broadcast its SSID on `ap0` at 2.4 GHz.
     A factory-fresh board shows `openMarquee-SETUP` for the brief
     window before `openmarquee-firstboot.service` runs; after
     first-boot rotation it broadcasts `MySignXXX` (the per-device
     ID stamped into `hostapd.conf`).
   - Connect a phone → captive portal should pop with the setup UI.
   - `ip addr show ap0` shows `10.0.0.1/24`; `ip addr show wlan0` shows
     whatever the home WiFi handed out (or "no carrier" if wlan0 isn't
     yet associated — that's fine, the AP still works standalone).
   - If something doesn't come up, follow the logs:
     `sudo journalctl -u openmarquee-ap0 -u hostapd -u dnsmasq -u openmarquee-backend -f`.

## Phase 7 status (refreshed 2026-05-14)

The configs here started as *setup-mode* defaults. Phase 7
(WiFi AP + captive portal) has now largely shipped:

- **SSID rotation:** SHIPPED via `openmarquee-firstboot.service`.
  Rewrites `hostapd.conf`'s `ssid=` line to `MySignXXX` derived
  from `device_id` (the same value used for the Tailscale
  hostname + sign_name default), so every device has a
  unique-looking network. Note: the rotation target is the
  per-device `device_id`, NOT `openMarquee-XXXX` from the MAC
  address as the prior README sketch claimed; the device_id
  source-of-truth gives a single identifier across SSID +
  hostname + sign_name (see `feedback_sign_name_sync_principle`).
- **Password rotation:** SHIPPED via the same firstboot oneshot.
  Per-device passphrase written to `/var/openmarquee/wifi.json`
  (0600) + templated into `hostapd.conf`'s `wpa_passphrase=`.
  The welcome screen (`ui/welcome.html`) reads the rotated SSID
  + password and encodes them into the QR code via
  `src/welcome.js`.
- **Settings → station-mode applier:** SHIPPED via
  `backend/openmarquee/wifi_station.py` (commits `6ecd1a2` for
  the nmcli rewrite + `0575572` for the rescan / radio-state
  polish). When the operator enters home-WiFi credentials in
  the Settings UI, the applier runs `nmcli device wifi rescan`
  + 2s settle, then `nmcli device wifi connect` against the
  matching SSID. Failures surface back to the UI via the API's
  state machine (idle → rescanning → connecting →
  connected | failed). Replaces the originally-planned
  `wpa_supplicant@wlan0` template-out path — Pi OS Lite trixie's
  default network manager is NetworkManager, and templating
  `wpa_supplicant-wlan0.conf` doesn't actually drive station
  association on that stack.
- **AP/NM coexistence:** SHIPPED via `68727de` (task #99) —
  see "AP + NetworkManager coexistence" section above. The
  factory-fresh-AP race against NM is eliminated by ordering
  `openmarquee-ap0.service` `Before=NetworkManager.service`.

Still open:

- **Captive-portal HTTP responder:** the OS-specific probes
  (`captive.apple.com/hotspot-detect.html`, Android's
  `connectivitycheck.gstatic.com/generate_204`, Microsoft's
  `msftconnecttest.com`) need 302-to-`/` handlers so the phone's
  background detector pops the portal automatically rather than
  the user having to open a browser and type something.
- **Tailscale reachability test:** once `tailscale up` returns,
  probe `tailscale status` + surface the node's magic-DNS
  hostname in the UI so the operator can verify they can reach
  the sign remotely.

## Phase 9 open items

- pi-gen recipe that bakes all of the above into an SD card image, so
  end-users just `rpi-imager` our `.img.zst` and it Just Works.
- First-boot self-provisioning: on initial power-up, a oneshot unit
  rotates the AP password, writes it into `/var/openmarquee/wifi.json`,
  templates it into `welcome.html`, and then disables itself.
