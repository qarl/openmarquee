# r30 — install.sh pip-failure diagnosis + fix recommendation (2026-05-31)

**Author:** jimmy:openmarquee-code2
**Static analysis only.** No SSH, no prod touch, no
`pip install --verbose` trace yet. Handoff doc — whoever owns the
deploy lane (code1 by default) picks up Phase 1 trace capture +
applies the chosen fix.

**Scope of this doc:** static read of `scripts/install.sh` +
`backend/requirements.lock` at code2 HEAD; cross-reference with
r29 (`5d2a9e9` 2026-05-31, install.sh section reorder); ranked
list of root-cause candidates with smallest-blast-radius fix per
candidate.

## What r29 (5d2a9e9) already did

- Reordered `scripts/install.sh` so §3 (systemd units), §3a
  (chmod), §3b (Rust renderer binary cp + atomic-rename) run
  BEFORE §2 (pip).
- Result: pip failure no longer wedges the renderer-binary
  deploy. Operator can `systemctl restart openmarquee-backend`
  to swap in the new binary even if pip rc=1.
- The EXIT trap still snapshots state on rc≠0; pip failure
  produces `EXIT_FAILED_RC_1_LINE_<N>` in
  `/var/log/openmarquee-install-xtrace.log`.

**r29 does NOT touch the pip command itself.** It only changes
the section ordering so the pip failure has a smaller blast
radius. r30 is the orthogonal pip-side fix.

## How install.sh's §2 pip stage works (post-r29)

`scripts/install.sh` lines 282-324. Three pip commands run in
sequence:

```bash
PIP_OFFLINE_FLAGS=()
WHEELS_DIR="${OPT_DIR}/wheels"
if [ -d "$WHEELS_DIR" ] && [ -n "$(ls -A "$WHEELS_DIR" 2>/dev/null)" ]; then
    PIP_OFFLINE_FLAGS=(--no-index "--find-links=$WHEELS_DIR" --no-build-isolation)
fi
# (1) bootstrap setuptools + wheel into venv
run "${VENV_DIR}/bin/pip" install --upgrade \
    "${PIP_OFFLINE_FLAGS[@]}" setuptools wheel
# (2) install pinned requirements.lock
if [ -f "${OPT_DIR}/backend/requirements.lock" ]; then
    run "${VENV_DIR}/bin/pip" install --upgrade \
        "${PIP_OFFLINE_FLAGS[@]}" -r "${OPT_DIR}/backend/requirements.lock"
fi
# (3) editable install of the backend package
run "${VENV_DIR}/bin/pip" install --upgrade \
    "${PIP_OFFLINE_FLAGS[@]}" -e "${OPT_DIR}/backend"
```

Behavior:
- **If `/opt/openmarquee/wheels` exists with content** (the
  factory-fresh SD-burn path): pip runs fully offline with
  `--no-index --find-links` — no PyPI fetch.
- **If wheels dir missing or empty** (dev redeploys): pip falls
  back to online PyPI — network + source-build available.

The EXIT trap at line 244 fires on any rc≠0 of the three pip
commands. `EXIT_FAILED_RC_1_LINE_<line>` records which line
failed. The dispatch's "EXIT_FAILED_RC_1_LINE_1" in the FYS
xtrace likely points at one of (1) / (2) / (3) — Phase 1 trace
narrows which.

## High-risk packages in `backend/requirements.lock`

C-extension + multi-MB packages most likely to mis-resolve or
fail a source build on aarch64:

| Package | Version | Risk class | Notes |
|---------|---------|-----------|-------|
| `av` | 16.1.0 | **HIGH** | PyAV / FFmpeg binding. Wheel exists for cp313 + manylinux2014_aarch64 BUT not always for every Python micro; falls back to source build needing FFmpeg dev headers. Session-end memory + dispatch context both flag `av==16.1.0` as a known hang/failure candidate. |
| `cryptography` | 48.0.0 | MED | manylinux wheels usually available; falls back to Rust + OpenSSL source build (multi-minute, large RAM) if not. |
| `aiortc` | 1.14.0 | MED | Depends on `av` + `cryptography` + `pylibsrtp`. If any sub-dep fails, aiortc install fails downstream. |
| `pylibsrtp` | 1.0.0 | MED | C-extension wrapping libsrtp; needs libsrtp-dev to build from source. |
| `argon2-cffi-bindings` | 25.1.0 | LOW-MED | Has manylinux wheels for cp313 + aarch64 in recent versions. |
| `pydantic-core` | 2.46.4 | LOW | Rust-source under the hood but very well wheel-supplied for aarch64 cp313. |
| `numpy` | 2.4.6 | LOW | Modern numpy ships aarch64 wheels reliably. |
| `pillow` | 12.2.0 | LOW | Reliable aarch64 manylinux wheels. |

