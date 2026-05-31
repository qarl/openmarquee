# r36 — SD bundle rebuild audit (v0.9.0 → v1.0.0 delta + concerns)

**Author lane:** code2 (static analysis only — no SD bundle build,
no SD-card burn, no SSH). Same shape as r30/r31/r33/r34/r35
recommendation docs.

**Audience:** code1 / whoever owns the bundle rebuild lane in a
future r37 (code1's numbering) dispatch.

**Why it exists.** The v0.9.0 SD bundle
(`~/tmp/openmarquee-sd-bundle-v0.9.0.tar.zst`, 152.1 MiB, SHA256
`9aec7cdaef771735d8085769d37b4d85b9f7e37c63c9db2281860742cdeaf987`
per session-end memory) was the last bundle build before v1.0.0
ship. Fresh customer burns of that bundle would now miss:

- v1.0.0 RELEASE commit + tag
- r25 glyph prewarm renderer
- r29 install.sh section reorder + atomic-rename
- r31 wheels refresh + ffmpeg-dev (recipe-side)
- r32 emoji font deploy guard
- r33 deploy.sh manifest test
- r34 dual-radio USB-WiFi-dongle topology (NEW udev rule)
- code2 r32 pre-push hook tightening
- All doc + hygiene rounds

For customer fresh-burns to be coherent with FYS prod and the
v1.0 ship, the bundle needs a rebuild + sanity-check.

**Origin/main HEAD at audit time:** `33233c5` (post-r35 FPS audit
cherry-pick). All file:line citations below verified against
that SHA or against code2 HEAD `a1cdcfa` where files match
byte-for-byte (`scripts/build_sd_bundle.sh` is byte-identical
between code2 and main; verified via `diff` against a /tmp
clone).

---

## Section A — Inventory: what the v0.9.0 bundle contains

`scripts/build_sd_bundle.sh` is **459 LOC**; the contract is
documented in its header comment block.

### A.1 Tarball top-level layout

Rooted at `openmarquee/` inside the .tar.zst:

| Path | Source | Bundle script section |
| --- | --- | --- |
| `backend/` | repo `backend/` (excludes tests + caches + runtime state + secrets) | lines 126-149 |
| `ui/` | repo `ui/` (excludes src, e2e, node_modules, tests); REQUIRES `ui/dist/` to exist | lines 155-180 |
| `scripts/` | repo `scripts/` (excludes `build_sd_bundle.sh`, `stage_sd_card.sh`, caches) | lines 182-194 |
| `system/` | repo `system/` (excludes only `._*` + `.DS_Store` + `.Jimmy/`) | lines 196-202 |
| `images/` | repo `images/` (pi-gen substage tree — Plymouth theme + boot-config-lib + handoff drop-in) | lines 210-216 |
| `bin/openmarquee-render` | `renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render` (with `$HOME/tmp/openmarquee-build/...` fallback) | lines 220-248 |
| `wheels/` | `pip download` per `requirements.lock` + bootstrap setuptools/wheel/pip | lines 252-314 |
| `debs/` | 5 vendored trixie .debs (hostapd + iptables + libip4tc2 + libip6tc2 + dnsmasq); SHA256-pinned + size-verified | lines 343-395 |
| `pyproject.toml` | repo `backend/pyproject.toml` (copy) | line 401 |
| `requirements.lock` | repo `backend/requirements.lock` (copy) | line 402 |
| `MANIFEST.txt` | git SHA + timestamp + content stats (wheel count, deb count, rust binary size, tree dump) | lines 404-425 |

### A.2 Build invariants

- **Output:** `dist/openmarquee-sd-bundle.tar.zst` by default;
  `--output PATH` overrides. The v0.9.0 build used a non-default
  `~/tmp/` location.
- **Secret scan** (lines 99-122): hard-fails on `.env*`,
  `credentials.*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`,
  `auth.json` anywhere outside `.git/`.
- **Arch sanity** (lines 236-243): `file` checks the Rust binary
  for `aarch64` / `ARM aarch64` / `ELF 64-bit`. Fails the build
  on x86_64-staged binary.
- **Wheel arch sanity** (lines 305-311): `find ... x86_64`
  matches → fails the build.
- **.deb integrity** (lines 369-388): SHA256 + size verified
  per .deb against the pinned manifest at lines 354-358.
- **macOS AppleDouble suppression** (line 47: `export
  COPYFILE_DISABLE=1`; line 434: `find ... -name '._*' -delete`).
- **No `setup.sh` invocation.** Bundle script assumes the build
  environment has already been set up.
- **No `(cd ui && npm run build)` invocation.** Errors out at
  line 155-158 if `ui/dist/` is missing or empty.
- **No `renderer_cross_build.sh` invocation.** Warns + ships
  without sidecar if cross-built binary is absent (lines
  245-248).

### A.3 Vendored .deb manifest (the 5 pinned to snapshot.debian.org)

Per lines 354-358, immutable timestamp `20260515T000000Z`:

1. `hostapd` 2:2.10-24 (~801 KB)
2. `iptables` 1.8.11-2 (~353 KB)
3. `libip4tc2` 1.8.11-2 (~19 KB)
4. `libip6tc2` 1.8.11-2 (~20 KB)
5. `dnsmasq` 2.91-1 (~69 KB)

Total: ~1.3 MB. Closure-regeneration recipe documented inline
(lines 347-353).

### A.4 What's IMPLICITLY shipped via rsync (not explicit fetch)

The `rsync` ui/ at lines 160-180 ships **anything in `ui/`**
except the named excludes. Specifically:

- `ui/fonts/*.ttf` — 25 font files (including bundled fonts
  like `inter.ttf`, `roboto-slab.ttf`, `unifrakturcook.ttf`).
- `ui/fonts/noto-color-emoji-colrv1.ttf` — **gitignored**, must
  be downloaded via `scripts/download-emoji-font-colrv1.sh`
  BEFORE the bundle build. If absent, the bundle ships without
  the emoji font.
- `ui/dist/` — must exist + non-empty per line 155 check.
- `ui/index.html`, `ui/welcome.html`, `ui/styles.css`,
  `ui/login.html`, `ui/set-password.html`, etc.

---

## Section B — Inventory: what's new since v0.9.0 (bundle impact)

The dispatch lists 9+ landed-since-v0.9.0 commits. For each,
bundle-side impact:

### B.1 v1.0.0 RELEASE (`57d95db`)

**Bundle impact:** YES — version stamp drift. The bundle's
`MANIFEST.txt` records `git rev-parse HEAD` at build time; a
rebuild from `33233c5` (current main HEAD) would record that SHA
instead of the v0.9.0 baseline. `pyproject.toml` may also carry
a version bump; `backend/openmarquee/__init__.py` or equivalent
similar.

**Action:** automatic via repo-state. Rebuild picks it up.

### B.2 r25 glyph prewarm renderer (`ab047a5`)

**Bundle impact:** YES — Rust binary cross-rebuild required.
The bundle reads `renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render`
or the BUILD_DIR mirror. r25 changed the source; the cross-built
artifact must be regenerated via `scripts/renderer_cross_build.sh`
BEFORE the bundle build OR the bundle will ship the stale
v0.9.0-era binary.

**Action:** code1 must run `bash scripts/renderer_cross_build.sh`
before `bash scripts/build_sd_bundle.sh`. Standing rule per
`[[feedback_cross_build_before_deploy]]`.

### B.3 r29 install.sh section reorder + atomic-rename (`5d2a9e9`)

**Bundle impact:** YES — install.sh source change. The bundle's
`scripts/` rsync at lines 186-194 picks up the new install.sh
(1131 LOC on main vs 958 on the v0.9.0-era code2). The reordered
section sequence ships automatically.

**Action:** automatic via repo-state.

### B.4 r31 wheels refresh + ffmpeg-dev (`687485d`)

**Bundle impact:** PARTIAL.

- **wheels refresh:** r31 added wheels refresh to `deploy.sh`, NOT
  to `build_sd_bundle.sh`. Bundle's `pip download` at lines
  270-283 already does the wheel refresh by construction (each
  rebuild fetches per `requirements.lock`). r31 was about
  bringing deploy.sh up to PARITY with the bundle behavior. No
  bundle-script change needed.
- **ffmpeg-dev:** r31's commit message states "Added 7
  `libav*-dev` packages to the pi-gen base-image package list
  (images/openmarquee/stage-openmarquee/00-install-packages/00-packages)".
  Code2 worktree currently shows only `ffmpeg` (runtime
  binary) in that file at HEAD `a1cdcfa`; the 7 `libav*-dev`
  additions live on origin/main and will pick up via the
  `images/` rsync at lines 210-216 once the rebuild runs on a
  worktree synced to main. The bundle's vendored `.deb`
  manifest at lines 354-358 does NOT include ffmpeg-dev .debs;
  the pi-gen-image path is the supply route. Redeploy-via-
  bundle path on a Pi that didn't get the pi-gen image (i.e.,
  bundle-only install) won't have ffmpeg-dev — meaning a future
  install.sh source-build fallback for av/pyav would fail.

