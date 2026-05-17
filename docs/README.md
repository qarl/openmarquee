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
