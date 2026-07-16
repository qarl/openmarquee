# Deploy-Hygiene Audit — openMarquee (2026-07-16)

**Author:** Jimmy-openmarquee-code
**Type:** Discovery report (Phase 1 of the always-sync-`system/` work stream)
**Trigger:** PR #89 (`1ac5811`, netctl exit-semantics) root-caused a failed
`openmarquee-netctl@` unit on JasonsSign1 to **deploy lag on `system/` files** —
not backend/renderer. `../IMPLEMENTATION_PLAN.md` §9 queued a deploy-pipeline fix
("sync `system/` on every backend deploy"). Before writing that fix we owe a
taxonomy of *every* file category that can drift between a full-image burn and a
targeted deploy — because the naive framing turns out to be incomplete.

> **This is a discovery report. No implementation changes are proposed as landed
> here.** The scope recommendations in §7 are for admin review; Phase 2 is gated
> on GO.

---

## 0. TL;DR — the finding that changes the scope

The §9 item reads: *"redeploy that updates backend ALSO syncs `system/`."* Taken
literally, that fix is **already shipped** — `scripts/deploy.sh` (the *full*
deploy) rsyncs `system/` **and** runs `install.sh`, which installs every staged
`system/` file to its live path. If every deploy went through `deploy.sh`,
`system/` would never lag.

The drift exists because **there are two deploy paths, and the one used for
routine live-sign pushes carries none of `system/`:**

