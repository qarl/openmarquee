//! GBM + EGL bring-up.
//!
//! Mesa Bookworm's libEGL auto-detects the platform from the
//! gbm_device pointer passed to eglGetDisplay, so we do NOT need
//! to hand-bootstrap eglGetPlatformDisplayEXT.
//!
//! `Drop` impls log and best-effort the teardown; never panic.

use anyhow::Result;

#[cfg(target_os = "linux")]
pub use linux::*;

#[cfg(not(target_os = "linux"))]
pub use stub::*;

#[cfg(not(target_os = "linux"))]
mod stub {
    use super::*;
    pub struct Gbm;
    pub struct Egl;
    impl Gbm {
        pub fn new(_: &crate::drm_probe::Card, _w: u32, _h: u32) -> Result<Self> {
            anyhow::bail!("GBM stub: Linux only")
        }
    }
    impl Egl {
        pub fn bring_up(_: &mut Gbm) -> Result<Self> {
            anyhow::bail!("EGL stub: Linux only")
        }
        pub fn swap_buffers(&self) -> Result<()> {
            anyhow::bail!("swap_buffers stub: Linux only")
        }
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use crate::drm_probe::Card;
    use anyhow::{anyhow, Context};
    use gbm::{AsRaw, BufferObjectFlags, Format as GbmFormat};
    use khronos_egl as egl;
    use std::os::fd::{AsFd, BorrowedFd};
    use std::sync::Arc;

    /// drm-rs Card wrapper for gbm::Device (which wants
    /// `AsFd + 'static`). Holds an `Arc<Card>` so the gbm device
    /// keeps the DRM fd alive for as long as it lives.
    pub struct CardFd(pub Arc<Card>);
    impl AsFd for CardFd {
        fn as_fd(&self) -> BorrowedFd<'_> {
            self.0.as_fd()
        }
    }

    pub struct Gbm {
        // ===== FIELD ORDER IS LOAD-BEARING — DO NOT REORDER ====
        //
        // Rust drops struct fields in DECLARATION order:
        // `surface` must drop BEFORE `dev`. gbm_device_destroy
        // unloads the gbm backend module (the DRI driver .so);
        // if `dev` drops first, the surface's front-buffer
        // gbm_bo destructor runs AFTER unload and calls into
        // unmapped code -> SEGV at process exit. This was a
        // LATENT BUG since Phase 0 (caught Phase 2 by QA's
        // ExecMainStatus check; --capture writes the PPM +
        // run_loop EXITs Ok BOTH happen BEFORE this teardown
        // SEGV, so it was invisible to pixel-only verification).
        //
        // The gbm::Surface holds its own Ptr to the bo pool but
        // does NOT keep gbm::Device alive (separate Arc graph),
        // so declaration order is the only thing pinning the
        // unload-then-bo-destroy hazard.
        //
        // If you ever add a third gbm field, put it BEFORE
        // `dev` so dev is always the LAST field to drop.
        //
        // Verified on glass 2026-06-19 by QA via gdb backtrace
        // (PtrDrop<gbm_bo>::drop calling into unmapped addr in
        // an unloaded module).
        // =======================================================
        pub surface: gbm::Surface<()>,
        pub dev: gbm::Device<CardFd>,
    }

    impl Gbm {
        pub fn new(card_arc: &Arc<Card>, w: u32, h: u32) -> Result<Self> {
            let dev = gbm::Device::new(CardFd(card_arc.clone()))
                .context("gbm::Device::new")?;
            // ARGB8888 matches `add_framebuffer(_, 32, 32)` in
            // kms.rs. SCANOUT | RENDERING gives us a BO usable as
            // both an EGL render target and a DRM FB.
            let surface = dev
                .create_surface::<()>(
                    w,
                    h,
                    GbmFormat::Argb8888,
                    BufferObjectFlags::SCANOUT | BufferObjectFlags::RENDERING,
                )
                .context("gbm::Device::create_surface")?;
            log::info!(
                "[gbm] surface {w}x{h} ARGB8888 SCANOUT|RENDERING"
            );
            // Field construction order MATCHES declaration order
            // (surface, dev) -- keeps the load-bearing comment
            // above honest. Named-struct construction is
            // order-insensitive but mirroring the declaration
            // makes the load-bearing intent visible at the
            // construction site too.
            Ok(Gbm { surface, dev })
        }
    }

    pub struct Egl {
        /// libEGL wrapped in Arc so workers + ShareGroupContext
        /// can clone it for their own FFI / Drop work without
        /// borrowing Egl past the call site. EGL functions are
        /// thread-safe per spec (EGL 1.4+).
        pub lib: std::sync::Arc<egl::DynamicInstance<egl::EGL1_5>>,
        pub display: egl::Display,
        pub surface: egl::Surface,
        pub context: egl::Context,
        /// Saved EGL config used to build the presenter context.
        /// Retained so workers can create additional contexts
        /// in the SAME share group (and matching pbuffer
        /// surfaces). gst-gl needs a separate context for the
        /// black-flash fix -- presenter context never touched
        /// by gst-gl GL state changes.
        pub config: egl::Config,
    }

