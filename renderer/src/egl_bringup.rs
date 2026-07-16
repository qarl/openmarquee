//! Shared GBM+EGL bring-up / tear-down primitive — Linux-only.
//!
//! Extracted from `hdmi.rs`'s `with_egl_session` (was inline at
//! lines 926-1015 + 1404-1418 pre-PR-#B0.5) because a second real
//! caller (Colorlight's `HeadlessGpuCompositor`, sibling
//! `colorlight_gpu_compositor.rs`) needs the exact same dance
//! minus the DRM modeset/page-flip (design doc §5 "Pattern A").
//! Two callers = the abstraction is warranted; the anti-list on
//! `hdmi.rs` naturally lifts for this refactor.
//!
//! ## Behavior-preserving contract
//!
//! `hdmi.rs`'s side of this refactor is a pure structural extract:
//! the calls made here, in this order, with these arguments, are
//! byte-identical to what `hdmi.rs` did inline pre-refactor. The
//! only new thing is the parameterization (`EglBringUpSpec`), and
//! `hdmi.rs` uses the `for_drm_scanout()` preset which is exactly
//! its previous hard-coded values (Argb8888 + SCANOUT|RENDERING +
//! swap_interval(0)). Any observable behavior change on the paint
//! hot path would be a regression.
//!
//! ## Two shapes for two callers
//!
//! - `bring_up_egl` / `tear_down_egl` — the paired functions.
//!   `hdmi.rs` uses these directly to match its existing
//!   `?`-then-warn-on-teardown-Err control flow (the errors are
//!   swallowed so the original cause propagates via `work_result?`).
//! - `HeadlessEgl` — a RAII wrapper for the Colorlight headless
//!   compositor path. Owns the handles; Drop calls `tear_down_egl`.
//!   Rust-idiomatic for a compositor lifecycle that shouldn't leak
//!   EGL resources across an `?`.

use anyhow::{anyhow, bail, Context, Result};
use gbm::{AsRaw, BufferObjectFlags, Format as GbmFormat};
use khronos_egl as egl;
use std::ffi::c_void;
use std::fs::File;
use std::ptr;

use crate::Card;

/// Parameterization of the bring-up.  Constructed via the
/// `for_drm_scanout` / `for_headless_compositor` presets; two-call-site
/// scoping so a future caller has to opt into the preset it wants.
pub struct EglBringUpSpec {
    /// GBM surface width in pixels.  For DRM scanout: panel-native
    /// physical dims (post-rotation).  For headless compositor: card-
    /// native dims (128 for Colorlight).
    pub width: u32,
    /// GBM surface height in pixels.
    pub height: u32,
    /// GBM pixel format.  DRM scanout uses `Argb8888` (the vc4
    /// scanout format).  Headless uses `Xrgb8888` (no alpha needed
    /// for the readback path; matches `docs/colorlight-egl-spike-2026-07-12.md`).
    pub format: GbmFormat,
    /// BO usage flags.  DRM scanout needs `SCANOUT | RENDERING` (the
    /// BO must be scan-outable by the display controller).  Headless
    /// needs only `RENDERING` (never gets scanned out).
    pub flags: BufferObjectFlags,
    /// `eglSwapInterval` to apply after `make_current`, or `None` to
    /// leave at the driver default.  `hdmi.rs` sets 0 (async page-
    /// flip pairing, see the extensive comment at the previous
    /// hdmi.rs:997-1006).  Headless compositor doesn't swap-buffers
    /// (`glReadPixels` before any swap), so `None` is fine.
    pub swap_interval: Option<i32>,
}

impl EglBringUpSpec {
    /// The `hdmi.rs`-preserving preset — parameters byte-identical
    /// to the inline values `with_egl_session` used pre-refactor.
    /// Callers pass the phys_w × phys_h that `hdmi.rs` computed
    /// from the DRM mode + rotation.
    pub fn for_drm_scanout(width: u32, height: u32) -> Self {
        Self {
            width,
            height,
            format: GbmFormat::Argb8888,
            flags: BufferObjectFlags::SCANOUT | BufferObjectFlags::RENDERING,
            swap_interval: Some(0),
        }
    }

    /// The Colorlight headless preset (design-doc §5 Pattern A):
    /// XRGB8888 (no alpha), RENDERING-only (no scanout permission
    /// needed), no swap_interval (compositor uses `glReadPixels`,
    /// not `eglSwapBuffers`).
    pub fn for_headless_compositor(width: u32, height: u32) -> Self {
        Self {
            width,
            height,
            format: GbmFormat::Xrgb8888,
            flags: BufferObjectFlags::RENDERING,
            swap_interval: None,
        }
    }
}

