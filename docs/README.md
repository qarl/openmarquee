# docs

Contributor-facing documentation. Grouped by topic; status flags
on the individual files mark draft vs. shipped vs. superseded.

## Renderer rewrite

The Python rendering subsystem was deleted in the DELETE-PIL purge
(2026-05-17). The Rust IPC sidecar at `renderer/` is now the only
production rendering path.

- [`renderer-rewrite-requirements.md`](renderer-rewrite-requirements.md) —
  what the renderer has to do (perf budget, render contract,
  protocol surface).
- [`renderer-rewrite-plan-rust.md`](renderer-rewrite-plan-rust.md) —
  Rust binary + IPC sidecar plan. **DONE** as of the DELETE-PIL
  purge.
- [`phase-7-as-built-2026-05-14.md`](phase-7-as-built-2026-05-14.md) —
  as-built phase 7 status snapshot.
- [`historical/`](historical/) — superseded design docs for the
  Python rendering subsystem (shader compositor, multi-plane DRM
  compositor, phase 1 spike data + plan). Kept for the design
  rationale they capture; no longer reflect shipped code.

## Renderer features

- [`text-layer-motion-spec.md`](text-layer-motion-spec.md) — motion
  effects (ticker / breathe / pulse / bounce / shake / blink) on
  per-text-layer basis. Still authoritative; the Rust path
  implements this contract.
- [`renderer-memory-budget.md`](renderer-memory-budget.md) —
  spec §4 hard memory limits + soak-test gate thresholds.
- [`v4l2-decode.md`](v4l2-decode.md) — H.264 hardware decode via
  V4L2 + NV12 GPU compose path. Implementation in
  `renderer/src/v4l2.rs` + `renderer/src/mp4_demux.rs` (HW-decode
  arc commit `5a35a7d`; NV12-greater-than-2048 hardening
  `81a594a`).

## Feature design + decision records

- [`STREAM_VLC_PROPOSAL.md`](STREAM_VLC_PROPOSAL.md) — design
  proposal for the network-stream feature (takeover + playlist-slide
  modes). Shipped end-to-end 2026-05-19/21 across the 9-slice build
  + stream-generalize + hardening arcs; doc carries an AS-BUILT
  preamble noting the renames + multi-protocol generalization +
  the deltas the doc body still describes in RTSP-only terms.

## Recons + investigations

- [`black-flash-at-transition-boundaries-recon.md`](black-flash-at-transition-boundaries-recon.md)
  — backlog-item #3 recon. Code-side fix shipped 2026-05-09
  (`7c605cce`); on-glass visual confirmation still pending.
- [`motion-phase-discontinuity-recon.md`](motion-phase-discontinuity-recon.md)
  — backlog-item #2 recon. Code-side fix shipped 2026-05-09 →
  2026-05-16 (`7417ae0` / `413efca` / `fff3ab8`); glass-time A/B
  on FYS still pending.

## Operator guides

- [`factory-fresh.md`](factory-fresh.md) — first-boot offline-AP
  flow + wheel-vendoring + cloud-init offline-pip behavior.
- [`sd-burn.md`](sd-burn.md) — SD-card flashing walkthrough
  (`build_sd_bundle.sh` / `stage_sd_card.sh` / `burn_sd_card.sh`).

## Phase B flock

- [`phase-b-flock-scope.md`](phase-b-flock-scope.md) — design
  for the cross-device content sync surface (self-card, peer
  discovery, manifest exchange, pull worker).

## Top-level

The user-facing story lives in the top-level
[`../README.md`](../README.md). The product brief +
implementation plan + system spec live in the outer repo
(`DESIGN_BRIEF.md`, `IMPLEMENTATION_PLAN.md`, `SYSTEM_SPEC.md`).

The originally-planned `hardware.md` / `dev-setup.md` /
`architecture.md` / `building-the-image.md` never landed under
those names. Hardware BOM lives in `DESIGN_BRIEF.md §2`; dev
setup lives in the top-level `README.md` + `code/scripts/`;
architecture is covered by `SYSTEM_SPEC.md` + the per-module
docstrings; SD-card image work is gated behind Phase 9 of
`IMPLEMENTATION_PLAN.md`.
