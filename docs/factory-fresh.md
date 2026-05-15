# Factory-fresh boot — the offline-install promise

openmarquee.com markets escape from vendor lock-in to fragile sign
software, which implies the Pi works when plugged in cold without
internet. That promise is enforced by two architectural decisions
that together let a freshly-flashed SD card bring up the AP at
`10.0.0.1` without ever touching the network.

## The promise

1. Flash an SD card with `scripts/burn_sd_card.sh`.
2. Insert into a Pi Zero 2 W / Pi 4 and power on.
3. Within ~5 min, an AP named `MySign-<4hex>` is broadcasting.
4. Connect a laptop/phone to it, browse to `http://10.0.0.1/`, fill
   out the welcome UI to attach the Pi to your local WiFi.

Steps 1-3 require ZERO external network. The Pi has not phoned
home, has not done `apt update`, has not done `pip install`, has not
fetched a single byte from the internet. Step 4 (and everything
after) may use the network, but bootstrap to AP-ready does not.

## How apt is handled (image build time, not first boot)

The apt packages required to run the AP + backend (hostapd, dnsmasq,
iptables, python3-pip, python3-venv, ffmpeg, fonts-dejavu, qrencode,
git, rsync, ca-certificates, cloud-init, wireless-regdb, iw,
wireless-tools, wpasupplicant, python3, zstd, v4l-utils — list lives
in `00-packages`) are **baked into the Pi OS Lite image at pi-gen
build time** via
`images/openmarquee/stage-openmarquee/00-install-packages/00-packages`.
The builder machine (which DOES have network) runs pi-gen, which apt-
installs those packages into the image. The image is shipped ready-
to-flash.

Cloud-init's user-data explicitly sets `package_update: false` and
`package_upgrade: false` and never declares a `packages:` directive.
Result: on first boot the Pi never invokes `apt`. Adding packages
to cloud-init's `packages:` list at any future date would break the
factory-fresh promise — don't.

**Adding a new apt package**: edit `00-packages`, rebuild the Pi OS
image via `images/openmarquee/build-image.sh`, re-flash. There is no
shortcut.

## How pip is handled (bundle wheels, install.sh offline switch)

Python deps from `backend/requirements.lock` (~40 packages) are
**vendored as aarch64 wheels into the SD bundle** at build time by
`scripts/build_sd_bundle.sh`. The wheels land under
`/opt/openmarquee/wheels/` on the booted Pi.

`scripts/install.sh` detects the directory and switches pip to
fully-offline mode:

```
pip install --no-index --find-links=/opt/openmarquee/wheels/
            --no-build-isolation --upgrade -r requirements.lock
pip install --no-index --find-links=/opt/openmarquee/wheels/
            --no-build-isolation --upgrade -e /opt/openmarquee/backend
```

- `--no-index` refuses to consult PyPI.
- `--find-links` resolves only against the bundled wheels.
- `--no-build-isolation` reuses the venv's pre-installed setuptools
  instead of fetching PEP-517 build deps from PyPI. Python 3.13's
  venv no longer installs setuptools by default, so
  `build_sd_bundle.sh` also vendors `setuptools/wheel/pip` as pure-
  Python wheels.

If `/opt/openmarquee/wheels/` is missing or empty (e.g., a dev
redeploy via `scripts/deploy.sh`, which does not rsync `wheels/`),
install.sh falls back to the previous online-pip behavior. This
preserves the fast dev-iteration loop while keeping fresh-flash
first-boot offline.

**Adding a new Python dep**: edit `backend/requirements.txt`,
regenerate `requirements.lock` (pip-compile or equivalent), rebuild
the SD bundle via `scripts/build_sd_bundle.sh`. The bundle's
`wheels/` will reflect the new dep.

### manylinux platform tags

`pip download` is strict about wheel-tag matching. Pi OS Lite trixie
ships glibc 2.36; any wheel tagged `manylinux_2_X` with `X <= 36` is
forward-compatible. `build_sd_bundle.sh` enumerates the modern
manylinux variants explicitly (`manylinux_2_17`, `_2_26`, `_2_28`,
`_2_31`, plus legacy `manylinux2014_aarch64` and `linux_aarch64`).
The 2026-05-15 Phase 4c-1 incident was a missing `manylinux_2_28`
entry that caused `argon2-cffi-bindings 25.1.0` to fail
"no matching distribution found" at build time — exactly the LOUD-
fail signature the `--only-binary=:all:` flag guarantees.

If a future package publishes wheels with a newer manylinux tag,
extend the `--platform` list in `build_sd_bundle.sh`.

## The incident this prevents (2026-05-15)

QA flashed an SD card with the pre-Phase-4a build, inserted into a
factory-fresh Pi with no network. Boot trace from the rootfs's
`/var/log/cloud-init-output.log`:

```
[install.sh] Install backend package into venv (pip install -e .)
[pip]        Collecting aioice==0.10.2
[pip]        ERROR: Could not install packages due to an OSError:
             HTTPSConnectionPool(host='pypi.org', port=443):
             Max retries exceeded with url: /simple/aioice/
             (Caused by NameResolutionError("Temporary failure in
              name resolution"))
[install.sh] exit non-zero
[systemd]    openmarquee-firstboot.service never enabled
[systemd]    AP never came up
[user]       no recovery path → factory-fresh promise broken
```

Phase 4a (commit `2473cfb`) wired install.sh's offline-wheels
switch. Phase 4c-1 (`0c8cf2e`) expanded the manylinux platform
list so the build step itself doesn't fail on modern wheels.

After both, install.sh runs through cleanly with no network and
the AP comes up as designed.

## Verification

- `pytest backend/tests/test_install_sh.py` — 29 tests, covers both
  the offline path (wheels/ present) and the online fallback path.
- `bash scripts/build_sd_bundle.sh --output /tmp/test.tar.zst` —
  produces a 155 MiB bundle with 42 wheels (14 aarch64 + 28 pure-
  python including setuptools/wheel/pip). Zero x86_64 wheels (the
  `--only-binary=:all:` guard is active).
- `pip install --no-index --find-links=<bundle>/wheels/ --dry-run
  -r <bundle>/requirements.lock` (with the same manylinux flags) —
  reports `Would install` for the full 40-package set with no
  unresolved deps.
- Phase 4c-3 ship gate (separate): flash a real SD, boot a Pi in a
  network-isolated environment, verify AP comes up + welcome UI
  responds. This is the human-in-the-loop validation.

## Related

- `docs/sd-burn.md` — operator instructions for `scripts/burn_sd_card.sh`.
- `images/openmarquee/pi-gen.config` — pi-gen image builder config.
- `images/openmarquee/cloud-init/user-data` — first-boot directives.
- `scripts/install.sh` — on-device provisioning (pip offline switch).
- `scripts/build_sd_bundle.sh` — bundle builder (wheel vendoring).
