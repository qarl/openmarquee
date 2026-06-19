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
        pub dev: gbm::Device<CardFd>,
        pub surface: gbm::Surface<()>,
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
            Ok(Gbm { dev, surface })
        }
    }

    pub struct Egl {
        pub lib: egl::DynamicInstance<egl::EGL1_5>,
        pub display: egl::Display,
        pub surface: egl::Surface,
        pub context: egl::Context,
    }

    impl Egl {
        pub fn bring_up(gbm: &mut Gbm) -> Result<Self> {
            let lib = unsafe {
                egl::DynamicInstance::<egl::EGL1_5>::load_required()
            }
            .map_err(|e| anyhow!("dlopen libEGL.so.1: {e:?}"))?;

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

            Ok(Egl { lib, display, surface, context })
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