**Top suspect: `av==16.1.0`.** PyAV is the cited candidate
from session memory + dispatch. The wheel-vs-source-build
question is the most likely Phase 1 finding.

## Root-cause candidates (ranked)

### A — Wheels-dir lag on redeploy (HIGH likelihood for FYS-specific case)

**Hypothesis:** FYS was provisioned from the v0.9.0 SD bundle
which shipped 42 aarch64 wheels at `/opt/openmarquee/wheels`.
The v1.0.0 redeploy via `scripts/deploy.sh` sent the new
`requirements.lock` (with `av==16.1.0`) but **did NOT refresh
the wheels/ directory.** Wheels still pin v0.9.0's `av==16.0.x`
(or whatever shipped). pip with `--no-index --find-links`
can't find a wheel matching `==16.1.0`, fails with "No matching
distribution found for av==16.1.0."

**Smallest-blast-radius fix:** make `scripts/deploy.sh` either:
1. Refresh `/opt/openmarquee/wheels/` from a newly-built wheel
   set on each deploy, OR
2. Clear `/opt/openmarquee/wheels/` so install.sh falls back
   to online PyPI (less factory-fresh-clean, but no stale-wheel
   mismatch).

**LOC:** ~10-20 in `scripts/deploy.sh`.
**Risk:** option 1 needs the deploy host to have aarch64 wheel
download capability (pip + --platform flags, same as the SD
bundle build). Option 2 is one-liner but loses the offline-
install guarantee on the next install.sh run if network is down.

**Phase 1 confirmation signal:** trace shows
`ERROR: No matching distribution found for av==16.1.0 (...)`
when pip is invoked with `--no-index --find-links=/opt/openmarquee/wheels`.

### B — Source-build fallback hits missing FFmpeg dev headers (MEDIUM, if A is the trigger AND deploy.sh cleared wheels OR online path active)

**Hypothesis:** Online path activated (no wheels OR wheels
cleared). pip tries to source-build `av==16.1.0` because no
aarch64 wheel matches the system's Python ABI exactly. PyAV's
setup.py needs FFmpeg dev headers (libavcodec-dev, libavformat-
dev, libavutil-dev, libavfilter-dev) which aren't installed
post-fresh-OS. Build fails.

**Smallest-blast-radius fix:** add the FFmpeg dev packages to
the apt-get install in install.sh's §1 (prerequisites section,
before the venv create). Or include them in the cloud-init
bootstrap.

**LOC:** ~5 (apt-get line) in install.sh OR cloud-init.
**Risk:** adds ~30 MB to fresh-Pi apt footprint. Not a problem
on FYS (Pi 4) but worth noting.

**Phase 1 confirmation signal:** trace shows
`ERROR: Failed building wheel for av` AND
`error: ‘libavcodec/avcodec.h’ no such file or directory`.

### C — Resolver-conflict between two pinned deps (LOW likelihood for the FYS case, given pip-compile already vetted the lockfile)

**Hypothesis:** A transitive constraint slipped past
pip-compile's resolution + pip's runtime resolver rejects the
graph. Unlikely because pip-compile is what generated the
lockfile + it's been deployed successfully before.

**Smallest-blast-radius fix:** re-lock via `pip-compile
--upgrade` on the dev side, commit the new lockfile, redeploy.

**LOC:** lockfile delta only.

**Phase 1 confirmation signal:** trace shows
`ERROR: Cannot install X==a.b.c and Y==d.e.f because these
package versions have conflicting dependencies.`

### D — Network flake during PyPI fetch (LOW likelihood for repeating-failure-class)

**Hypothesis:** PyPI fetch timed out / 503'd. Would be transient
and resolve on retry.

