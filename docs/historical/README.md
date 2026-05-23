# Historical design docs

This directory holds design docs for subsystems or pre-implementation
recons that no longer reflect shipped reality. The Python rendering
subsystem was deleted in the DELETE-PIL purge (2026-05-17, commits
67cea75 .. adea339); the Rust IPC sidecar at `renderer/` is now the
only production rendering path. The pre-implementation recon for the
SDF + emoji text rendering arcs is also kept here as a record of the
design choices that didn't ship verbatim. All docs in this directory
are reference-only — for the active architecture, read the canonical
SYSTEM_SPEC (outer-repo) + the live docs in `..`.

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
- `sdf-text-rendering-recon.md` — pre-implementation recon for the
  SDF text + emoji arcs (2026-05-17, moved here 2026-05-22). The
  SDF arc (slices A–E) shipped 2026-05-17/18 (deploy `6251a9e`);
  the emoji arc (Bug 3 Slices 3A.rev → 3D) shipped 2026-05-19/20
  via runtime COLRv1 vector emoji (`skrifa` + `tiny-skia`,
  `renderer/src/glyph_cache_colr.rs`), superseding the recon's
  §9 + Slice C CBDT-extraction plan. Read for the pre-impl
  reasoning + the assumption flags that became override points;
  read `renderer/src/sdf_atlas*.rs` + `glyph_cache*.rs` +
  `atlas_page.rs` for production.

For the active rendering architecture, see:

- `../renderer-rewrite-plan-rust.md` — the Rust IPC sidecar plan.
- `../renderer-memory-budget.md` — runtime memory accounting.
- `../text-layer-motion-spec.md` — motion math contract (still
  authoritative; the Rust path implements this).