**Action:** verify the pi-gen 00-packages list is current via
the images/ rsync (automatic). Decide whether to add 7
`libav*-dev` .debs to the bundle manifest (~1-3 MB additional
download). See §F.4.

### B.5 r32 emoji font deploy guard (`621b4f3`)

**Bundle impact:** **CRITICAL — bundle script lacks the same guard.**

r32's commit added a `bash "$OPENMARQUEE_SRC/scripts/download-emoji-font-colrv1.sh"`
call to `deploy.sh` BEFORE `sync_to_build_dir`. This protects
deploy.sh against `/tmp` clones that never ran `setup.sh`.

**`scripts/build_sd_bundle.sh` has NO equivalent guard.** Grep
for `download-emoji|noto-color-emoji|emoji-font` in the bundle
script returns zero matches.

If a fresh-clone build environment runs `bash scripts/build_sd_bundle.sh`
without first running `setup.sh`, the bundle ships with
`ui/fonts/noto-color-emoji-colrv1.ttf` ABSENT (the file is
gitignored; only `setup.sh` → `download-emoji-font-colrv1.sh`
puts it in place). The bundle's `ui/` rsync at line 160-180
silently copies whatever's there.

**Same regression class that bit FYS prod 2026-05-31 r31 → r32.**
Fresh customer burns of a stale-environment bundle would have
emoji tofu rendering on every emoji-bearing slide.

