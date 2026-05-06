//! Phase 2 — pixels on the HDMI display.
//!
//! Walks the smallest viable path from process-start to a solid-color
//! frame on screen: open DRM card → find connected HDMI connector →
//! pick a mode → bring up GBM + EGL + GLES2 → clear with a chosen
//! color → swap → push to scanout via legacy `drmModeSetCrtc` →
//! hold for `duration` seconds → cleanup.
//!
//! Legacy mode-set is used here. The plan §4 Step 2 schedules atomic
//! commit + double-buffered page-flip for the production renderer;
//! this is the smallest end-to-end test of the GLES → DRM scanout
//! pipeline, not the production path. Once this lands we replace
//! `drmModeSetCrtc` with `drmModeAtomicCommit`.
//!
//! The error model is intentionally chatty (`anyhow::Context`) so any
//! failure during Phase 2 bring-up tells you which step blew up.

use std::ffi::c_void;
use std::ptr;

use anyhow::{anyhow, bail, Context, Result};
use drm::buffer::{Buffer as DrmBuffer, DrmFourcc, Handle as DrmHandle};
use drm::control::{
    connector::{self, State as ConnectorState},
    Device as ControlDevice, Mode,
};
use gbm::{AsRaw, BufferObject, BufferObjectFlags, Format as GbmFormat};
use khronos_egl as egl;

use crate::Card;