| Path | What it moves | Runs `install.sh`? | Syncs `system/`? |
|---|---|---|---|
| `code/scripts/deploy.sh` | backend + ui + scripts + **system/** + images + wheels + renderer binary | **yes** (`deploy.sh:300`) | **yes** (`deploy.sh:180`) |
| `qa/deploy-to-sign.sh` | **one binary** → `/usr/local/bin/openmarquee-render` | **no** | **no** |

`qa/deploy-to-sign.sh` is the *canonical QA sign deploy* (its own header, line 2)
and exists specifically to be **low-impact**: the sign overloads while rendering,
so file transfers during playback starve the WiFi and drop the link (qarl, twice,
per `deploy-to-sign.sh:4-6`). That is exactly why routine renderer iteration
prefers it over the heavy `deploy.sh`. The cost of that choice: **`system/` only
advances on a full `deploy.sh` run or a fresh image burn**, both of which are
infrequent on a fielded sign. Result: the 06-02..06-17 stale-helper cohort QA
found on JasonsSign1.

Two secondary findings compound it:

- **Renderer-binary staging/live split (revert hazard).** `deploy-to-sign.sh`
  writes the *live* binary (`/usr/local/bin/openmarquee-render`) directly but
  never updates the *staging* copy (`/opt/openmarquee/bin/openmarquee-render`)
  that `install.sh` treats as source-of-truth. A later `install.sh` run
  (via `deploy.sh`) atomically copies staging→live (`install.sh:404`) and would
  **revert** a `deploy-to-sign.sh` binary to the older staged one. This is the
  "different md5 in each path" drift caught during the PR #89 redeploy verify.

- **Two `system/` files `install.sh` never installs at all** (`openmarquee-wifi-watchdog`,
  `wpa_supplicant-openmarquee.conf`) — these drift regardless of which deploy
  path runs, because *no* automated path installs them (§5).

**Scope implication:** the Phase-2 fix is **not** "add a `system/` rsync to the
deploy script" (that rsync already exists). It targets the *binary-only path* and
the *binary staging split*. Options in §7.

---

## 1. Deploy topology — the four ways bits reach a sign

```
                    ┌─────────────────────────────────────────────┐
                    │  repo:  code/system/, code/scripts/,         │
                    │         code/backend/, code/ui/, code/images/│
                    └───────────────┬─────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────────┐
        │                           │                               │
   (A) FRESH IMAGE            (B) FULL DEPLOY              (C) BINARY DEPLOY
   pi-gen stages +            scripts/deploy.sh           qa/deploy-to-sign.sh
   scripts/build_sd_bundle.sh                             (+ renderer_cross_build.sh
        │                           │                        produces the artifact)
        │  stages system/ →         │  rsync system/ →           │
        │  /opt/openmarquee/system/ │  /opt/openmarquee/system/  │  rsync ONE binary
        │  + boot cfg at BUILD time │  + rsync backend/ui/images/ │  → /tmp → mv →
        │                           │  + wheels + renderer bin    │  /usr/local/bin/
        │                           │                             │  openmarquee-render
        ▼                           ▼                             ▼
   first-boot runs            ssh runs                     (NO install.sh,
   install.sh                 install.sh (deploy.sh:300)    NO system/ sync)
        │                           │
        └─────────────┬─────────────┘
                      ▼
        ┌─────────────────────────────────────────┐
        │  install.sh — THE COMMON INSTALL FUNNEL  │
        │  reads /opt/openmarquee/system/  →        │
        │  writes live paths (/etc, /usr/local/…)   │
        └─────────────────────────────────────────┘
```

- **(A) Fresh image** — `images/openmarquee/stage-openmarquee/*` (pi-gen) +
  `scripts/build_sd_bundle.sh`. `build_sd_bundle.sh:316-322` copies `system/`
  into the bundle at `/opt/openmarquee/system/`; boot config
  (`config.txt`/`cmdline.txt`) is patched at **build time** by pi-gen stage
  `02-boot-config` (`boot-config-lib.sh`). First boot runs `install.sh`.
- **(B) Full deploy** — `scripts/deploy.sh <host>`. Rsyncs backend
  (`:92`), renderer binary→`/opt/.../bin` (`:138`), ui (`:144`), scripts
  (`:174`), **system/** (`:180`), images (`:191`), wheels (`:274`), then runs
  `install.sh` on the remote (`:300`).
- **(C) Binary deploy** — `qa/deploy-to-sign.sh <host> <binary>`. Stops backend
  (`:49`), rsyncs one file to `/tmp` over the quiet link (`:54`), installs it to
  `/usr/local/bin/openmarquee-render` with a `.pre-deploy` backup (`:62`),
  restarts (`:66`). **Does not touch `system/` and does not run `install.sh`.**
- **Renderer cross-build** — `scripts/renderer_cross_build.sh` is *not* a deploy
  path; it produces the aarch64 `openmarquee-render` artifact that (B) and (C)
  consume. It writes only into the local `renderer/target/…` tree.

### The install funnel (`install.sh`)

Both (A) and (B) converge on `install.sh`, whose contract is:
**read from `/opt/openmarquee/system/` (`OPT_DIR=/opt/openmarquee`,
`install.sh:84`) → install to live paths.** It is idempotent and safe to re-run.
So `/opt/openmarquee/system/` is a **staging tree**, distinct from the live
locations. `deploy.sh` populating staging is necessary but not sufficient — the
`install.sh` run is what promotes staging→live. **Path (C) does neither.**

---

## 2. Category taxonomy

For each category: repo path → on-sign live path → what installs it → synced by
full `deploy.sh` (B)? → synced by binary `deploy-to-sign.sh` (C)? → drift risk.

| # | Category | Repo path | On-sign live path | Installer | (B) full | (C) binary | Drift risk |
|---|---|---|---|---|---|---|---|
| 1 | systemd units (`.service`/`.timer`/`.socket`) | `system/*.service` etc. | `/etc/systemd/system/` | `install.sh` §3 loop (`:306-325`), mtime `-nt` update; `firstboot.service` installed separately at `:776-779` | ✅ | ❌ | **HIGH** — netctl@ already bit |
| 2 | netctl privileged helpers | `system/openmarquee-netctl`, `-netctl-daemon` | `/usr/local/sbin/` | `install.sh` (`:824-833`), `install -m0755` | ✅ | ❌ | **HIGH** — confirmed stale on JasonsSign1 |
| 3 | netctl socket + template | `system/openmarquee-netctl.socket`, `-netctl@.service` | `/etc/systemd/system/` | `install.sh` (`:836-842`) | ✅ | ❌ | HIGH (couples to #2) |
| 4 | `.sh` helper scripts | `system/*.sh` (ap0-setup, firstboot, tailscale, cma-watchdog, best-wifi, wifi-powersave-off, boot-gesture, stability-promoter) | **run in place** from `/opt/openmarquee/system/` (ExecStart targets there) | `deploy.sh` rsync + `install.sh` `chmod +x` loop (`:336-345`) | ✅ | ❌ | MED |
| 5 | renderer binary | (cross-build artifact) | `/usr/local/bin/openmarquee-render` + `/opt/.../bin` staging | `install.sh` §3b atomic-rename (`:380-409`) **or** `deploy-to-sign.sh` direct (`:62`) | ✅ | ✅ live-only | **MED** — staging/live split → revert hazard (§4) |
| 6 | hostapd / dnsmasq / NM configs | `system/hostapd.conf`, `dnsmasq.conf`, `dnsmasq.service.d/*`, `NetworkManager-openmarquee-unmanaged.conf` | `/etc/hostapd/`, `/etc/`, `…/dnsmasq.service.d/`, `/etc/NetworkManager/…` | `install.sh` — hostapd `:556`, dnsmasq `:565`, dnsmasq drop-in `:578`, NM `:603` | ✅ | ❌ | MED |
| 7 | udev rules | `system/99-openmarquee-usb-wlan.rules` | `/etc/udev/rules.d/` | `install.sh` (`:630`) | ✅ | ❌ | LOW-MED (dual-radio boards) |
| 8 | sudoers fragment | `system/openmarquee-sudoers` | `/etc/sudoers.d/` | `install.sh` §7b (`:866`) | ✅ | ❌ | MED — privilege scope; skew = failed wifi/settings action |
| 9 | tmpfiles / swappiness / cma-default | `system/openmarquee-tmpfiles.conf`, `99-openmarquee-swappiness.conf`, `openmarquee-cma-watchdog.default` | `/etc/tmpfiles.d/`, `/etc/sysctl.d/`, `/etc/default/` | `install.sh` (`:854`,`:974`,`:979`) | ✅ | ❌ | LOW-MED |
| 10 | avahi (mDNS) | `system/avahi/avahi-daemon.conf`, `openmarquee.service` | `/etc/avahi/`, `/etc/avahi/services/` | `install.sh` (`:1273-1311`) | ✅ | ❌ | MED — ties to netctl `avahi-write-and-restart` |
| 11 | service-unit drop-ins (log-to-file) | `system/{firstboot,ap0,hostapd}.service.d/log-to-file.conf` | `/etc/systemd/system/*.service.d/` | `install.sh` loop (`:789-796`) | ✅ | ❌ | LOW |
| 12 | boot config | patched in place (not a file copy) | `/boot[/firmware]/config.txt`, `cmdline.txt` | pi-gen `02-boot-config` (build) **+** `install.sh` §7d (`:1137-1146`) reusing `images/.../boot-config-lib.sh` | ✅ | ❌ | LOW — rarely changes; both callers share one idempotent lib |
| 13 | cron.d (daily restart) | `system/openmarquee-daily-restart.cron` | `/etc/cron.d/` | `install.sh` (`:429`) | ✅ | ❌ | LOW |
| 14 | cron.d (wifi watchdog) | `system/openmarquee-wifi-watchdog` | `/etc/cron.d/` (if deployed) | **NONE** — not referenced by `install.sh` | ❌ | ❌ | **UNRESOLVED** (§5) |
| 15 | legacy station template | `system/wpa_supplicant-openmarquee.conf` | `/etc/wpa_supplicant/` (manual) | **NONE** — legacy fallback per `system/README.md:29`; manual install steps at `README.md:298,304-305` | ❌ | ❌ | N/A — legacy fallback (§5) |
| 16 | backend (Python) | `backend/` | `/opt/openmarquee/backend/` | `deploy.sh` rsync (`:92`) | ✅ | ❌ | (the known path — this is what "backend deploy" means) |
| 17 | UI bundle | `ui/` (built `dist/`) | `/opt/openmarquee/ui/` | `deploy.sh` rsync (`:144`) | ✅ | ❌ | (covered by B) |
| 18 | Python wheels | (cross-downloaded) | `/opt/openmarquee/wheels/` | `deploy.sh` (`:274`) | ✅ | ❌ | (covered by B) |
| 19 | polkit rules | — | — | — | — | — | **N/A — none in repo** (verified: no polkit files under `system/`, `scripts/`, `images/`) |

Legend: ✅ synced by that path · ❌ not synced · rows 1-15 are the drift surface,
16-18 are the already-understood app-layer paths, 19 is a listed-but-absent
category confirmed inapplicable.

---

## 3. Why the skew is silent (mechanism)

`systemctl --failed` cannot distinguish "the daemon is stale and rejected a new
subcommand" from any other unit failure. The skew only surfaces when a **new
backend subcommand meets an old deployed daemon**:

- Backend `name_actuator` calls `avahi-write-and-restart` (added `cafcf49`,
  2026-07-03). Deployed daemon predating that commit has no such entry in its
  allowlist → rejects → (pre-#89) `exit 1` → `openmarquee-netctl@<inst>.service`
  lands in `failed`. (PR #89 changed the *exit contract* so a handled client
  error exits 0 — but that is a **symptom fix**; the underlying stale daemon is
  still missing the subcommand and the feature — avahi hostname write — is still
  non-functional until `system/` is resynced.)
- Reveal-secret subcommands (added ~2026-07-14 per `../IMPLEMENTATION_PLAN.md`
  §9; not independently datable from this repo's history) have the same latent skew.

So the health signal is structurally blind to component skew — which is why a
**deploy-time / boot-time compatibility gate** is on the option list (§7, option
b) alongside the always-sync fix.

---

## 4. Renderer-binary staging/live split (secondary hazard, detail)

Two on-sign copies of the same binary:

| Copy | Path | Role | Written by |
|---|---|---|---|
| **live** | `/usr/local/bin/openmarquee-render` | what systemd runs | `install.sh` §3b (from staging) **and** `deploy-to-sign.sh:62` (direct) |
| **staging** | `/opt/openmarquee/bin/openmarquee-render` | `install.sh` source-of-truth | `deploy.sh:138` only |

`install.sh:380-409` copies `staging → live.new` then `mv -f` (atomic rename).
`deploy-to-sign.sh` writes **live** directly and never refreshes **staging**.
Consequence:

1. `deploy-to-sign.sh` puts binary vN at live. Staging still holds v(N-k).
2. Someone later runs `deploy.sh` for an unrelated reason (or just to refresh
   `system/`). Its `install.sh` copies **staging v(N-k) → live**, silently
   reverting the renderer to an older build. No error; only a visual/behavioral
   regression.

This is the "renderer binary had a different md5 in each path" observation from
the PR #89 redeploy verify. Cheap to fix (have `deploy-to-sign.sh` also refresh
staging, or have it be a thin wrapper that keeps both in lockstep), but it must
be part of the Phase-2 scope conversation because it is the same
merged-vs-deployed failure class.

---

## 5. Per-file resolution of the two truly-orphaned `system/` files

A naive "is this basename referenced in `install.sh`?" scan flags ~20 files as
missing, but most are installed via **loops** (units §3 `install.sh:306-325`;
`.sh` helpers §3a `:336-345`) whose bodies reference `${unit}`/`${sh_helper}`,
not literal basenames. After resolving the loops, exactly **two** `system/`
files have **no** installer:

1. **`system/openmarquee-wifi-watchdog`** — a `cron.d` fragment (fires the WiFi
   watchdog every 30 s; would live at `/etc/cron.d/openmarquee-wifi-watchdog`).
   Last changed 2026-05-24. Not referenced by `install.sh`, not staged by any
   pi-gen substage. There is a *separate* `scripts/wifi-watchdog.sh` and a
   systemd `openmarquee-best-wifi.{service,timer}` path that *is* installed —
   this cron.d fragment appears to be a superseded predecessor. **Needs a
   decision:** either (a) it is dead and should be deleted from the repo, or
   (b) it is a live mechanism that install.sh must start installing. Recommend
   confirming against the running JasonsSign1 crontab (read-only probe, QA's
   lane) before either action.

2. **`system/wpa_supplicant-openmarquee.conf`** — legacy station-mode template
   (`/etc/wpa_supplicant/wpa_supplicant-wlan0.conf`). `system/README.md:29`
   marks it *"legacy station-mode template (kept for fallback / pre-trixie
   boards). Pi OS Lite trixie uses NetworkManager + nmcli instead."* Install is
   documented as **manual** (the wpa_supplicant-specific `scp` + `mv` steps are
   at `README.md:298,304-305`). **Resolution:** by-design
   not-auto-deployed; a legacy fallback, not a drift risk. Leave as-is (or move
   under a `system/legacy/` subdir to make its status self-evident — optional
   housekeeping, not required).

(These two are the "4 heuristic-MISSING candidates" from QA's audit collapsing to
2 real cases once the install loops are resolved; the other flagged names —
`openmarquee-mini.service`, `openmarquee-stability-promoter.service`, the
`*-best-wifi.*`, `*-boot-gesture*`, `*-cma-watchdog*`, `*-wifi-powersave-off*`
units and their `.sh` helpers — are all installed via the §3/§3a loops and are
**not** orphaned.)

---

## 6. A note on `install.sh`'s unit-update comparator (minor)

The §3 unit loop updates an installed unit only when
`already_done "$SRC" -nt "$DST"` — i.e. **source mtime newer than installed
mtime** (`install.sh:317`). `rsync -a` preserves source mtime, and a `git`
checkout stamps changed files with the checkout time, so the happy path works.
But it is mtime-based, not content-based: a revert that brings back an
older-mtime version, or clock skew between build host and sign, can cause a
changed file to be **skipped**. A content-hash comparison (or unconditional
`install` of the small config set) would be strictly more robust. Flagging as a
candidate hardening item for Phase 2, not a confirmed active bug.

---

## 7. Scope options for Phase 2 (for admin review — NOT decided here)

The taxonomy shows the §9 "always-sync-`system/`" item is really **three**
distinct fixes. Recommending we land the first, discuss the rest:

- **(P2-a) Close the binary-only gap — the core fix.** The routine live-sign
  path (`qa/deploy-to-sign.sh`) is the actual source of `system/` drift.
  Sub-options:
  - **(a1)** Make `deploy-to-sign.sh` also rsync `system/` + run `install.sh`.
    *Simplest, but* reintroduces the heavy transfer + full-provision the script
    exists to avoid, and couples every binary swap to a full `install.sh` run
    (StartLimit/backend-bounce surface). Likely too heavy for the "quiet link
    during playback" constraint.
  - **(a2) (recommended)** Add a lightweight **`--sync-system`** mode to
    `deploy-to-sign.sh` (or a small companion `sync-system-to-sign.sh`): stop
    backend → rsync `system/` over the quiet link → run *only* the `install.sh`
    system-file install sections (skip venv/wheels/UI) → `daemon-reload` →
    restart. Keeps the low-impact posture, makes `system/` sync a first-class,
    on-demand operation, and is what QA's Karl-gated quiescence-window pass can
    invoke as durable tooling rather than a one-shot.
  - **(a3)** Leave deploy paths as-is; add the **compatibility gate** below.
    Weaker (detects skew, doesn't fix it) but cheap and complementary.

- **(P2-b) Compatibility gate (option (b) from §9).** A deploy-time and/or
  boot-time check that the deployed netctl daemon's allowlist covers every
  subcommand the current backend calls; **fail loud** on skew. Complements (a2):
  (a2) prevents the drift, (b) catches it if a binary-only push slips through.
  Cheap; recommend landing alongside (a2).

- **(P2-c) Fix the renderer-binary staging/live split (§4).** Have
  `deploy-to-sign.sh` also refresh `/opt/openmarquee/bin/openmarquee-render` so a
  later `install.sh` cannot revert a hand-deployed binary. ~1-line change; low
  risk; recommend folding into (a2).

- **(P2-d) Resolve the two orphaned files (§5).** Delete `wifi-watchdog` if
  dead (confirm via read-only crontab probe first) or add it to `install.sh`;
  optionally relocate the legacy `wpa_supplicant` template under
  `system/legacy/`. Small; do after the crontab confirmation.

- **(P2-e) Optional hardening — content-hash unit comparator (§6).**
  Nice-to-have; not required for handover de-risk.

**Discipline for whatever we land:** preserve the FYS 6-step backup discipline
(backup + md5 + tar-verify + extract-verify + sacred-review + stop-services) with
per-file granular rollback sidecars; add a post-sync assertion that
`systemctl --failed` shows **0** units in the `openmarquee-netctl@` family (or a
strict subset that pre-existed); fail-loud-and-clean on any sync error to keep
the StartLimitBurst-clean posture.

---

## 8. Appendix — evidence index

| Claim | Evidence |
|---|---|
| `deploy.sh` rsyncs `system/` | `scripts/deploy.sh:179-182` |
| `deploy.sh` runs `install.sh` | `scripts/deploy.sh:300` |
| `deploy-to-sign.sh` is binary-only, no `install.sh`, no `system/` | `qa/deploy-to-sign.sh` whole file; `install.sh` refs = 0 |
| `deploy-to-sign.sh` exists for the quiet-link constraint | `qa/deploy-to-sign.sh:4-6` |
| `install.sh` reads staging `/opt/openmarquee/system/` | `OPT_DIR=/opt/openmarquee` `install.sh:84`; `deploy.sh REMOTE_ROOT=/opt/openmarquee` `:53` |
| systemd unit install loop + mtime update | `install.sh:306-325` |
| `.sh` helpers `chmod +x` in place (run from staging) | `install.sh:336-345` |
| netctl helpers → `/usr/local/sbin/` | `install.sh:824-833` |
| renderer binary atomic-rename staging→live | `install.sh:380-409` |
| `deploy-to-sign.sh` writes live binary directly | `qa/deploy-to-sign.sh:62` |
| `deploy.sh` writes staging binary only | `scripts/deploy.sh:138` |
| boot config patched at build + redeploy via shared lib | pi-gen `02-boot-config/02-run.sh`; `install.sh:1137-1146` |
| SD bundle stages `system/` | `scripts/build_sd_bundle.sh:316-322` |
| netctl skew root cause (avahi 07-03, reveal 07-14) | PR #89 `1ac5811` commit body; `../IMPLEMENTATION_PLAN.md` §9 |
| two orphaned `system/` files | repo-wide grep; `system/README.md:29,287-293` |
| no polkit files in repo | grep `polkit` over `system/ scripts/ images/` → empty |

---

*Discovery complete. Phase 2 is gated on admin GO. Recommended landing set:
(P2-a2) + (P2-b) + (P2-c); (P2-d) after a read-only crontab confirmation;
(P2-e) optional.*
