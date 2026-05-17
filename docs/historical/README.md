# Historical design docs

This directory holds design docs for subsystems that no longer ship.
They describe the Python rendering subsystem deleted in the
DELETE-PIL purge (2026-05-17, commits 67cea75 .. adea339). The
Rust IPC sidecar at `renderer/` is now the only production
rendering path; these docs are kept as historical reference for
the design decisions behind the architecture they replaced.

Contents:

- `shader-compositor.md` — EGL/GLES2 fragment-shader transitions
  via DRM peer-renderer fd-sharing. Implementation:
  `backend/openmarquee/rendering/shader_compositor.py` (deleted in
  commit 70a4865).
- `multi-plane-gpu-compositor.md` — DRM atomic multi-plane scanout
  with HVS overlay planes for animated text layers. Implementation:
  `backend/openmarquee/rendering/{gpu_compositor,drm_kms}.py`
  (deleted in commits b320dfd + 53b5c30).
- `renderer-rewrite-plan.md` — original phase-7 rewrite plan,
  superseded by `renderer-rewrite-plan-rust.md` (still active).
- `renderer-rewrite-spike-data.md` — Pi Zero 2 W shader-compositor
  feasibility spike measurements.
- `renderer-rewrite-phase1-status.md` — phase-1 progress log.

For the active rendering architecture, see:

- `../renderer-rewrite-plan-rust.md` — the Rust IPC sidecar plan.
- `../renderer-memory-budget.md` — runtime memory accounting.
- `../text-layer-motion-spec.md` — motion math contract (still
  authoritative; the Rust path implements this).
