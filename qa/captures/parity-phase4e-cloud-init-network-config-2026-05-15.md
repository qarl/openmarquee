# Phase 4e diagnostic: why tonight's pre-configured WiFi failed

**Date:** 2026-05-15
**Dispatch:** QA edited `/Volumes/bootfs/network-config` to add a `wifis:`
block for SSID `qarl`, bumped meta-data instance-id, rebooted. Pi shows
no DHCP lease, no AP, no ping. Hypothesis: openmarquee pi-gen image
disables cloud-init's network module, so the wifi pre-config never gets
applied.

## Hypothesis VERDICT: PARTIAL / REFUTED — bigger blocker found

Refuted in the narrow sense: the openmarquee pi-gen recipe does NOT
disable cloud-init's network module. There is no
`cloud.cfg.d/99-disable-network.cfg`, no
`/etc/cloud/cloud.cfg.d/99_disable_network.cfg`, no override in
`images/openmarquee/stage-openmarquee/`. Cloud-init's network module
on this image IS active and should process `network-config` including
`wifis:` blocks. Source search across `images/openmarquee/` confirms
no cloud-init module-disable directives.

Partial in the broader sense: **a separate blocker masks the issue.**
The user-data written by `scripts/stage_sd_card.sh` carries
`package_update: true` AND a `packages:` apt list:

```yaml
package_update: true
packages:
  - zstd
  - python3-venv
  - hostapd
  - dnsmasq
  - iptables
  - v4l-utils
```

stage_sd_card.sh:166-172. Cloud-init runs `package_update + packages`
BEFORE `runcmd`. Without working network, this step fails. install.sh
in runcmd then never fires. openmarquee-firstboot.service never enables.
AP never comes up. Even if `wifis:` block in network-config IS being
processed correctly by cloud-init, NM joining the operator network is
not guaranteed before the apt-update kicks off — there's a race.

**Worse, every one of those 6 apt packages is ALREADY pre-baked** into
the image by `pi-gen` via
`images/openmarquee/stage-openmarquee/00-install-packages/00-packages`:

```
hostapd, dnsmasq, iptables, python3, python3-pip, python3-venv, ffmpeg,
fonts-dejavu, qrencode, git, rsync, ca-certificates, cloud-init,
wireless-regdb, iw, wireless-tools, wpasupplicant
```

zstd is missing from the pi-gen list; everything else in the user-data
`packages:` list is already there. The user-data `packages:` step is
load-bearing for zstd ONLY (needed to decompress the bundle in
runcmd) — but trying to install all 6 packages on first boot blocks
the entire chain when network is unavailable, AND blocks it for at
least zstd's-dependency-closure-of-1 even if just zstd is missing.

## Concrete evidence

- `scripts/stage_sd_card.sh:166-172` — package_update + packages list.
- `scripts/stage_sd_card.sh:118-130` — network-config writer; explicitly
  omits `wifis:` section with comment "Intentionally no `wifis:`
  section. AP mode is configured by openmarquee-firstboot.service".
- `images/openmarquee/cloud-init/user-data:24-25` — the IMAGE-baked
  user-data sets `package_update: false`, `package_upgrade: false`.
  But this is the cloud-init-IN-IMAGE user-data; the SD-card-staged
  user-data (which OVERRIDES the image's via NoCloud's `/boot/firmware/`
  precedence) re-enables apt.
- `images/openmarquee/stage-openmarquee/00-install-packages/00-packages`
  — list of 17 packages pre-baked at pi-gen time; covers 5/6 of the
  user-data's `packages:` list redundantly. zstd is missing.
- `system/openmarquee-ap0.service:13` — `Before=NetworkManager.service
  NetworkManager-wait-online.service`. ap0 is created before NM starts.
  NM then manages wlan0 station-side; ap0 is `unmanaged`.
- `images/openmarquee/README.md:125-126` (potentially stale) — claims
  "NetworkManager is intentionally absent — we manage wpa_supplicant
  directly." Conflicts with `backend/openmarquee/wifi_station.py:4-11`
  which says "trixie defaults to NetworkManager... All operations go
  through nmcli". The 00-packages list does NOT add `network-manager`,
  but Pi OS Lite trixie ships NM by default in the base image — so NM
  IS present (the README is misleading at minimum, stale at worst).

## Why tonight's network-config edit DIDN'T work

Most likely answer (without a journalctl trace from the Pi): the
package_update step blocked at the apt-fetch stage, runcmd never
fired, AP never came up. Whether the wifis: pre-config worked or not,
the apt blocker would have killed the chain.