**Action:** code1's r37 rebuild dispatch MUST (a) verify
`ui/fonts/noto-color-emoji-colrv1.ttf` is present (size > 3 MB)
BEFORE the bundle build, OR (b) add a guard to
`build_sd_bundle.sh` mirroring r32's deploy.sh fix. **Strongly
recommend (b)** as the structural fix; (a) is the manual
sanity-check the rebuilder must do AS WELL even with (b) until
(b) is itself shipped. See §C.1 + §D.2.

### B.6 r33 deploy.sh runtime-asset manifest test (`3cee501`)

**Bundle impact:** PARTIAL.

- The test (`scripts/tests/test_deploy_sh_runtime_asset_manifest.sh`)
  covers `deploy.sh`, NOT `build_sd_bundle.sh`.
- The bundle's `scripts/` rsync at lines 186-194 has NO
  `--exclude 'tests/'`, so `scripts/tests/` ships in the bundle.
  Harmless (operators can run them, or not), but adds ~1 KB to
  the bundle.
- The same bug class (gitignored-asset wipe via rsync --delete)
  applies to the bundle in a different shape: the bundle's `ui/`
  rsync has no `--delete` (line 160 uses `rsync -a --delete` ←
  WAIT it does have `--delete`); a stale BUILD_DIR mirror could
  cause similar wipe-via-stage but the staging is fresh per
  build (`mktemp -d` at line 86), so this specific issue is
  bounded to the source tree state at build time.

