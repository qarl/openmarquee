# V1 spec-delta — 2026-05-14

Coverage audit cross-referencing the canonical spec docs against the
actually-shipped surface at HEAD. Triggered per the QA charter
(`feedback_qa_audit_cumulative_coverage`) before stamping the
2026-05-14 arc complete.

Tonight's flight stamped a lot complete in one push:

- Phase 7 slice 4 + slice 4 followups (transitions)
- V4L2 H.264 decode pieces 1–4 (perf-feature-complete)
- SD-burn flow + Mac single-command burn
- AP/NM coexistence fixes (task #99)
- Wifi station-mode applier (nmcli rewrite + rescan polish)
- Backend test-pollution fix (task #94, MockTransport injection)

This audit ran read-only across six arcs; output is a gap list with
spec + code citations, severity rating, and recommended-fix
complexity.

## §1 Scope

Audited arcs and canonical references:

| Arc | Canonical | Implementation |
|-----|-----------|----------------|
| Phase 7 IPC sidecar + wire protocol | `docs/renderer-rewrite-plan-rust.md` + `docs/renderer-rewrite-requirements.md` | `backend/openmarquee/rendering/rust_renderer.py` + `renderer/src/ipc_main.rs` |
| V4L2 H.264 decode | `docs/v4l2-decode.md` | `renderer/src/v4l2.rs` + `renderer/src/mp4_demux.rs` + `renderer/src/hdmi.rs` + `renderer/src/hdmi_logic.rs` + `renderer/src/ipc_main.rs` |
| SD-burn flow | `docs/sd-burn.md` | `scripts/build_sd_bundle.sh` + `scripts/stage_sd_card.sh` + `scripts/burn_sd_card.sh` + `scripts/install.sh` + `system/*.sh` + cloud-init |
| AP-mode + station wifi | `system/README.md` | `system/openmarquee-ap0.service` + `system/openmarquee-ap0-setup.sh` + `system/hostapd.conf` + `system/dnsmasq.conf` + `system/openmarquee-firstboot.sh` + `backend/openmarquee/wifi_station.py` + `system/openmarquee-sudoers` |
| Settings UI ↔ backend | `backend/openmarquee/settings.py` (Pydantic schema as ground truth) | `backend/openmarquee/api_settings.py` + `ui/src/settings*.js` |
| As-built doc currency | `docs/phase-7-as-built-2026-05-14.md` (refreshed `65bfacd`) | git log `65bfacd..HEAD` |

**Note on canonical reference**: the dispatch cited `docs/SYSTEM_SPEC.md
§10` for Phase 7, but no `SYSTEM_SPEC.md` exists in the repo. Audit
substituted `docs/renderer-rewrite-plan-rust.md` (the live forward-
looking spec) + `docs/renderer-rewrite-requirements.md` (the contract
surface).

## §2 Per-arc findings

### 2.1 Phase 7 IPC + sidecar — *cleanest arc*

The 7-op IPC contract (Open / BeginSlide / Advance / BeginTransition /
Capture / Reconfigure / Close) is fully wired both sides. All 4 error
classes (`RustRendererSubprocessError`, `RustRendererRespawnedError`,
`RustRendererUnsupportedSlideError`,
`RustRendererUnsupportedTransitionError`) defined + tested. Robustness
contract (reconnect bounded retry 3-in-60s, watchdog 1Hz,
AutoFallbackRenderer one-way swap with Respawned-first catch ordering)
matches spec exactly.

One spec drift (already documented as known-and-acknowledged in the
as-built §3):

- **Reconfigure intentionally returns `"Reconfigure not yet
  implemented (slice e)"`** at `renderer/src/ipc_main.rs:960`. Not a
  surprise; pinned by test at `:1176`. Documented at
  `docs/phase-7-as-built-2026-05-14.md:138`.

One real gap (P2):

- **VideoSlide Capture path emits `"video slides TBD"` marker** while
  the paint_slide validator now accepts Video. The substring matcher
  in `rust_renderer.py:226-239` catches both because it shares one
  unsupported-marker tuple. Already flagged in as-built doc §3 at
  `docs/phase-7-as-built-2026-05-14.md:190-196`. Code:
  `renderer/src/ipc_main.rs:648,718` for the still-emitting Capture
  side. Fix is a Capture-side validator split + UnsupportedSlideError
  promotion. ~30 LOC.

### 2.2 V4L2 H.264 decode — *spec is staler than code*

All 6 pieces (1, 2a, 2b, 3a-f, 4a-f) shipped. Code is operationally
complete (1080p sub-33ms p99 on both MMAP and DMABUF paths per piece
4f profile). The canonical doc was written for piece 1 in early
April and has not been refreshed for pieces 3 + 4. Five doc-drift
gaps; one quantization gap that's a real code claim to validate.

P1 gaps (the doc drift):

- **DMA-BUF path entirely undocumented in `docs/v4l2-decode.md`.**
  Pieces 4a–f shipped EXPBUF + EGLImage import + samplerExternalOES
  +  paint helper branch; doc still says piece 4 is "TBD/future" at
  `docs/v4l2-decode.md:118-124`. Code anchors:
  `renderer/src/v4l2.rs:900-920` (REQBUFS=MMAP + EXPBUF post-mmap),
  `renderer/src/hdmi.rs:7149-7250` (EGLImage import + external-OES
  bind), `renderer/src/hdmi_logic.rs` (`FS_NV12_DMABUF_TO_RGB`).
- **`OPENMARQUEE_RENDERER_DMABUF` env var gate not in spec.**
  Production-default-flip is qarl-eyeball-gated per as-built §7;
  the env var itself is undocumented in `docs/v4l2-decode.md`. Code:
  `renderer/src/ipc_main.rs:237-242`.
- **REQBUFS=MMAP + EXPBUF post-mmap pattern undocumented.** Piece
  4a-fix (`634eae2`) is the canonical "EXPBUF requires REQBUFS=MMAP,
  not DMABUF" learning; future maintainer reading
  `docs/v4l2-decode.md:51-60` will not know this. Code:
  `renderer/src/v4l2.rs:900-920` (in-code comments cover it; spec
  doesn't).
- **`V4L2_CID_QUANTIZATION` control not set.** Spec mentions the
  full/limited-range BT.601 matrix choice at
  `docs/v4l2-decode.md:146-175`. Code at
  `renderer/src/ipc_main.rs:250-270` calls `set_capture_format` but
  never sets the quantization control; the `FS_NV12_TO_RGB` shader
  hardcodes limited-range coefficients. Fine when CAPTURE always
  emits limited-range (typical hardware default), but a
  format-driven verification (assert + fail-loud, or set explicitly)
  would close the latent risk.

P2:

- **Profile gate `OPENMARQUEE_FIRSTFRAME_PROFILE=1` not in spec.**
  Piece 4f shipped a diagnostic instrumentation that's gated off by
  default. Spec has no mention. Code:
  `renderer/src/hdmi.rs:2733-2750`.

### 2.3 SD-burn flow — *one real doc drift + one internal contradiction*

End-to-end flow works: `build_sd_bundle.sh` → bundle, `burn_sd_card.sh`
→ flash + stage, cloud-init → first boot → `install.sh` → systemd
units come up → ap0 broadcasts. Per-device password generation lives
in `system/openmarquee-firstboot.sh` and rotates the ssid + passphrase
on first boot.

P1:

- **README claims SSID rotation goes to `openMarquee-XXXX` from MAC**
  at `system/README.md:167-170`. Actual rotation in
  `system/openmarquee-firstboot.sh:114` uses `device_id` (the
  `MySign<N>` derivation), not MAC-last-4. Two-way drift — doc
  description outdated, AND `system/hostapd.conf:32` ships the
  cold-boot default `openMarquee-SETUP` which never matches either
  spec sentence. The `docs/sd-burn.md` flow at `:116` correctly
  says `MySign<N>` is what an operator sees post-firstboot.
- **`system/openmarquee-sudoers` internal contradiction.** Header at
  line 7 says "exactly two nmcli subcommands"; line 17 then says
  "Anything beyond these four invocations should NOT be added here".
  File contains exactly 2 grants at lines 28-29. The "four" is stale
  comment from an earlier draft (likely when the spec called for
  `connection up`, `connection down`, `device wifi connect`,
  `connection delete`). Pure doc cleanup — code is the right shape
  per qarl's "narrow as possible" rule.

### 2.4 AP-mode + station-mode wifi — *one stale spec section*

AP brings up cleanly (post-`68727de` unmask + ordering fix). Station
mode applier landed on nmcli (`6ecd1a2`) + polish (`0575572`). Sudoers
narrow. AP/NM coexistence verified per task #99.

P2:

- **README's "Settings → wpa_supplicant template-out" Phase-7 open
  item is stale** at `system/README.md:172-179`. The feature
  SHIPPED, but as `backend/openmarquee/wifi_station.py` via `nmcli`
  rather than `wpa_supplicant@wlan0`. The doc still references the
  superseded approach. Either remove the item (since it shipped) or
  rewrite it as "Settings → nmcli connection apply" so a future
  maintainer doesn't re-implement the wpa_supplicant version.
- **README has no AP/NM coexistence note** at `system/README.md:47-72`
  (the architecture section). The `Before=NetworkManager.service`
  ordering trick from `system/openmarquee-ap0.service:12-13` is
  load-bearing for factory-fresh-boot reliability but isn't called
  out anywhere a future maintainer would find it. As-built §6 has
  it; system/README doesn't.

### 2.5 Settings UI ↔ backend — *clean by intent*

19 fields in `backend/openmarquee/settings.py`. UI surfaces 16 of
them. The 3 not-surfaced are intentional:

- `flock_sync_enabled` (`settings.py:95-103`) — kill switch managed
  by infra, not operator
- `ui_first_run_seen` (`settings.py:105-114`) — transient state flag
  flipped at welcome-screen dismiss
- `schema_version` (`settings.py:86`) — on-disk migration marker

Defaults parity verified on a sample (`display_width: 1920`,
`brightness: 80`, `wifi_ap_enabled: true`). PATCH paths for the 3
secret fields (`wifi_password`, `wifi_station_password`,
`tailscale_auth_key`) all wired at `api_settings.py:179-481`.

**No gaps.** The 3 omitted fields are by-design.

### 2.6 As-built doc currency — *clean*

`docs/phase-7-as-built-2026-05-14.md` refreshed at `65bfacd`. Commits
after: `52aa2c2` (test-pollution MockTransport injection, test-only).
No production architecture drift; the test commit doesn't change any
claim in the doc.

**No gaps.**

## §3 Consolidated gap list

| # | Severity | Arc | Description | Fix complexity |
|---|----------|-----|-------------|----------------|
| 1 | P1 | V4L2 | DMA-BUF path entirely missing from `docs/v4l2-decode.md` (pieces 4a-f) | M (doc rewrite, ~60-100 lines) |
| 2 | P1 | V4L2 | `OPENMARQUEE_RENDERER_DMABUF` env var gate undocumented | S (one paragraph in §wire-format) |
| 3 | P1 | V4L2 | REQBUFS=MMAP + post-mmap EXPBUF pattern undocumented (piece 4a-fix learning) | S (one paragraph in §pixel-format) |
| 4 | P1 | V4L2 | `V4L2_CID_QUANTIZATION` control not set; shader hardcodes limited-range. Validation gap, not a known bug | S (code add: 1 ioctl in `set_capture_format`; or fail-loud assertion ~20 LOC) |
| 5 | P1 | SD-burn | SSID rotation: doc says `openMarquee-XXXX` from MAC; code uses `MySign<N>` from device_id; `hostapd.conf` ships `openMarquee-SETUP` (cold-boot only). Three-way drift | S (doc edit + cross-reference, ~10 LOC of doc) |
| 6 | P1 | AP/wifi | `system/openmarquee-sudoers` internal contradiction: "two" in header + "four" in body, file grants two | S (one-line comment fix) |
| 7 | P2 | Phase 7 | VideoSlide Capture still emits `"video slides TBD"` marker (paint_slide now supports Video) | M (Capture validator split + new UnsupportedSlideError path, ~30 LOC) |
| 8 | P2 | V4L2 | `OPENMARQUEE_FIRSTFRAME_PROFILE` profile gate undocumented in spec | S (one bullet in §diagnostics) |
| 9 | P2 | AP/wifi | `system/README.md` Phase-7 open-items list is stale: "wpa_supplicant template-out" shipped as nmcli; should be rewritten or removed | S (doc edit, ~15 lines) |
| 10 | P2 | AP/wifi | README missing AP/NM coexistence + `Before=NetworkManager.service` note (load-bearing for fresh-boot reliability) | S (one paragraph + cross-ref to as-built §6) |

**Totals: 0 P0 ship-blockers, 6 P1 (mostly doc-staleness + one
validation), 4 P2 (cleanup + non-load-bearing doc gaps).**

P0=0 means the arcs are operationally complete; the work is closing
documentation debt and one validation gap, not unblocking a feature.

## §4 Not gaps — deliberate, ignore on review

These look like gaps on first read but are intentional:

- **`Reconfigure not yet implemented (slice e)`** at
  `renderer/src/ipc_main.rs:960`. Documented in as-built §3;
  acknowledged design deferral.
- **`flock_sync_enabled` / `ui_first_run_seen` / `schema_version`
  not in UI.** Kill switch / transient state / migration marker —
  intentionally non-operator-facing per settings audit.
- **`scripts/_lib.sh` stays 100644.** Sourced-only helper; bash
  sourcing doesn't read the mode bit. Per `e8545bd` commit note.
- **`docs/SYSTEM_SPEC.md` referenced in dispatch but doesn't exist.**
  Substitute is the rewrite-plan-rust.md + requirements.md pair.
- **DELETE-PIL phases 5+6 / phase 4 of the renderer rewrite.** Per
  `feedback` memory, the Rust shader cutover is operationally complete
  but the PIL deletion arc has phases 5+6 deferred until production-
  default-flip. They're queued, not forgotten.
- **`scripts/sidecar_smoke_driver.py` not run per-commit.** Per
  `feedback_no_soak_during_dev`: release-candidate gating only.

## §5 Stale spec docs that need a refresh dispatch

Two docs are stale enough to merit follow-up dispatches:

1. **`docs/v4l2-decode.md`** — pre-dates pieces 4a-f (DMA-BUF
   pathway, EGLImage import, samplerExternalOES, env-var gate, profile
   gate). 4 of the 6 P1 gaps point at it. A refresh dispatch landing a
   §"piece 4" section and a §"diagnostics" section would close all 4
   in one shot. Same shape as the as-built doc refresh.
2. **`system/README.md`** — pre-dates the `nmcli`/`wpa_supplicant`
   swap (`6ecd1a2`) and the AP/NM coexistence work (`68727de` / task
   #99). The "Phase 7 open items" section is misleading; the
   "Architecture" section has no AP/NM ordering note. 3 of the 4 P2
   gaps point at it.

## §6 Deliberate deferrals worth re-evaluating

The QA charter asks: "are any previously-deferred decisions now
ready to unblock?" Findings:

- **DMABUF production-default-flip** — pending qarl eyeball on color
  quality at office. Piece 4f confirmed p99 sub-33ms on both paths;
  not perf-blocked. Re-evaluate at next office-glass session.
- **DELETE-PIL phases 5 + 6** — the PIL software-renderer codebase
  is still in tree behind `OPENMARQUEE_RENDERER=auto` / `mock`. With
  Phase 7 slices 1–4 + V4L2 pieces 1–4 + transitions + rust SD-burn
  all shipped, the architectural blocker for full PIL deletion is
  the qarl-eyeball-gated rust-sidecar default-flip (slice 5). Once
  that lands, DELETE-PIL phases 5+6 unblock. Recommend qarl confirm
  next office-glass session covers both slice-5 and DELETE-PIL
  unlock.
- **Atlas SB 29.5 vc4 ceiling** — task #279 (pending). The Atlas SB
  sanity-capture concluded SB bake is NOT the bottleneck; the ceiling
  lives elsewhere. No new data tonight to change the picture; stays
  qarl-direct.

No surprise unblocks.

## §7 Confidence

| Arc | Confidence | What was read end-to-end | What was skimmed |
|-----|------------|--------------------------|------------------|
| 2.1 Phase 7 IPC | High | IPC enum defs both sides, 4 error classes, robustness anchors | full op-by-op wire format |
| 2.2 V4L2 | High | spec doc in full (~176 lines), v4l2.rs open/format/allocate, ipc_main `prime_video_decoder`, hdmi paint path, hdmi_logic shaders | mp4_demux internals |
| 2.3 SD-burn | High | spec doc + 3 stage/build/burn scripts, firstboot.sh, ap0.service, sudoers, hostapd.conf | wifi-prefill code, backend tests |
| 2.4 AP-mode wifi | High | system/README.md, ap0.service, sudoers, firstboot SSID rotation, recent commits 6ecd1a2/0575572/68727de | end-to-end live-Pi verification (didn't re-run task #99 smoke) |
| 2.5 Settings | High | full settings.js (~1000 LOC), settings.py schema, api_settings.py PATCH paths, spot-checked 3 defaults | UI behavior at runtime — only static cross-reference |
| 2.6 As-built currency | High | full `git log 65bfacd..HEAD`, doc §§1–6 vs HEAD code | — |

**Overall confidence: high.** All audit arcs had clear canonical
references (or, for settings, a clearly-typed Pydantic schema serving
as ground truth). The DELTAS surfaced are concrete (file:line on both
sides). No P0 ship-blocker fell out. The arc is defensibly complete
with known-and-tracked gaps.

## Subagent LGTM

Audit was done across 4 parallel Explore subagents, one per arc
cluster (Phase 7 IPC + as-built / V4L2 / SD-burn + AP-wifi /
Settings). All 4 returned with high-confidence per-arc findings.
Synthesized into this doc with three subagent claims re-verified
against the actual files: sudoers contradiction (confirmed at
`system/openmarquee-sudoers:7,17,28-29`), hostapd.conf SSID default
(confirmed at `system/hostapd.conf:32`), and README's wpa_supplicant
Phase-7 open item (confirmed at `system/README.md:172-179`). One
subagent finding (V4L2 device path "P0") was downgraded to "Not a
gap" — `/dev/video10` matches between spec + code; the subagent
flagged absence of an env-var override surface but no such surface
is spec-required.

LGTM for the synthesized output.
