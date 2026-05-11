# docs

Contributor-facing documentation. Grouped by topic; status flags
on the individual files mark draft vs. shipped vs. superseded.

## Renderer rewrite

The Python software renderer was superseded by a Rust binary
during the 2026-05-06 pivot. These five documents trace the
decision + the live spec:

- [`renderer-rewrite-requirements.md`](renderer-rewrite-requirements.md) —
  what the new renderer has to do (perf budget, render contract,
  protocol surface).
- [`renderer-rewrite-plan-rust.md`](renderer-rewrite-plan-rust.md) —
  current implementation plan (Rust binary + IPC sidecar).
- [`renderer-rewrite-plan.md`](renderer-rewrite-plan.md) —
  **SUPERSEDED**; the earlier Python-renderer plan, kept as
  historical record.
- [`renderer-rewrite-spike-data.md`](renderer-rewrite-spike-data.md) —
  spike numbers + Pi-side bench results that fed the Rust pivot.
- [`renderer-rewrite-phase1-status.md`](renderer-rewrite-phase1-status.md) —
  Phase 1 progress log against `-plan-rust.md`.

## Compositor design

Two parallel design tracks shipped here, each with a "shipped"
status pointing at the production module:

- [`multi-plane-gpu-compositor.md`](multi-plane-gpu-compositor.md) —
  vc4 multi-plane DRM atomic compositor (HVS overlay planes for
  motion + auto layers). Shipped 2026-05-02/03; see
  [`gpu_compositor.py`](../backend/openmarquee/rendering/gpu_compositor.py).
- [`shader-compositor.md`](shader-compositor.md) — EGL+GLES2+dmabuf
  shader compositor design. Single-pass-only per the vc4 bandwidth
  audit; status of implementation tracked in commits +
  `rendering/shader_compositor.py`.

## Renderer features

- [`text-layer-motion-spec.md`](text-layer-motion-spec.md) — motion
  effects (ticker / breathe / pulse / bounce / shake / blink) on
  per-text-layer basis. Shipped 2026-05-02; see
  [`motion.py`](../backend/openmarquee/motion.py).
- [`renderer-memory-budget.md`](renderer-memory-budget.md) —
  spec §4 hard memory limits per renderer mode (CMA / dumb-buffer
  budget, soak-test gate thresholds).

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