**Action:** flag for §F as out-of-scope of r36: add a parallel
`test_build_sd_bundle_runtime_asset_manifest.sh` that exercises
the bundle script's asset-presence assumptions. Same shape as
the r33 test but targeting `build_sd_bundle.sh`. Out-of-scope
for r36 (we're auditing, not implementing).

### B.7 r34 dual-radio USB-WiFi-dongle topology (`b5b9919`)

**Bundle impact:** YES — multiple files.

- `system/99-openmarquee-usb-wlan.rules` — NEW udev rule. Picked
  up automatically by the `system/` rsync at lines 196-202.
- `scripts/install.sh §5b` — udev install step. Picked up by the
  `scripts/` rsync at lines 186-194.
- `scripts/burn_sd_card.sh` — gains `--mgmt-wifi-ssid` /
  `--mgmt-wifi-password` flags. **CRITICAL CAVEAT:**
  `build_sd_bundle.sh:193` `--exclude 'stage_sd_card.sh'` BUT
  `burn_sd_card.sh` IS NOT EXCLUDED — so the bundle ships
  `scripts/burn_sd_card.sh`. Good. On-device redeploys could
  re-burn additional cards via this. But more importantly:
  bundles to be SHIPPED to operators don't carry the
  intent-to-burn-more-cards use case typically; the script ships
  anyway with no harm.
- `system/openmarquee-firstboot.sh §5d` — new mgmt-keyfile
  drop. Picked up via system/ rsync.
- `system/README.md` — dual-radio docs (~125 LOC). Picked up via
  system/ rsync.
- `docs/dual-radio-shipping-test.md` — NEW per r34's commit
  msg. **NOT** picked up: the bundle doesn't ship `docs/` (no
  rsync clause for it).
- `backend/tests/test_firstboot_oneshot.py` — new tests. NOT
  picked up: `backend/` rsync at line 134 excludes `tests/`.

**Action:** automatic via repo-state. The udev rule + install.sh
§5b + firstboot.sh §5d + system/README.md ship in the rebuild.
`docs/dual-radio-shipping-test.md` is missing from the bundle by
construction; it's a developer doc, not a runtime artifact, so
this is fine.

### B.8 code2 r32 pre-push hook tightening (`d06c506`)

**Bundle impact:** NO. The `.githooks/pre-push` change is in
the dev's local git config (`core.hooksPath .githooks`),
applied via `scripts/install-git-hooks.sh`. Not a runtime
artifact; not bundled. The bundle script's `scripts/` rsync
includes `install-git-hooks.sh`, but that's a no-op on a
factory-burned device (no git checkout, no hook to install).

### B.9 r33 hdmi.rs:3520 BT.709 + r33 v4l2 ride-along

**Bundle impact:** YES — Rust binary cross-rebuild required.
Same shape as B.2 (r25). The new comment-only changes don't
affect runtime behavior but the cross-built binary needs to be
regenerated for SHA-fidelity tracking.

**Action:** automatic in the cross-rebuild step.

### B.10 Doc rounds (r27/r28/r30/r31/r33/r34/r35/r36)

**Bundle impact:** NO. All under `qa/`, which is NOT in any
of the bundle's rsync paths (`backend/`, `ui/`, `scripts/`,
`system/`, `images/`). Implicitly excluded by virtue of not
being copied.

### B.11 Summary table

| Commit | Bundle impact | Action |
| --- | --- | --- |
| `57d95db` v1.0.0 RELEASE | YES | automatic via repo-state |
| `ab047a5` r25 glyph prewarm | YES | renderer cross-rebuild |
| `5d2a9e9` r29 install.sh reorder | YES | automatic |
| `687485d` r31 wheels refresh + ffmpeg-dev | PARTIAL | auto for wheels; ffmpeg-dev pi-gen-only; bundle .deb manifest NEEDS DECISION |
| `621b4f3` r32 emoji deploy guard | **CRITICAL** | bundle lacks parallel guard |
| `3cee501` r33 deploy manifest test | PARTIAL | scripts/tests ships harmlessly; bundle-side test NEEDED (F.5) |
| `b5b9919` r34 dual-radio dongle | YES | automatic; system/ + install.sh §5b auto-picked-up |
| `d06c506` code2 r32 pre-push tighten | NO | dev-only |
| Doc rounds | NO | qa/ not bundled |

---

## Section C — Pre-rebuild concerns

### C.1 CRITICAL: emoji font dependency on build environment

`build_sd_bundle.sh` does NOT call
`scripts/download-emoji-font-colrv1.sh`. A fresh-clone build
environment that never ran `setup.sh` will ship the bundle
WITHOUT `ui/fonts/noto-color-emoji-colrv1.ttf`.

Same regression class as r31 → r32 deploy.sh fix.

**Mitigation in r37 rebuild dispatch:**

A. **Manual pre-flight check (always do this).** Before
   `bash scripts/build_sd_bundle.sh`, verify:
   ```
   ls -la ui/fonts/noto-color-emoji-colrv1.ttf
   # Must be > 3 MB (~4.8 MB after fontTools strip)
   ```