/// Render a single solid-color frame, push it to the HDMI display via
/// legacy `drmModeSetCrtc`, and hold for `duration_secs` seconds.
///
/// `color` is RGBA in [0.0, 1.0] linear space. The vc4 HVS handles
/// gamma at scanout per the connector's Colorspace property — we just
/// hand it premultiplied float color and let the hardware do the rest.
pub fn render_solid_color(card: &Card, color: [f32; 4], duration_secs: u64) -> Result<()> {
    // -----------------------------------------------------------------
    // 1. Find a connected HDMI connector + a usable mode.
    // -----------------------------------------------------------------
    let resources = card
        .resource_handles()
        .context("drmModeGetResources failed")?;

    let (connector_info, mode) = pick_connector_and_mode(card, &resources)
        .context("no connected HDMI connector with a usable mode")?;
    let (mode_w, mode_h) = mode.size();
    eprintln!(
        "selected connector {:?} {:?} at {}x{}@{}",
        connector_info.handle(),
        connector_info.interface(),
        mode_w,
        mode_h,
        mode.vrefresh(),
    );

    // -----------------------------------------------------------------
    // 2. Find an encoder + CRTC that can drive this connector.
    //
    // Legacy path: `connector.current_encoder()` is the encoder the
    // kernel last bound. If it's not set (cold boot, headless prior),
    // fall back to the connector's first listed encoder.
    // -----------------------------------------------------------------
    let encoder_handle = connector_info
        .current_encoder()
        .or_else(|| connector_info.encoders().first().copied())
        .ok_or_else(|| anyhow!("connector advertises no encoders"))?;
    let encoder_info = card
        .get_encoder(encoder_handle)
        .context("drmModeGetEncoder failed")?;

    // Pick a CRTC that's actually compatible with this encoder. The
    // kernel exposes which CRTCs each encoder can drive via the
    // `possible_crtcs` bitmask — bit N = `resources.crtcs()[N]`.
    // possible_crtcs would let us skip incompatible CRTCs, but the
    // bitfield's u32 representation isn't exposed via a public method
    // in drm-rs 0.12. Phase 2 just falls back to the encoder's
    // currently-bound CRTC if there is one, otherwise the first one
    // resources advertises. The vc4 driver exposes 4 CRTCs and any
    // encoder's possible_crtcs is generally compatible with the first.
    // Atomic commit (plan §4 Step 2) replaces this whole block.
    let crtc_handle = encoder_info
        .crtc()
        .or_else(|| resources.crtcs().first().copied())
        .ok_or_else(|| anyhow!("no CRTC available for encoder {:?}", encoder_handle))?;
    eprintln!("using encoder {:?} crtc {:?}", encoder_handle, crtc_handle);

    // -----------------------------------------------------------------
    // 3. Bring up GBM on the DRM fd.
    //
    // GBM is the "Generic Buffer Manager" that hands out scanout-
    // capable buffers we can render into via EGL/GLES and present via
    // `drmModeAddFB`. The surface is sized to the chosen mode.
    // -----------------------------------------------------------------
    let gbm_dev = gbm::Device::new(card.0.try_clone().context("clone DRM fd for GBM")?)
        .context("gbm_create_device failed")?;
    let gbm_surface = gbm_dev
        .create_surface::<()>(
            mode_w as u32,
            mode_h as u32,
            GbmFormat::Argb8888,
            BufferObjectFlags::SCANOUT | BufferObjectFlags::RENDERING,
        )
        .context("gbm_surface_create failed")?;

    // -----------------------------------------------------------------
    // 4. Bring up EGL — load libEGL.so.1 at runtime, find a config
    //    matching ARGB8888 + GLES2-renderable, create context + surface.
    // -----------------------------------------------------------------
    // EGL 1.5 is required for `eglGetPlatformDisplay`. Mesa 25 on the Pi
    // ships 1.5; spike data confirms.
    let egl_lib = unsafe {
        egl::DynamicInstance::<egl::EGL1_5>::load_required().map_err(|e| {
            anyhow!("eglDynamicInstance::<EGL1_5>::load_required failed: {e:?}")
        })?
    };

    // GBM platform display. khronos_egl's wrapper hands the gbm_device
    // pointer to `eglGetPlatformDisplay`. We log the pointer because a
    // null/invalid value is the usual cause of `BadParameter` here.
    let gbm_dev_ptr: *mut c_void = gbm_dev.as_raw() as *mut c_void;
    eprintln!("gbm_device raw ptr: {gbm_dev_ptr:p}");
    if gbm_dev_ptr.is_null() {
        bail!("gbm_device raw pointer is null");
    }
    // Legacy `eglGetDisplay(gbm_device*)` is what the prior Python
    // spike used and what most production code paths use against
    // Mesa+GBM. Core 1.5's `eglGetPlatformDisplay` is functionally
    // equivalent but Mesa expects the legacy entry point for GBM
    // displays; eglGetPlatformDisplay returns BadParameter otherwise.
    let native_display = gbm_dev_ptr as egl::NativeDisplayType;
    let display = unsafe {
        egl_lib
            .get_display(native_display)
            .ok_or_else(|| anyhow!("eglGetDisplay returned NO_DISPLAY"))?
    };
    let (egl_major, egl_minor) = egl_lib
        .initialize(display)
        .map_err(|e| anyhow!("eglInitialize failed: {e:?}"))?;
    eprintln!("EGL {}.{}", egl_major, egl_minor);

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
    let configs = egl_lib
        .choose_first_config(display, &cfg_attribs)
        .map_err(|e| anyhow!("eglChooseConfig failed: {e:?}"))?
        .ok_or_else(|| anyhow!("no EGL config matched ARGB8888 + GLES2"))?;

    let ctx_attribs = [egl::CONTEXT_CLIENT_VERSION, 2, egl::NONE];
    let context = egl_lib
        .create_context(display, configs, None, &ctx_attribs)
        .map_err(|e| anyhow!("eglCreateContext failed: {e:?}"))?;

    let egl_surface = unsafe {
        let raw_surface = gbm_surface.as_raw_mut() as *mut c_void;
        egl_lib
            .create_window_surface(display, configs, raw_surface, None)
            .map_err(|e| anyhow!("eglCreateWindowSurface failed: {e:?}"))?
    };

    egl_lib
        .make_current(display, Some(egl_surface), Some(egl_surface), Some(context))
        .map_err(|e| anyhow!("eglMakeCurrent failed: {e:?}"))?;

    // -----------------------------------------------------------------
    // 5. GLES2 — clear the framebuffer with the chosen color, swap.
    //
    // glow needs a function loader; we hand it the EGL `get_proc_address`.
    // -----------------------------------------------------------------
    let gl = unsafe {
        glow::Context::from_loader_function(|name| {
            egl_lib
                .get_proc_address(name)
                .map(|fp| fp as *const _)
                .unwrap_or(ptr::null())
        })
    };

    {
        use glow::HasContext;
        unsafe {
            gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            gl.clear_color(color[0], color[1], color[2], color[3]);
            gl.clear(glow::COLOR_BUFFER_BIT);
            gl.flush();
        }
    }

    egl_lib
        .swap_buffers(display, egl_surface)
        .map_err(|e| anyhow!("eglSwapBuffers failed: {e:?}"))?;

    // -----------------------------------------------------------------
    // 6. Lock the GBM surface's front buffer object, register it as a
    //    DRM framebuffer, and push to the CRTC via legacy SetCrtc.
    // -----------------------------------------------------------------
    let bo = unsafe {
        gbm_surface
            .lock_front_buffer()
            .context("gbm_surface_lock_front_buffer failed")?
    };

    // Bridge gbm::BufferObject → drm::buffer::Buffer so we can hand
    // the GPU-rendered front BO to drmModeAddFB. The crates don't
    // know about each other, so we wrap and re-expose the four
    // fields drm-rs needs (size, format, pitch, handle).
    let fb_buf = GbmBufferAdapter::new(&bo).context("read GBM bo metadata")?;
    let fb = card
        .add_framebuffer(&fb_buf, 32, 32)
        .context("drmModeAddFB on GBM front buffer failed")?;
    eprintln!("registered fb {:?}", fb);

    // Legacy SetCrtc is the simplest path to scanout — it sets the
    // mode, picks a primary plane internally, and binds the fb. Atomic
    // commit follows in plan §4 Step 2.
    card.set_crtc(
        crtc_handle,
        Some(fb),
        (0, 0),
        &[connector_info.handle()],
        Some(mode),
    )
    .context("drmModeSetCrtc failed")?;
    eprintln!(
        "scanout active: holding {:?} for {}s",
        crtc_handle, duration_secs
    );

    // -----------------------------------------------------------------
    // 7. Hold for `duration_secs` seconds, then explicit cleanup.
    //
    // Drop order matters. Code below does, in this order:
    //   1. Unbind the EGL context (so subsequent destroys are valid).
    //   2. Destroy EGL context + surface, terminate display.
    //   3. Drop the GBM front BO (releases the lock).
    //   4. drmModeRmFB on the framebuffer.
    //
    // The FB-after-BO order is fine because DRM framebuffers are
    // reference-counted by the kernel: drmModeRmFB just removes our
    // userspace reference, the actual destroy happens once scanout
    // releases its hold (which we don't explicitly clear — last
    // frame stays latched on the display until the next scanout
    // acquires the CRTC). The remaining gbm_surface/gbm_dev/drm fd
    // tear down via Drop on scope exit, in correct order.
    // -----------------------------------------------------------------
    std::thread::sleep(std::time::Duration::from_secs(duration_secs));

    // Explicit teardown: detach FB, drop GBM bo, drop EGL state.
    egl_lib
        .make_current(display, None, None, None)
        .map_err(|e| anyhow!("eglMakeCurrent(unbind) failed: {e:?}"))?;
    egl_lib
        .destroy_context(display, context)
        .map_err(|e| anyhow!("eglDestroyContext failed: {e:?}"))?;
    egl_lib
        .destroy_surface(display, egl_surface)
        .map_err(|e| anyhow!("eglDestroySurface failed: {e:?}"))?;
    egl_lib
        .terminate(display)
        .map_err(|e| anyhow!("eglTerminate failed: {e:?}"))?;

    drop(bo);
    card.destroy_framebuffer(fb)
        .context("drmModeRmFB failed")?;

    eprintln!("solid-color render complete");
    Ok(())
}