/// The loose set of EGL/GBM handles a successful `bring_up_egl`
/// yields.  Fields are `pub` so callers (hdmi.rs's `with_egl_session`
/// via disjoint field borrows) can pass them into their downstream
/// data structures without going through accessors.
///
/// Callers are responsible for calling `tear_down_egl(&handles)` at
/// end-of-life (or wrap in `HeadlessEgl` for RAII).  Dropping this
/// struct WITHOUT calling `tear_down_egl` leaks the EGL display /
/// context / surface (the GBM surface + device DO drop cleanly via
/// their own RAII).
pub struct EglHandles {
    pub egl_lib: egl::DynamicInstance<egl::EGL1_5>,
    pub display: egl::Display,
    pub config: egl::Config,
    pub context: egl::Context,
    pub egl_surface: egl::Surface,
    pub gbm_surface: gbm::Surface<()>,
    /// Kept alive for the lifetime of the surface (prefixed `_` to
    /// document intent — this field is only used via Drop).  Backing
    /// type is `File` (from `card.0.try_clone()`), NOT `Card` — the
    /// `gbm::Device::new` call consumes the file descriptor directly.
    pub _gbm_dev: gbm::Device<File>,
    pub gl: glow::Context,
}

/// Run the full GBM+EGL bring-up dance.  Byte-identical to the
/// pre-refactor `hdmi.rs:926-1015` inline sequence when called with
/// `EglBringUpSpec::for_drm_scanout(phys_w, phys_h)`.
///
/// Logs `EGL <major>.<minor>` + the EGL extensions list to stderr
/// on success (same instrumentation `hdmi.rs` emitted).  Warn-on-
/// Err for the swap_interval call so a driver that refuses it
/// (uncommon) doesn't fail the whole bring-up.
pub fn bring_up_egl(spec: &EglBringUpSpec, card: &Card) -> Result<EglHandles> {
    let gbm_dev = gbm::Device::new(card.0.try_clone().context("clone DRM fd for GBM")?)
        .context("gbm_create_device failed")?;
    let gbm_dev_ptr: *mut c_void = gbm_dev.as_raw() as *mut c_void;
    if gbm_dev_ptr.is_null() {
        bail!("gbm_device raw pointer is null");
    }
    // FYS bug 5 (pre-refactor comment preserved) — the scanout
    // buffer is PANEL-NATIVE, so DRM callers pass phys_w × phys_h
    // (never the rotation-swapped logical dims).  Headless callers
    // pass card-native dims directly.
    let mut gbm_surface = gbm_dev
        .create_surface::<()>(spec.width, spec.height, spec.format, spec.flags)
        .context("gbm_surface_create failed")?;

    let egl_lib = unsafe {
        egl::DynamicInstance::<egl::EGL1_5>::load_required().map_err(|e| {
            anyhow!("eglDynamicInstance::<EGL1_5>::load_required failed: {e:?}")
        })?
    };
    let display = unsafe {
        egl_lib
            .get_display(gbm_dev_ptr as egl::NativeDisplayType)
            .ok_or_else(|| anyhow!("eglGetDisplay returned NO_DISPLAY"))?
    };
    let (egl_major, egl_minor) = egl_lib
        .initialize(display)
        .map_err(|e| anyhow!("eglInitialize failed: {e:?}"))?;
    eprintln!("EGL {}.{}", egl_major, egl_minor);
    // Flip-race fix D (pre-refactor comment preserved): startup log
    // of advertised EGL extensions so QA can confirm
    // EGL_KHR_fence_sync is present (rotate_scanout_3_deep uses it
    // for the per-slot per-buffer sync; if absent the rotation
    // degenerates to no-sync and the snap-back race may reappear).
    match egl_lib.query_string(Some(display), egl::EXTENSIONS) {
        Ok(cs) => eprintln!(
            "EGL_EXTENSIONS: {}",
            cs.to_str().unwrap_or("<non-utf8>"),
        ),
        Err(e) => eprintln!("warn: eglQueryString(EGL_EXTENSIONS) failed: {e:?}"),
    }

    egl_lib
        .bind_api(egl::OPENGL_ES_API)
        .map_err(|e| anyhow!("eglBindAPI(GLES) failed: {e:?}"))?;
    let cfg_attribs = [
        egl::SURFACE_TYPE,
        egl::WINDOW_BIT,
        egl::RED_SIZE,
        8,
        egl::GREEN_SIZE,
        8,
        egl::BLUE_SIZE,
        8,
        egl::ALPHA_SIZE,
        8,
        egl::RENDERABLE_TYPE,
        egl::OPENGL_ES2_BIT,
        egl::NONE,
    ];
    let config = egl_lib
        .choose_first_config(display, &cfg_attribs)
        .map_err(|e| anyhow!("eglChooseConfig failed: {e:?}"))?
        .ok_or_else(|| anyhow!("no EGL config matched ARGB8888 + GLES2"))?;
    let ctx_attribs = [egl::CONTEXT_CLIENT_VERSION, 2, egl::NONE];
    let context = egl_lib
        .create_context(display, config, None, &ctx_attribs)
        .map_err(|e| anyhow!("eglCreateContext failed: {e:?}"))?;
    let egl_surface = unsafe {
        let raw_surface = gbm_surface.as_raw_mut() as *mut c_void;
        egl_lib
            .create_window_surface(display, config, raw_surface, None)
            .map_err(|e| anyhow!("eglCreateWindowSurface failed: {e:?}"))?
    };
    egl_lib
        .make_current(display, Some(egl_surface), Some(egl_surface), Some(context))
        .map_err(|e| anyhow!("eglMakeCurrent failed: {e:?}"))?;

    if let Some(interval) = spec.swap_interval {
        // eglSwapInterval (pre-refactor comment preserved): pair
        // with DRM_MODE_PAGE_FLIP_ASYNC so eglSwapBuffers does NOT
        // wait for vsync to release a back buffer.  Default EGL
        // behaviour is interval=1 (vsync-lock), which on vc4 + GBM
        // means 16.67ms quantization on swap returns even though
        // the kernel page-flip is async.  Setting interval=0 hands
        // buffer management to the kernel/driver.
        //
        // Warn-string preservation: for `interval == 0` we keep the
        // exact pre-refactor phrase ("defaulting to vsync-locked
        // swap") so QA journal-scraping / log-search patterns keyed
        // to that literal don't silently miss.  Any other interval
        // gets a generic message.
        if let Err(e) = egl_lib.swap_interval(display, interval) {
            if interval == 0 {
                eprintln!(
                    "warn: eglSwapInterval(0) failed: {e:?}; defaulting to vsync-locked swap"
                );
            } else {
                eprintln!(
                    "warn: eglSwapInterval({interval}) failed: {e:?}; defaulting to driver default"
                );
            }
        }
    }

    let gl = unsafe {
        glow::Context::from_loader_function(|name| {
            egl_lib
                .get_proc_address(name)
                .map(|fp| fp as *const _)
                .unwrap_or(ptr::null())
        })
    };

    Ok(EglHandles {
        egl_lib,
        display,
        config,
        context,
        egl_surface,
        gbm_surface,
        _gbm_dev: gbm_dev,
        gl,
    })
}