If apt DID succeed (i.e., wifis: worked + NM joined `qarl`), Pi
should be visible on the operator's LAN with a DHCP lease and an
mDNS hostname. The fact that there's NO DHCP lease at all suggests
either:
- NM never joined `qarl` (wifis: pre-config didn't take), so apt failed.
- Cloud-init's NoCloud datasource caches network-config + instance-id
  bump may not invalidate the cached network setup.
- There's a single-radio race: openmarquee-ap0.service creates ap0
  via `iw dev wlan0 interface add ap0`. If NM then tries to associate
  wlan0 with `qarl` while hostapd is binding to ap0, the radio may
  refuse both.

## Recommended fix — Phase 4e proper

**Three-step plan:**

1. **Immediate (tonight unblock): drop the apt-on-first-boot step from
   stage_sd_card.sh's user-data.** Keep only zstd if it's truly not in
   pi-gen — but better: add zstd to pi-gen's 00-packages so user-data
   needs no `packages:` directive at all. Result: cloud-init runcmd
   needs zero network. install.sh runs offline (Phase 4a wheels). AP
   comes up. User configures WiFi via welcome UI.

   The 1-line fix is to change `package_update: true` → `package_update:
   false` and remove the `packages:` list in stage_sd_card.sh. Add zstd
   to 00-packages and rebuild the image, OR fall back to ensure zstd is
   in the image (Pi OS Lite trixie may already ship it).

2. **Tonight recovery for THIS card (no new flash needed):** mount
   /Volumes/bootfs, edit user-data to remove `package_update: true`
   and the `packages:` list (or hand-edit to `package_update: false`
   + delete packages:), re-bump meta-data instance-id, eject + boot.
   Cloud-init runs runcmd with no network requirement. AP comes up.

3. **Pre-config wifi for shipped-to-customer flow (Phase 4e proper,
   non-blocking):** drop an NM keyfile to /boot/firmware/ at flash
   time:
   ```
   /Volumes/bootfs/openmarquee-wifi.nmconnection
   ```
   Then openmarquee-firstboot.service detects + moves to
   `/etc/NetworkManager/system-connections/wifi.nmconnection` (chmod
   600). NM picks it up automatically — bypasses cloud-init
   network-config processing entirely. burn_sd_card.sh grows a
   `--wifi-ssid SSID --wifi-password PASS` flag that writes this
   keyfile if specified.

## Does this also explain openMarqueeDev's success?

YES — partial answer. Per `[[project_dev_pi_provisioned]]` memory:
"Dev Pi provisioned and serving the UI on port 80 — 2026-05-05".
Phase B SD-card automation didn't land until 2026-05-11. So
openMarqueeDev was set up BEFORE the current burn flow existed; it
was provisioned via a different path (likely a hand-run install.sh
over rsync from the dev box, per `[[project_phase6_hdmi_landed]]`).
That path does NOT use the staged user-data with the apt step. So
openMarqueeDev's apparent health doesn't validate the current
factory-fresh path; it's a separate provisioning lineage that
predates the regression.

Specifically: openMarqueeDev's WiFi is NM-managed via a
hand-installed NM keyfile (per `[[reference_demo_system]]` +
the wifi_station.py architecture). The same keyfile mechanism is
the Phase 4e fix for new Pis.

## Verification commands (for QA + qarl tonight)

1. Mount the booted SD's bootfs, inspect the live user-data:
   ```
   diskutil mount disk2s1  # bootfs
   grep -E "package_update|packages" /Volumes/bootfs/user-data
   ```
   Should show the apt step.

2. Mount the booted SD's rootfs (ext4fuse-mac), inspect cloud-init logs:
   ```
   ext4fuse /dev/disk2s2 ~/sd-rootfs
   tail /private/var/lib/cloud/instance/cloud-init-output.log
   ```
   Should show the apt failure or runcmd never firing.

3. If both confirm: edit user-data to remove apt step, re-bump
   instance-id, eject, reboot. AP should come up within ~2 min.

## Out of scope

- Pre-Phase 4e source change: this is investigate-only per dispatch
- Halftone/bricks polish (Phase 3af)
- Motion/transitions parity arc

## Next slice candidates

- **Phase 4e-a (1-line ship-tonight)**: drop apt step from stage_sd_card.sh
  user-data + add zstd to 00-packages if missing. 5-LOC change. Ship +
  re-bundle + re-flash.
- **Phase 4e-b (operator WiFi pre-config)**: NM keyfile path. Adds
  burn_sd_card.sh `--wifi-ssid/--wifi-password` flags + firstboot
  detect-and-move logic. ~50 LOC.
- **Phase 4f**: clarify README's outdated NetworkManager-absent claim
  to match wifi_station.py's NM-via-nmcli reality.