B. **Structural fix (recommend code1 fold into the rebuild
   commit).** Add to `build_sd_bundle.sh` immediately before the
   `ui/` rsync (around line 158):
   ```bash
   # r36: ensure COLRv1 emoji font present in source tree before
   # the ui/ rsync. The font is gitignored (~5 MB binary); without
   # this guard a fresh-clone build environment ships the bundle
   # without it. Same regression class deploy.sh got in r32.
   echo "==> ensure COLRv1 emoji font present in source tree"
   bash "$REPO_ROOT/scripts/download-emoji-font-colrv1.sh"
   ```
   ~5 LOC. Idempotent (the download script SHA-pins + skips on
   existing file).

C. **Test guard.** Add `scripts/tests/test_build_sd_bundle_runtime_asset_manifest.sh`
   parallel to the r33 deploy.sh test (out-of-scope of r36;
   see §F.5).

### C.2 Cross-build environment for the renderer binary

The bundle reads from `renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render`
or `$HOME/tmp/openmarquee-build/renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render`.

**The standing rule per `[[feedback_cross_build_before_deploy]]`** is
that the pre-push hook GATES on aarch64 cross-compile but does
NOT BUILD. The actual cross-build is `bash scripts/renderer_cross_build.sh`
— a separate ~3-5 min step.

If code1 runs `build_sd_bundle.sh` from a tree where:
- Local target/ has a stale binary from earlier (e.g. v0.9.0-era)
- BUILD_DIR mirror has the latest binary

The bundle picks **local** first (line 223-224). **Stale-binary
risk** if the local repo's target/ wasn't cleaned + cross-rebuilt.

**Mitigation:** explicit pre-flight verification:
```
file renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render
# Must include "aarch64" + "ELF 64-bit"
stat -f%m renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render
# Modification time must be >= the HEAD commit time
```

The bundle script itself fails loudly on x86_64 binary (line
236-243), so the most-likely failure mode is "stale aarch64
binary" not "wrong-arch binary."

### C.3 Wheel resolver in the bundle build context

Same multi-manylinux platform list as deploy.sh (per r31). Will
pick up the certifi 2026.5.20 pin that r31 fixed. **No bundle-
specific concern.**

The bundle's wheel-arch sanity gate at lines 305-311 catches
the x86_64 wheel slip-through that r31 also guarded against.

### C.4 Bundle size estimate

v0.9.0 baseline: 152.1 MiB.

Additions:
- Emoji font ($COLRv1$ stripped): +4.8 MB (CRITICAL — if it ships)
- udev rule: +1 KB
- system/dnsmasq.service.d drop-in: +few KB
- Refreshed wheels (certifi 2026.5.20 + other r31-vintage pin
  bumps): roughly unchanged size; just newer SHAs
- r25 renderer prewarm code: binary size delta unknown without
  rebuild; r25 added ~30 KB of source-side prewarm code,
  compiled binary delta probably +50-200 KB
- r34 install.sh §5b udev install step: +~25 LOC, negligible

Rough estimate post-rebuild: **152.1 → ~157-160 MiB**.

If §C.1 mitigation (B) is implemented + run, the +4.8 MB emoji
font lands; if not, the bundle could ship at ~152 MiB and be
silently emoji-broken.

### C.5 ui/dist/ pre-flight

The bundle errors out at lines 155-158 if `ui/dist/` is missing
or empty:
```
error: ui/dist missing or empty; run `(cd ui && npm run build)` first
```

Mandatory pre-flight: `(cd ui && npm run build)` before the
bundle build.

### C.6 deploy.sh artifact

The bundle's `scripts/` rsync at lines 186-194 ships `deploy.sh`
in the bundle. **Recently r31 + r32 changed deploy.sh** (wheels
refresh + emoji font guard). The current `deploy.sh` IS
intended to be in the bundle so on-device redeploys (e.g. from
a Pi to a peer) work with the latest pipeline. The bundle's
deploy.sh will reflect r31 + r32 changes after rebuild.
**No bundle-specific concern.**

---

## Section D — Concrete rebuild dispatch sketch (for r37 / code1)

### D.1 Pre-rebuild checklist

Before running `bash scripts/build_sd_bundle.sh`:

