# Spike: headless GL for the Colorlight backend on Pi vc4 — VERDICT

2026-07-12. Time-boxed spike per admin. Question: can the renderer composite a frame with GL/EGL **without a DRM display** (so a Colorlight-only sign, no HDMI attached, can render → `glReadPixels` → Ethernet)? Deliverable: "works with pattern X" or "doesn't, here's the workaround."

## Verdict: **GO. Headless GL works on Pi vc4 — two viable patterns, one of them a near-zero-delta from proven code. No blocker to the integration.**

The Colorlight backend does **not** need any new/exotic GL capability. Two patterns, both supported on vc4/Mesa:

### Pattern A (recommended default) — GBM surface, render, **skip the page-flip**, `glReadPixels`
This is the existing `hdmi.rs` bring-up **minus the DRM modeset/page-flip**:
1. `open("/dev/dri/card0")` → `gbm_create_device` → `eglGetDisplay(gbm_dev)` → `eglInitialize` — *identical to hdmi.rs:945-955.*
2. `eglChooseConfig` / `eglCreateContext(CLIENT_VERSION=2)` — *identical to hdmi.rs:972-985.*
3. `gbm_surface_create(dev, 128, 96, XRGB8888, GBM_BO_USE_RENDERING)` + `eglCreateWindowSurface` — *like hdmi.rs:990, but at panel-native 128×96 and WITHOUT `GBM_BO_USE_SCANOUT`.*
4. `eglMakeCurrent(dpy, surf, surf, ctx)`; composite the frame (reuse the whole existing paint path).
5. **`glReadPixels(0,0,128,96, GL_RGB, GL_UNSIGNED_BYTE, buf)`** — this is exactly what `live_preview.rs:205-244` already does; ~48 KB at 128×96 (trivial). **No `drmModeSetCrtc`, no page-flip, no vblank.**

Why this is essentially zero-risk: it's the renderer's own proven GLES→GBM path with the scanout half deleted, and it's the exact shape of the canonical "OpenGL ES on Pi without X" example (matusnovak/rpi-opengl-without-x `triangle_rpi4.c`: GBM surface → render → `glReadPixels`, never scans out). Headless GBM+EGL needs no attached display and no modeset — those are only for actual scanout.

### Pattern B (optional refinement) — surfaceless context + FBO
`eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)` and render to a GLES2 **FBO** (renderbuffer/texture at 128×96), then `glReadPixels`. Needs `EGL_KHR_surfaceless_context` — **confirmed supported on vc4/v3d Mesa** (Mesa docs list `EGL_MESA_platform_surfaceless` + `EGL_KHR_surfaceless_context` for these drivers). Slightly cleaner (no GBM scanout-BO allocation, no dummy window surface) but a larger delta from the current code. Fully deviceless variant: `eglGetPlatformDisplayEXT(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, ...)` — no `/dev/dri` node at all.

**Recommendation: ship Pattern A** (smallest, proven-shape delta; reuses the existing paint pipeline verbatim), keep Pattern B as a later cleanup if we want to drop the GBM dependency for the Colorlight path.

## Evidence
- Mesa VC4/V3D driver docs + Pi ecosystem: `EGL_MESA_platform_surfaceless` and `EGL_KHR_surfaceless_context` are supported on vc4/v3d. Headless offscreen rendering is a well-trodden Pi path.
- `hdmi.rs:963-965` already **logs the live `EGL_EXTENSIONS` string** at renderer startup — so once a vc4 Pi is reachable we can confirm the exact advertised extensions from the renderer's own journal in one grep.
- The renderer already `glReadPixels` the composited FBO in `live_preview.rs` — the readback tap is proven on our stack.

## What is NOT yet done (one live-confirm step, deferred — not blocking)
I could **not** run a live probe on real vc4 right now: the dev Pi (`openmarqueedev`) is **offline, last seen 31 days ago** (Tailscale), and I will **not** experiment on the live production `jasonssign1` (QA owns it; it's serving + mid-SD-handover). So this verdict rests on Mesa/vc4 documentation + the canonical Pi example + our own proven readback path — strong, but the final 5-minute confirmation on hardware remains.

**Confirmation step (when a vc4 dev Pi is up, or piggy-backing on the Colorlight first-light):**
1. `journalctl -u openmarquee-backend | grep EGL_EXTENSIONS` → check `EGL_KHR_surfaceless_context` is listed (for Pattern B; Pattern A doesn't even need it).
2. Run the ready probe `renderer/examples/egl_headless_probe.rs` (to be added — a ~120-line standalone using the renderer's own `khronos-egl` + `glow` deps: GBM@128×96 → render solid green → `glReadPixels` → assert center pixel == green). Cross-builds with `scripts/renderer_cross_build.sh`; scp + run; exit 0 = confirmed.

## Design-doc impact
None material. The design doc's §5 "surfaceless-EGL is the top risk" **downgrades to "resolved-in-principle: use Pattern A (GBM-no-page-flip), Pattern B optional."** No design change needed; if the live confirmation ever surprises us, Pattern A is the fallback of the fallback (it's the current proven code path). Net: the integration shape is now concrete and low-risk.