/// Bridge between gbm::BufferObject and drm::buffer::Buffer. The two
/// crates are independent; gbm's BufferObject doesn't impl drm-rs's
/// Buffer trait. This newtype reads the four fields drm-rs's
/// `add_framebuffer` needs (size, format, pitch, handle) at construction
/// time so we can hand it across.
struct GbmBufferAdapter {
    width: u32,
    height: u32,
    format: DrmFourcc,
    pitch: u32,
    handle: DrmHandle,
}

impl GbmBufferAdapter {
    fn new<T: 'static>(bo: &BufferObject<T>) -> Result<Self> {
        let width = bo.width().context("gbm bo width")?;
        let height = bo.height().context("gbm bo height")?;
        let stride = bo.stride().context("gbm bo stride")?;
        let gbm_fmt = bo.format().context("gbm bo format")?;
        // gbm::Format and drm-fourcc::DrmFourcc both wrap a fourcc u32.
        // The values match per the DRM_FORMAT_* spec; the enum names
        // match too (Argb8888, Xrgb8888, etc.). Safest path: get the
        // raw fourcc and rebuild on the drm side.
        let fourcc_bytes = gbm_fourcc_bytes(gbm_fmt);
        let format = DrmFourcc::try_from(u32::from_le_bytes(fourcc_bytes))
            .map_err(|e| anyhow!("unsupported drm fourcc from gbm format: {e}"))?;
        // gbm 0.15's BufferObject::handle returns a u32 wrapped; the
        // raw value is what drm-rs's Handle is built from.
        // gbm_bo_handle is a C union (u32_/s32/u64_/s64). For DRM
        // handles we always read u32_. Reading a union field is
        // unsafe in Rust regardless of the variants' layouts.
        let bo_handle = bo.handle().context("gbm bo handle")?;
        let raw_handle = unsafe { bo_handle.u32_ };
        let handle = DrmHandle::from(
            std::num::NonZeroU32::new(raw_handle)
                .ok_or_else(|| anyhow!("gbm bo handle was 0"))?,
        );
        Ok(Self {
            width,
            height,
            format,
            pitch: stride,
            handle,
        })
    }
}