    /// Share-group EGL context + 1x1 pbuffer surface,
    /// dedicated to ONE PendingDecoder/GstDecoder pair so
    /// gst-gl's GL ops never touch the presenter context or
    /// window surface. Created in Egl::create_share_group_
    /// context; owned by the GstDecoder; self-destroyed on
    /// Drop via the Arc<DynamicInstance> clone.
    ///
    /// Textures created on this context are visible from the
    /// presenter context via GL share-group semantics
    /// (eglCreateContext share_context=presenter). So gst-gl
    /// can write into a GLMemory texture on the worker's
    /// context, and the present thread can sample it.
    ///
    /// The pbuffer is required because EGL_KHR_surfaceless_
    /// context isn't guaranteed on vc4; a 1x1 pbuffer is
    /// always cheap.
    pub struct ShareGroupContext {
        lib: std::sync::Arc<egl::DynamicInstance<egl::EGL1_5>>,
        pub display: egl::Display,
        pub context: egl::Context,
        pub surface: egl::Surface,
    }

    // SAFETY: ShareGroupContext holds EGL handles (Display +
    // Context + Surface). khronos-egl marks them !Send because
    // they wrap raw pointers, but per EGL 1.4+ spec the
    // handles + the libEGL functions that use them are
    // thread-safe. Marking Send + Sync here is what lets us
    // move the ShareGroupContext to a worker thread + into a
    // GstDecoder that itself crosses threads (retire worker
    // in kms.rs Streams::tick).
    unsafe impl Send for ShareGroupContext {}
    unsafe impl Sync for ShareGroupContext {}

    impl ShareGroupContext {
        /// eglMakeCurrent THIS context on the calling thread,
        /// using the 1x1 pbuffer as read/draw surface. Used by
        /// the worker thread before invoking any gst-gl APIs.
        pub fn make_current_here(&self) -> Result<()> {
            self.lib
                .make_current(
                    self.display,
                    Some(self.surface),
                    Some(self.surface),
                    Some(self.context),
                )
                .map_err(|e| {
                    anyhow!("eglMakeCurrent(share-group): {e:?}")
                })?;
            Ok(())
        }

        /// Release this thread's binding on the share context.
        /// The worker calls this before exiting so subsequent
        /// gst-gl streaming threads can eglMakeCurrent the
        /// context (single-thread-current semantics).
        pub fn release_here(&self) {
            let _ = self.lib.make_current(self.display, None, None, None);
        }

        /// Raw context handle as a usize -- for
        /// GstGLContext::new_wrapped which takes a uintptr_t.
        pub fn context_handle_as_usize(&self) -> usize {
            self.context.as_ptr() as usize
        }

        /// Raw display handle for symmetry with the wrap call.
        pub fn display_handle_as_usize(&self) -> usize {
            self.display.as_ptr() as usize
        }
    }

    impl Drop for ShareGroupContext {
        fn drop(&mut self) {
            // No thread should be current on this context at
            // Drop time (owning GstDecoder::Drop releases via
            // explicit release_here on the appropriate thread,
            // or the gst-gl streaming threads naturally release
            // when the pipeline goes to NULL). Best-effort
            // destroy; never panic.
            let _ = self.lib.destroy_surface(self.display, self.surface);
            let _ = self.lib.destroy_context(self.display, self.context);
        }
    }

