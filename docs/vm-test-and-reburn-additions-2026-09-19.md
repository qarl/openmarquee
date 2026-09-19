# VM-test results + reburn additions (2026-09-19)

Jimmy-openmarquee-code → admin. Gate-2 VM test + analysis of admin's two
reburn additions (base-ssh, home-wifi). Heavy pi-gen rebuild remains HELD for
admin's GO + fleet-safety disk check.

## Gate 2 — VM test (pi-test Lima VM)

Environment: **aarch64, Debian 13 trixie, cloud-init 25.1.4, systemd 257** —
high-fidelity to the pi-gen image (same arch + release + cloud-init major).

Scope (agreed): the dwc2 `[all] dtoverlay=dwc2,dr_mode=peripheral` is Pi-firmware
config.txt, NOT parsed by Lima/QEMU — covered by test-boot-config.sh + the
[cm4] regression, provable only on real HW. The VM covers the cloud-init +
install chain.

### Validated
- **cloud-init unit names on 25.1.4**: `cloud-init-local`, `cloud-init-network`,
  `cloud-init-main`, `cloud-config`, `cloud-final` (+ `cloud-init.target`,
  `cloud-init-hotplugd`). My 06-run.sh tolerant enable loop covers all present
  names; it also lists the legacy `cloud-init.service` (absent on 25.1.4 →
  correctly skipped). Enablement fix is correct on the real version. (On this
  VM the units are already enabled — Lima provisions via cloud-init; on the
  pi-gen RPi-OS image apt-install leaves them disabled = the bug 06 fixes.)
- **99_openmarquee.cfg**: `cloud-init schema` says "unrecognized user-data
  header" — FALSE ALARM. That validator checks *user-data*; this is a
  cloud.cfg.d datasource drop-in (system config), correctly not user-data.
- **NoCloud seedfrom `file:///boot/firmware/`**: standard NoCloud file:// seed.
  Admin's live card is the real-HW proof of consumption (the VM can't fully
  exercise it without hijacking Lima's own system cloud-init).

### BUG FOUND + FIXED (the VM earned its keep)
`images/openmarquee/cloud-init/user-data` runcmd had:
```
  - usermod -aG audio openmarquee || echo "warn: usermod audio failed"
```
The unquoted `: ` inside makes YAML parse the entry as a **mapping**, not a
string — `cloud-init schema` on 25.1.4 = INVALID user-data. Fixed to list form
`- [ sh, -c, '...' ]`; re-validated = **Valid schema**.

**Blast radius: LIMITED.** This is the BASE user-data (base-image-standalone
path). The STAGED card uses `stage_sd_card.sh`'s own inline user-data, which
ALREADY uses safe list-form runcmd (`- [ systemctl, ... ]`, `- [ sh, -c, ... ]`)
→ unaffected. So NOT a fireplacesign-burn blocker, but a real bug now fixed.

## Reburn addition #1 — ssh at base level

CONFIRMED: ssh is enabled ONLY via cloud-init (base user-data `systemctl enable
ssh`; stage_sd_card user-data `systemctl unmask/enable --now ssh.service`). Not
at base. 04-ssh-user only writes sshd_config + sets up the user; it does not
enable the service.

**Enabling at base is necessary-but-NOT-sufficient.** Two more gaps make
"ssh works even if cloud-init hiccups" real:
- The operator KEY also reaches the device only via cloud-init: `build-image.sh
  --ssh-key` substitutes the key into the cloud-init user-data ONLY; it does not
  bake `authorized_keys` into the rootfs. Base-enabled ssh with no baked key =
  ssh up but no login without cloud-init.
- `openssh-server` is NOT in 00-packages (relies on the Pi OS Lite base image;
  service disabled by default).

Proposed complete fix (confirm before I implement):
  a. add `openssh-server` to 00-packages (guarantee present).
  b. `systemctl enable ssh` in the 04-ssh-user chroot.
  c. extend `build-image.sh --ssh-key` to ALSO bake the key into the image's
     `/home/openmarquee/.ssh/authorized_keys` (0600 openmarquee:openmarquee).
Security: key-only + no-password + no-root is already baked
(sshd_config.d/openmarquee.conf), so an enabled ssh with a baked operator key
is exactly the intended access path — no new exposure.
Admin pre-flight debugfs check should then also verify authorized_keys present
+ ssh.service enabled.

## Reburn addition #2 — home wifi bake path

RECOMMEND: **NM `.nmconnection` keyfile via `stage_sd_card.sh --wifi-profiles
<dir>`.** NOT cloud-init network-config wifi — the repo README documents that
cloud-init's `wifis:` block is BROKEN on this image (cloud-init writes eni
format that NetworkManager silently ignores; see the cloud-init-wifis
investigation capture). `stage_sd_card --wifi-profiles` already splices a
pre-NM bootcmd that installs NM keyfiles (proven path), and keeps Karl's PSK
out of git (per-card staging). On Karl's SSID+PSK I'll generate
`<SSID>.nmconnection` (`[wifi] ssid=...`, `[wifi-security] key-mgmt=wpa-psk
psk=...`, `[connection] id/type/autoconnect`, 0600 root:root) and we stage with
`--wifi-profiles`.

## VM-boot division with QA
QA offered to drive the full VM-test of the REBUILT IMAGE (Lima boot of the
actual artifact) once it exists — accept: that faithful full-image boot is QA's
historical lane. This provision-logic VM test (unit names + schema-validation +
the user-data bug) is complementary. pi-test VM left running for QA.

## Status
- user-data YAML fix: staged (commit bundling with the ssh change once #1 is
  confirmed).
- Rebuild: HELD for admin GO + disk check.