1. **Pull origin/main to current HEAD** (`33233c5` or later).
2. **Run `bash scripts/setup.sh`** — populates `ui/fonts/noto-color-emoji-colrv1.ttf`
   + creates Python venv + npm-installs ui/.
3. **Cross-build the renderer**:
   ```
   bash scripts/renderer_cross_build.sh
   ```
   ~3-5 min. Verify:
   ```
   file renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render
   # Expected: "ELF 64-bit ... aarch64"
   ```
4. **Build the UI**:
   ```
   (cd ui && npm run build)
   ```
   ~1s. Verify `ui/dist/` is non-empty.
5. **Run the deploy.sh manifest test** (catches gitignored-asset
   regressions in deploy.sh; sanity proxy for similar bundle
   issues):
   ```
   bash scripts/tests/test_deploy_sh_runtime_asset_manifest.sh
   ```
6. **Verify emoji font is present** (the §C.1 critical guard):
   ```
   ls -la ui/fonts/noto-color-emoji-colrv1.ttf
   # > 3 MB
   ```
7. **(Recommended) apply the §C.1 mitigation B fix** to the bundle
   script BEFORE the rebuild so the next build inherits the
   structural guard.

### D.2 Rebuild command

```bash
bash scripts/build_sd_bundle.sh \
    --output ~/tmp/openmarquee-sd-bundle-v1.0.0.tar.zst
```

Expected wall-clock: **~10-20 min** on a code1 dev host
(pip download dominates: ~30+ wheel fetches over PyPI/wheel
mirrors, ~10 min depending on network).

### D.3 Post-rebuild verification

1. **Bundle size**:
   ```
   ls -la ~/tmp/openmarquee-sd-bundle-v1.0.0.tar.zst
   # Expected: ~157-160 MiB
   ```
2. **SHA256 stamp**:
   ```
   shasum -a 256 ~/tmp/openmarquee-sd-bundle-v1.0.0.tar.zst
   # Record this in the QA pingback for verification
   ```
3. **Content verification** (`tar -tzf` is slow for zstd;
   `tar --use-compress-program='zstd -d' -tf` is faster):
   ```
   tar --use-compress-program='zstd -d' -tf ~/tmp/openmarquee-sd-bundle-v1.0.0.tar.zst | head -50
   ```
   Confirm presence:
   - `openmarquee/bin/openmarquee-render` (aarch64 ELF)
   - `openmarquee/ui/fonts/noto-color-emoji-colrv1.ttf` (~4.8 MB)
   - `openmarquee/system/99-openmarquee-usb-wlan.rules` (r34 udev rule)
   - `openmarquee/system/dnsmasq.service.d/...` (the r34-vintage drop-in)
   - `openmarquee/wheels/certifi-2026.5.20-*.whl` (r31-vintage pin)
   - `openmarquee/debs/hostapd_2.10-24_arm64.deb` (still pinned)
   - `openmarquee/MANIFEST.txt` with `git: <33233c5 or later>`
4. **Manifest inspection**:
   ```
   tar --use-compress-program='zstd -d' -xOf \
       ~/tmp/openmarquee-sd-bundle-v1.0.0.tar.zst openmarquee/MANIFEST.txt
   ```
5. **install.sh smoke-parse**: extract install.sh from the bundle
   and confirm §5b udev rule install step is present:
   ```
   tar --use-compress-program='zstd -d' -xOf \
       ~/tmp/openmarquee-sd-bundle-v1.0.0.tar.zst \
       openmarquee/scripts/install.sh \
       | grep -c "5b\. udev rule"
   # Expected: 1
   ```

### D.4 Burn-test on fresh SD card

Per `[[feedback_vm_test_install_before_burn]]` (if still
applicable): Lima/QEMU arm64 VM workflow before a physical-card
burn. **Caveat:** QEMU cannot simulate USB-WiFi dongle insertion;
the dual-radio code path won't exercise without physical hardware.
QEMU is fine for verifying install.sh runs cleanly + hostapd /
dnsmasq + backend start.

For full coverage:
1. QEMU arm64 VM smoke: install.sh runs to completion; backend
   responds to /healthz; AP comes up.
2. Physical Pi burn: same flow PLUS dongle hot-plug test (per
   `code/docs/dual-radio-shipping-test.md` from code1's r34 — but
   note this doc IS NOT IN THE BUNDLE; QA-pull from the inner
   repo).