    impl Egl {
        pub fn bring_up(gbm: &mut Gbm) -> Result<Self> {
            let lib = std::sync::Arc::new(unsafe {
                egl::DynamicInstance::<egl::EGL1_5>::load_required()
            }
            .map_err(|e| anyhow!("dlopen libEGL.so.1: {e:?}"))?);

            // Mesa auto-detects EGL_PLATFORM_GBM_KHR from the
            // gbm_device pointer here.
            let dev_ptr = gbm.dev.as_raw() as *mut std::ffi::c_void;
            let display = unsafe {
                lib.get_display(dev_ptr)
            }
            .ok_or_else(|| anyhow!("eglGetDisplay(gbm_device) returned None"))?;

            let (vmaj, vmin) = lib
                .initialize(display)
                .map_err(|e| anyhow!("eglInitialize: {e:?}"))?;
            log::info!("[egl] initialized EGL {vmaj}.{vmin}");

            lib.bind_api(egl::OPENGL_ES_API)
                .map_err(|e| anyhow!("eglBindAPI(GLES): {e:?}"))?;

            let attribs = [
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
            let config = lib
                .choose_first_config(display, &attribs)
                .map_err(|e| anyhow!("eglChooseConfig: {e:?}"))?
                .ok_or_else(|| anyhow!("no matching EGL config"))?;

            let ctx_attribs = [egl::CONTEXT_CLIENT_VERSION, 2, egl::NONE];
            let context = lib
                .create_context(display, config, None, &ctx_attribs)
                .map_err(|e| anyhow!("eglCreateContext: {e:?}"))?;

            let surface_ptr =
                gbm.surface.as_raw_mut() as egl::NativeWindowType;
            let surface = unsafe {
                lib.create_window_surface(display, config, surface_ptr, None)
            }
            .map_err(|e| anyhow!("eglCreateWindowSurface: {e:?}"))?;

            lib.make_current(
                display,
                Some(surface),
                Some(surface),
                Some(context),
            )
            .map_err(|e| anyhow!("eglMakeCurrent: {e:?}"))?;

            // swap_interval(1) blocks on vsync; paired with
            // PAGE_FLIP_EVENT (no ASYNC) downstream this yields
            // firmware-paced 60 Hz on FYS HDMI.
            lib.swap_interval(display, 1)
                .map_err(|e| anyhow!("eglSwapInterval(1): {e:?}"))?;

            Ok(Egl { lib, display, surface, context, config })
        }

        /// Create a NEW EGL context in the SAME share group as
        /// the presenter context, plus a 1x1 pbuffer surface
        /// so the context can be made-current on any thread
        /// (no window surface needed for off-screen gst-gl
        /// work).
        ///
        /// gst-gl will use THIS context (not the presenter's)
        /// for glupload + GLMemory buffer-pool work. Textures
        /// created here are visible from the presenter
        /// context via share-group semantics, so the present
        /// thread still samples them normally.
        ///
        /// The caller (worker thread) is responsible for
        /// eglMakeCurrent on this context before calling
        /// gst-gl APIs, and for destroying the
        /// ShareGroupContext when the owning GstDecoder Drops
        /// (call Egl::destroy_share_group_context).
        pub fn create_share_group_context(&self) -> Result<ShareGroupContext> {
            let ctx_attribs = [egl::CONTEXT_CLIENT_VERSION, 2, egl::NONE];
            // share_context = Some(self.context) puts the new
            // context in the SAME share group as the presenter.
            let context = self
                .lib
                .create_context(
                    self.display,
                    self.config,
                    Some(self.context),
                    &ctx_attribs,
                )
                .map_err(|e| anyhow!("eglCreateContext(share): {e:?}"))?;
            // 1x1 pbuffer suffices for the worker to be
            // current somewhere; gst-gl doesn't actually draw
            // to it.
            let pbuf_attribs =
                [egl::WIDTH, 1, egl::HEIGHT, 1, egl::NONE];
            let surface = self
                .lib
                .create_pbuffer_surface(
                    self.display,
                    self.config,
                    &pbuf_attribs,
                )
                .map_err(|e| anyhow!("eglCreatePbufferSurface(1x1): {e:?}"))?;
            log::info!(
                "[egl] share-group context created (context+1x1 pbuffer)"
            );
            Ok(ShareGroupContext {
                lib: self.lib.clone(),
                display: self.display,
                context,
                surface,
            })
        }

        pub fn swap_buffers(&self) -> Result<()> {
            self.lib
                .swap_buffers(self.display, self.surface)
                .map_err(|e| anyhow!("eglSwapBuffers: {e:?}"))?;
            Ok(())
        }

        /// Resolve a GLES2 entry point. Used by glow's loader.
        pub fn get_proc_address(&self, name: &str) -> *const std::ffi::c_void {
            self.lib
                .get_proc_address(name)
                .map(|p| p as *const _)
                .unwrap_or(std::ptr::null())
        }

        /// Re-assert that THIS thread holds the GL context
        /// current. Needed under Phase 1 because gst-gl's
        /// streaming thread may have made-current the SAME
        /// shared EGL handle for upload/conversion; before
        /// blendr's main-thread draw we re-claim it.
        pub fn make_current(&self) -> Result<()> {
            self.lib
                .make_current(
                    self.display,
                    Some(self.surface),
                    Some(self.surface),
                    Some(self.context),
                )
                .map_err(|e| anyhow!("eglMakeCurrent (re-claim): {e:?}"))?;
            Ok(())
        }

        /// Check whether the EGL context currently bound on
        /// THIS thread matches our presenter context.
        /// Returns:
        ///   true  -- our context is bound here; make_current
        ///            would be a no-op.
        ///   false -- a different context is bound (or
        ///            EGL_NO_CONTEXT) -- make_current is
        ///            required.
        ///
        /// Cost: one eglGetCurrentContext FFI (~µs; ptr lookup
        /// in TLS), no GL queue flush. Safe to call per-frame.
        ///
        /// Use case: detect when gst-gl's streaming threads
        /// have stolen the binding (their own eglMakeCurrent
        /// for upload/preroll unbinds us). Pair with
        /// make_current() to repair + count.
        pub fn is_current_context_ours(&self) -> bool {
            self.lib.get_current_context() == Some(self.context)
        }
    }

    impl Drop for Egl {
        fn drop(&mut self) {
            // Never panic in Drop. Best-effort teardown.
            let _ = self.lib.make_current(
                self.display,
                None,
                None,
                None,
            );
            let _ = self.lib.destroy_surface(self.display, self.surface);
            let _ = self.lib.destroy_context(self.display, self.context);
            let _ = self.lib.terminate(self.display);
        }
    }
}
