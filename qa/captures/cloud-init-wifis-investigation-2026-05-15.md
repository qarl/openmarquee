# cloud-init `wifis:` block silently failing on the openmarquee image

**Date:** 2026-05-15
**Dispatch:** Phase 4e-b shipped an NM-keyfile-bypass operator API
(commit 9c7ae78). This investigation answers WHY cloud-init's native
`network-config` `wifis:` block silently failed to translate into
NM keyfiles when QA tested it tonight (NM keyfile dir was empty on
rootfs after a `wifis:`-edited card booted twice).
**Status:** Diagnostic-only. No source change recommended for this
slice; Phase 4e-b remains canonical.

## Source-side survey

The openmarquee image lays down NO custom cloud-init configuration
beyond the documented user-data + meta-data + network-config files
under `/boot/firmware/`:

- `images/openmarquee/stage-openmarquee/` contains only
  `00-install-packages/00-packages` (apt package list) and
  `prerun.sh` (pi-gen boilerplate). No `cloud.cfg.d/*.cfg` drops.
- `images/openmarquee/cloud-init/user-data` carries `package_update:
  false` (Phase 4e-a) + ssh keys + bootcmd + runcmd. No `cc_*`
  module disables, no renderer overrides.
- `scripts/stage_sd_card.sh` writes a `network-config` to bootfs
  with `version: 2` + `ethernets.eth0.dhcp4: true` and an explicit
  comment that wifis: is intentionally omitted (operator-side
  pre-config goes through Phase 4e-b's NM keyfile path).

So the cloud-init configuration on the booted Pi is **exactly the
Pi OS Lite trixie base** — no openmarquee overlay touches the
network-handling modules.

## Three candidate root causes

### A. cloud-init's NM renderer not selected (MOST LIKELY)

Pi OS Lite trixie ships both `network-manager` (the new default
network stack) AND legacy `ifupdown` (`/etc/network/interfaces`).
cloud-init's renderer-selection logic on Debian iterates a
priority list and picks the first viable renderer. The priority
list is configurable via `/etc/cloud/cloud.cfg.d/*-net*.cfg`
drops, e.g.:

```yaml
system_info:
  network:
    renderers: [netplan, eni, networkd, sysconfig, network-manager, freebsd, netbsd]
```

The cloud-init Debian package historically ships a drop that
prefers `eni` (ifupdown) over `network-manager` for backward
compatibility with older Debian installs that still use
`/etc/network/interfaces` natively. If the image inherits that
default ordering, cloud-init writes the `wifis:` block as an
`/etc/network/interfaces.d/wlan0.cfg` entry (the eni format).
NetworkManager on trixie is configured with `[ifupdown]
managed=false` by default, so it ignores anything in
`/etc/network/interfaces.d/` — the wifi credentials silently
vanish.

Result: NM keyfile dir is empty (cloud-init wrote eni format,
not keyfile format); NM doesn't see the credentials; Pi never
joins station-mode wifi.

Definitive evidence requires on-disk inspection of:
- `/etc/cloud/cloud.cfg.d/*-net*.cfg` — confirm `renderers:` order
- `/var/log/cloud-init.log` — grep for `"selected renderer"`
- `/etc/network/interfaces.d/` — look for a wlan0.cfg drop
- `/var/lib/cloud/instance/network-config.json` — confirm
  cloud-init parsed the wifis: block AT ALL

### B. cloud-init's network module disabled (UNLIKELY)

Cloud-init's network module would have to be explicitly removed
from `cloud_init_modules` in `/etc/cloud/cloud.cfg`. Pi OS Lite
trixie's base config includes the module. Our image doesn't
override it. This candidate is refuted unless Pi OS Lite trixie
itself disables it (unprecedented).

### C. cloud-init wrote the keyfile but something cleared it (UNLIKELY)

For "empty dir post-boot," `openmarquee-firstboot.sh` or
`openmarquee-ap0.service` would need to actively `rm` the
keyfile after cloud-init wrote it. Neither does — both leave
`/etc/NetworkManager/system-connections/` alone. The Phase 4e-b
flow that we just shipped ADDS a keyfile from bootfs but never
removes existing ones. Refuted.

## Why Phase 4e-b is the right canonical path regardless

Even if (A) is fixed by adding a custom `cloud.cfg.d/99-openmarquee-
network.cfg` drop that pins `renderers: [network-manager]`, the
cloud-init path has additional friction:

1. **Caching across reboots.** cloud-init's NoCloud datasource
   caches the rendered network state in
   `/var/lib/cloud/instance/network-config.json`. Bumping
   meta-data instance-id re-triggers user-data + runcmd but does
   NOT reliably re-render network. Operators editing network-config
   between reboots is surprising-by-default.
2. **Keyfile perms.** cloud-init's NM renderer chmods 600
   correctly, but the timing relative to NM startup is implicit.
   Phase 4e-b's openmarquee-firstboot.sh runs `nmcli connection
   reload` explicitly after copy, removing the timing question.
3. **Audit trail.** Phase 4e-b leaves clear logging
   ("Operator pre-configured WiFi keyfile found; moving to ...")
   in journalctl. cloud-init's renderer logging is buried in
   cloud-init.log with no operator-visible breadcrumb.
4. **PSK lifetime on FAT32.** Phase 4e-b explicitly `rm`'s the
   bootfs copy of the keyfile after promotion to rootfs, so the
   plaintext psk doesn't linger on a FAT32 partition any host can
   mount. cloud-init reads /boot/firmware/network-config but
   doesn't delete it.

Phase 4e-b's design (`scripts/burn_sd_card.sh --wifi-ssid` →
`/boot/firmware/openmarquee-wifi.nmconnection` → firstboot.sh
detect-and-move) sidesteps all four issues. It's the better
operator API even when cloud-init's path is functioning.

## Recommendation

**Keep Phase 4e-b as the canonical operator path.** No source
change in this slice. Three follow-up options ranked by ROI:

1. **Phase 4g (optional, low-priority): docs-only note in
   factory-fresh.md** documenting that cloud-init's `wifis:`
   block in network-config is NOT supported — operators should
   use `burn_sd_card.sh --wifi-ssid` instead. ~5 LOC. Prevents
   future surprise.
2. **Phase 4h (optional, medium-priority): on-disk diagnostic
   from qarl/QA** to pin down which of (A)/(B)/(C) is the actual
   root cause. Requires ext4fuse-mac of the rootfs and surveying:
   - `/etc/cloud/cloud.cfg.d/*-net*.cfg`
   - `/var/log/cloud-init.log` (renderer selection line)
   - `/var/lib/cloud/instance/network-config.json`
   - `/etc/network/interfaces.d/` (eni drops, if any)
   Confirms (A) hypothesis and tells us whether Pi OS Lite trixie
   ships eni-preference or not.
3. **Phase 4i (optional, low-priority): if (A) confirmed, ship a
   `cloud.cfg.d/99-openmarquee-network.cfg` drop in the pi-gen
   stage** that pins `renderers: [network-manager]`. Would make
   cloud-init's path work BUT introduces a path that competes
   with Phase 4e-b. Probably skip; documenting the preferred path
   (option 1) is enough.

## Limitations

- **Source-only investigation.** Confident on what the openmarquee
  codebase lays down (nothing custom). NOT confident on what Pi
  OS Lite trixie's cloud-init base config does — that requires
  on-disk verification. Hypothesis (A) is the documented Debian
  default behavior pre-trixie but trixie may have shifted.
- **QA's exact wifis: edit not preserved.** The `wifis:` YAML
  syntax that QA pasted into network-config isn't archived. If
  the YAML had a syntax error (e.g., missing `access-points:`
  level), cloud-init would log a parse warning and silently skip
  the block. Less likely than (A) but possible.
- **Two-boot evidence.** QA observed empty NM keyfile dir after
  TWO reboots. The instance-id bump should re-fire user-data but
  cloud-init's network-config processing has different caching
  semantics. Without cloud-init.log, can't disambiguate
  "didn't run again" vs "ran but produced nothing."

## Verdict

**A (cloud-init's NM renderer not selected) is the most likely
root cause, but Phase 4e-b's NM keyfile bypass is the right
canonical path regardless.** Definitive root cause requires
on-disk inspection (queued as optional Phase 4h).

Phase 4e-b stays canonical. Phase 4g (docs note in
factory-fresh.md) is the recommended cheap follow-up to prevent
future operators from trying the cloud-init path and silently
failing the same way QA did tonight.

## Other surfaces during the trace

- `images/openmarquee/cloud-init/README.md:50` says "Default
  cloud-init network config is fine for our setup (DHCP on
  whatever interfaces are up). station-mode WiFi join is handled
  later, post-captive-portal." This is consistent with the
  Phase 4e-b architecture and doesn't need updating.
- `scripts/stage_sd_card.sh:118-130` has an explicit "Intentionally
  no `wifis:` section" comment in the network-config writer. Also
  consistent with the canonical Phase 4e-b path. No drift here.
- Nothing else surfaced during the survey.