3. Capture journalctl output for 5 min; confirm no
   `tick over budget` warns that would worsen the FYS prod
   baseline (the r35 audit's open question).

### D.5 Rollback path

The v0.9.0 bundle is still at
`~/tmp/openmarquee-sd-bundle-v0.9.0.tar.zst` (per session-end
memory). If v1.0.0 bundle rebuild produces a broken artifact:

1. Re-burn from v0.9.0 → device boots to v0.9.0 state.
2. `bash scripts/deploy.sh openmarquee@<sign-hostname>` brings
   to current v1.0+ state via the deploy.sh path (which has the
   r31 + r32 wheel + emoji guards).

Note: **rollback via burn requires physical access** to the SD
card. If the customer is remote, the v1.0.0 bundle MUST be
verified working before any operator-distribution step.

### D.6 r37 dispatch shape

Suggested commit message for code1's r37 (when ready):

```
chore(release): r37 — rebuild SD bundle for v1.0.0 + dual-radio + deploy resilience

Per code2's r36 audit at code/qa/r36-sd-bundle-rebuild-audit-2026-05-31.md.

Inputs:
- origin/main HEAD at build: <SHA>
- renderer cross-rebuild: <renderer/target/.../openmarquee-render mtime>
- ui/dist build: <ui/dist/index.html mtime>
- emoji font: ui/fonts/noto-color-emoji-colrv1.ttf size <bytes>

Bundle artifact:
- Path: ~/tmp/openmarquee-sd-bundle-v1.0.0.tar.zst
- Size: <bytes>
- SHA256: <hash>

Optional structural fix (recommend):
- scripts/build_sd_bundle.sh — add download-emoji-font-colrv1.sh
  call before the ui/ rsync per code2 r36 §C.1 mitigation B.
- ~5 LOC; idempotent.

QEMU burn-test result: <PASS/FAIL>
Physical Pi burn-test result: <PASS/FAIL or DEFERRED>
Verification SHA stamped at <path>.
```

---

## Section E — Bundle metadata + storage

### E.1 Where should the v1.0.0 bundle live?

Current state per session memory:
`~/tmp/openmarquee-sd-bundle-v0.9.0.tar.zst`. This is ephemeral
on macOS — `~/tmp/` is operator-set, NOT system-managed
`/tmp/` which clears. Per memory `[[feedback_no_tmp_use_~_tmp]]`-
adjacent, this is the standing convention.

**For v1.0.0 (the shipping artifact), more durable options:**

a. **Keep `~/tmp/` convention** — fast, matches v0.9.0 precedent,
   but at risk of operator-cleanup loss. Suitable for short-lived
   "v1.0.0 ship handoff" artifact.
b. **Move to `~/project/openmarquee/dist/`** — outside the code
   worktree, alongside outer-repo specs. Persistent. Requires
   `--output` flag override.
c. **Hosted (S3/Cloudflare R2)** — durable for customer
   distribution. Requires distribution-path design (out-of-scope
   of r36; see §F.2).

**Recommendation:** option (b) for v1.0.0. The cost is one
`--output` flag; the gain is a persistent, qarl-managed shipping
artifact location. Customer distribution path is a separate
design question.

### E.2 Bundle naming convention

v0.9.0 used `openmarquee-sd-bundle-v0.9.0.tar.zst`. Continue with
`openmarquee-sd-bundle-v1.0.0.tar.zst`. **Recommend** also
writing a sidecar SHA256 stamp at
`openmarquee-sd-bundle-v1.0.0.tar.zst.sha256` (single-line file
matching `shasum -a 256` output). The bundle script could
optionally produce this automatically (~3 LOC; see §F.6).

### E.3 Customer distribution path

**Out of scope of r36.** The codebase has no `download URL` /
mirror / OCI registry / signature workflow references for the
bundle. Distribution today is presumably:
- qarl ships the bundle to a customer manually (USB stick, scp,
  Signal message, etc.).
- Customer runs `scripts/burn_sd_card.sh` to write the bundle to
  an SD card.

**Flag for separate audit:** the lack of a documented distribution
+ signature path is a v1.x → v2 trajectory concern. Out of
r36 scope but adjacent. See §F.2.

---

## Section F — Open questions for qarl / QA

### F.1 Bundle storage path

Per §E.1: `~/tmp/` (ephemeral) vs `~/project/openmarquee/dist/`
(persistent) vs hosted (S3/R2)?

**Recommendation:** `~/project/openmarquee/dist/` for v1.0.0.

### F.2 Customer distribution path

Per §E.3: no documented distribution mechanism in the codebase.
Manual scp / USB stick / hosted? **Question to qarl:** what's
the customer-distribution UX target — physical-media handoff (USB
stick), signature-verified hosted download, or operator-builds-
locally?

### F.3 Bundle build host environment

**Question to qarl + code1:** does code1 build on Mac
aarch64-cross-compile or on a Linux build host? Affects
reproducibility, AppleDouble sidecar risk
(`COPYFILE_DISABLE=1` is the macOS guard at bundle script line
47), and the speed of `pip download` (Linux build hosts can have
nearer-to-PyPI network paths).

### F.4 FFmpeg-dev .debs in the bundle?

Per §B.4: r31 added 7 `libav*-dev` packages to the pi-gen
00-packages list (covered automatically). The bundle's vendored
.deb manifest doesn't include them. **Question to qarl:** is the
bundle-only install path (without pi-gen image) a supported
operator flow? If yes, ffmpeg-dev .debs should be added (~1-3 MB)
to the manifest. If no (only pi-gen-burn supported), the gap is
fine.

### F.5 r33-style manifest test for the bundle?

Per §B.6 + §C.1: should a parallel
`scripts/tests/test_build_sd_bundle_runtime_asset_manifest.sh`
exist? Same shape as the r33 deploy.sh test. **Question to qarl:**
add as part of r37, or as a separate r38?

### F.6 Bundle sidecar SHA256 stamp

Per §E.2: should `build_sd_bundle.sh` write a sidecar
`.sha256` file automatically? ~3 LOC append at the end of the
script. **Recommendation: yes**, ship in r37 alongside the §C.1
emoji-font guard fix. Both are tiny structural improvements.

### F.7 Bundle build sequence ergonomics

Currently `build_sd_bundle.sh` REQUIRES 3 pre-build steps:
- `setup.sh` (emoji font + Python venv + npm ci)
- `(cd ui && npm run build)` (UI dist)
- `scripts/renderer_cross_build.sh` (aarch64 binary)

**Question to qarl:** should `build_sd_bundle.sh` auto-detect-
and-run these pre-conditions, OR fail loudly if any is missing
(current behavior is mixed — emoji font silently absent, ui/dist
fails loudly, cross-build warns)? If auto-run: ~15 LOC + ~3-5
min added to the bundle build. If fail-loud-everywhere: ~5 LOC
+ no time added. **Recommendation:** fail-loud-everywhere with
diagnostic, including for emoji font (the §C.1 mitigation B
fix).

---

## Hand-off shape

1. **qarl reviews this audit** + answers F.1-F.7. Especially
   F.2 (distribution UX) since that scopes the broader question.
2. **Code1's r37 dispatch** applies §D.1-D.5 + the §C.1
   mitigation B emoji-font guard fix + (optionally) §F.5 +
   §F.6 + §F.7.
3. **QA verifies** the rebuild on QEMU + (if physical hardware
   available) a physical Pi burn.
4. **The new bundle** lands at the §F.1-chosen path.
5. **Subsequent customer-distribution dispatch** (separate from
   r37) handles §F.2 distribution UX.

---

## Out-of-scope items flagged for follow-up

- **Customer distribution path** (§E.3 + §F.2). Lack of
  documented distribution flow is a separate audit.
- **Sidecar SHA256 stamp** + **bundle-side manifest test**
  (§F.5 + §F.6) — small structural improvements, can fold into
  r37 or split out.
- **Build host environment standardization** (§F.3). Docker
  container? Specific Linux distro? Out of r36 scope.
- **FFmpeg-dev .deb addition** to the bundle manifest (§F.4) —
  conditional on §F.2's customer-distribution decision.
- **install.sh pre-flight checklist via build_sd_bundle.sh
  auto-run** (§F.7) — ergonomics, not correctness.

— jimmy:openmarquee-code2 (lane: code2 bundle-audit recommendation)