/// Reverse of `bring_up_egl` — releases the EGL context / surface /
/// display in the correct order.  Warn-on-Err at each step so the
/// original cause (from the caller's `?`-propagated error) survives.
///
/// The `gbm_surface` and `_gbm_dev` inside `EglHandles` clean up via
/// their own RAII Drop when `EglHandles` itself goes out of scope
/// after this call.
pub fn tear_down_egl(handles: &EglHandles) {
    if let Err(e) = handles
        .egl_lib
        .make_current(handles.display, None, None, None)
    {
        eprintln!("warn: eglMakeCurrent(unbind): {e:?}");
    }
    if let Err(e) = handles
        .egl_lib
        .destroy_context(handles.display, handles.context)
    {
        eprintln!("warn: eglDestroyContext: {e:?}");
    }
    if let Err(e) = handles
        .egl_lib
        .destroy_surface(handles.display, handles.egl_surface)
    {
        eprintln!("warn: eglDestroySurface: {e:?}");
    }
    if let Err(e) = handles.egl_lib.terminate(handles.display) {
        eprintln!("warn: eglTerminate: {e:?}");
    }
}

/// RAII wrapper for the Colorlight headless-compositor path.
///
/// Owns an `EglHandles` and tears it down on Drop.  Callers can
/// `?`-propagate errors during compositor use without leaking the
/// EGL context / surface / display — Drop unwinds them cleanly.
///
/// `hdmi.rs` deliberately does NOT use this wrapper — its existing
/// control flow interleaves `work_result?` with the teardown so
/// the original error propagates via `work_result?` at the very
/// end.  Using `HeadlessEgl` there would change that shape.
///
/// Structural invariant preserved from hdmi.rs's behavior: the
/// bring-down order is (make_current(None) → destroy_context →
/// destroy_surface → terminate).  Any callers using `HeadlessEgl`
/// get the same order automatically via `tear_down_egl` in Drop.
pub struct HeadlessEgl {
    inner: EglHandles,
}

impl HeadlessEgl {
    /// Bring up a fresh headless EGL context per `spec`.  Failure
    /// leaves nothing to clean up (bring-up is all-or-nothing per
    /// `bring_up_egl`'s `?` chain).
    pub fn new(spec: &EglBringUpSpec, card: &Card) -> Result<Self> {
        bring_up_egl(spec, card).map(|inner| Self { inner })
    }

    /// Read-only view of the underlying handles.  Callers that need
    /// to hand parts to `glow::Context` / `egl` calls take individual
    /// field references (`&self.handles().gl`, etc.).
    #[inline]
    pub fn handles(&self) -> &EglHandles {
        &self.inner
    }
}

impl Drop for HeadlessEgl {
    fn drop(&mut self) {
        tear_down_egl(&self.inner);
    }
}