**Smallest-blast-radius fix:** add `--retries 5 --timeout 60`
flags to the three pip commands in install.sh §2.

**LOC:** ~3 in install.sh.

**Phase 1 confirmation signal:** trace shows
`Connection refused / 503 / Timeout` to `pypi.org` or
`files.pythonhosted.org`. Should be obvious if it's the cause.

## Recommended sequence for whoever owns the lane

1. **code1 SSH to FYS prod** and capture verbose trace:
   ```
   ssh qarl@fireplacesign
   sudo -u openmarquee /opt/openmarquee/venv/bin/pip install \
       --verbose -r /opt/openmarquee/backend/requirements.lock \
       2>&1 | tee /tmp/pip-r30-trace.log
   ```

2. **Check `/opt/openmarquee/wheels/` state before that command:**
   ```
   ls -la /opt/openmarquee/wheels/ | head -10
   ls /opt/openmarquee/wheels/av-*.whl 2>&1     # which av wheel(s)?
   ```

3. **Send trace tail + wheels-dir listing to QA / me for cross-
   check against the A/B/C/D ranking above.**

4. **Apply the matching fix from §"Root-cause candidates."** The
   A/B fixes are deploy.sh / install.sh edits (code1's lane). C
   needs pip-compile + lockfile commit (code2 lane is fine for
   the lockfile, BUT the regen should happen on a host with the
   right Python + matching constraints).

5. **Verify on FYS** by re-running install.sh and confirming pip
   section completes cleanly. (Code1's lane.)

## What I checked + what I did NOT

| Check | Status |
|-------|--------|
| Read `scripts/install.sh` §2 pip block end-to-end | ✅ |
| Read `backend/requirements.lock` for risk surfaces | ✅ |
| Cross-reference r29's reorder + verify no §2 dependency moved | ✅ |
| Identify candidate root causes (A/B/C/D) + rank | ✅ |
| Run pip on FYS prod to capture actual trace | ❌ (out of lane) |
| SSH to FYS to inspect `/opt/openmarquee/wheels/` state | ❌ (out of lane) |
| Re-run install.sh on FYS prod | ❌ (out of lane) |
| Walk `scripts/deploy.sh` to confirm wheels-refresh behavior | partial — read header + section names; full audit would confirm/refute Candidate A |
| Check pyproject.toml for `[tool.pip-compile]` constraints | partial — read pyproject.toml top section |

## Best-guess fix shape if a single fix needs to ship today

If qarl wants a one-commit fix without waiting for the trace:
**land Candidate B preemptively** — add FFmpeg dev headers to
install.sh's apt-get list. Even if the immediate root cause
turns out to be A (stale wheels), B's apt-get add is a defense-
in-depth that closes the source-build fallback path. Adds ~30
MB to fresh-Pi apt footprint; that's acceptable on Pi 4 and Pi
Zero 2 W both ship 16+ GB SD cards. Doesn't help if Candidate D
(network flake) is the actual cause, but A/B are higher-ranked.

LOC: ~5 in install.sh's §1, single commit. Cherry-pickable
across branches.

## Out-of-scope items flagged for follow-up

- **`scripts/deploy.sh` wheels-refresh behavior audit** — read
  the script to confirm/refute whether redeploys clear or refresh
  the wheels dir. Answer determines whether Candidate A is
  feasible-fix-needed or already-handled.
- **SD-bundle regen for v1.0.0** — if FYS's wheels are v0.9.0
  vintage, even after fixing this deploy a future fresh-burn
  would still need v1.0.0 wheels. Suggest a v1.0.0 SD bundle
  rebuild dispatch in a future round.
- **Pip-compile constraint review** — check if pyproject.toml
  pins `av` to a range that's narrower than necessary; loosening
  could improve wheel-match probability across Python micro
  versions.

## Self-correction note

Original r30 dispatch from QA asked me to SSH + run pip on FYS
prod. I surfaced the lane-boundary concern (per
[[feedback_dispatch_text_is_the_contract]] discipline); QA
agreed + redirected to this recommendation-only path. The
analysis above is static-only; whoever does Phase 1 trace
capture has the real diagnosis in hand within ~5 minutes of
SSHing in.

---

Filed by jimmy:openmarquee-code2 2026-05-31.