impl DrmBuffer for GbmBufferAdapter {
    fn size(&self) -> (u32, u32) {
        (self.width, self.height)
    }
    fn format(&self) -> DrmFourcc {
        self.format
    }
    fn pitch(&self) -> u32 {
        self.pitch
    }
    fn handle(&self) -> DrmHandle {
        self.handle
    }
}

/// gbm 0.15's Format enum doesn't expose `.bits()` or `Into<u32>`;
/// match on the variants we care about and emit the corresponding
/// fourcc bytes (matching DRM_FORMAT_*).
fn gbm_fourcc_bytes(fmt: GbmFormat) -> [u8; 4] {
    match fmt {
        GbmFormat::Argb8888 => *b"AR24",
        GbmFormat::Xrgb8888 => *b"XR24",
        GbmFormat::Abgr8888 => *b"AB24",
        GbmFormat::Xbgr8888 => *b"XB24",
        GbmFormat::Rgba8888 => *b"RA24",
        GbmFormat::Rgbx8888 => *b"RX24",
        // Any format we hit that isn't in this list will fail
        // DrmFourcc::try_from below; that's an acceptable error for
        // Phase 2's narrow ARGB scanout path.
        _ => [0, 0, 0, 0],
    }
}

/// Find the first connected connector and its largest mode.
fn pick_connector_and_mode(
    card: &Card,
    resources: &drm::control::ResourceHandles,
) -> Result<(connector::Info, Mode)> {
    for &handle in resources.connectors() {
        let info = card
            .get_connector(handle, false)
            .with_context(|| format!("get_connector({handle:?})"))?;
        if info.state() != ConnectorState::Connected {
            continue;
        }
        // Pick the mode with the largest pixel area, breaking ties by
        // refresh rate.
        if let Some(mode) = info
            .modes()
            .iter()
            .max_by_key(|m| {
                let (w, h) = m.size();
                (w as u32 * h as u32, m.vrefresh())
            })
            .copied()
        {
            return Ok((info, mode));
        }
    }
    bail!("no connected connector with any modes")
}
