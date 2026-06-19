//! DRM node probe.
//!
//! /dev/dri/card0 vs card1 swap roles across boots on Pi: the v3d
//! 3D node will accept open() + DRM_IOCTL_SET_MASTER, but page-flips
//! silently no-op on it. We MUST pick the vc4 display node by
//! reading its driver name via DRM_IOCTL_VERSION; hardcoded indices
//! fail 50/50.
//!
//! The picker also rejects nodes with zero connectors (cable
//! unplugged is reported here as a distinct error, NOT as
//! "wrong node").

use anyhow::{anyhow, bail, Context, Result};
use std::path::{Path, PathBuf};

#[cfg(target_os = "linux")]
pub use linux::*;

#[cfg(not(target_os = "linux"))]
pub use stub::*;

#[cfg(not(target_os = "linux"))]
mod stub {
    use super::*;
    pub struct Card;
    pub struct ProbePick {
        pub path: PathBuf,
        pub driver: String,
    }
    pub fn pick_display_node(
        _override_path: Option<&Path>,
    ) -> Result<ProbePick> {
        bail!("DRM probe is Linux-only")
    }
    impl Card {
        pub fn open(_: &Path) -> Result<Self> {
            bail!("Card::open is Linux-only")
        }
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use drm::control::{connector, Device as ControlDevice};
    use drm::Device;
    use std::fs::{File, OpenOptions};
    use std::os::fd::{AsFd, BorrowedFd};

    /// Newtype owning the DRM fd. Implements `drm::Device` +
    /// `drm::control::Device` so drm-rs can issue ioctls through it.
    pub struct Card(pub File);

    impl AsFd for Card {
        fn as_fd(&self) -> BorrowedFd<'_> {
            self.0.as_fd()
        }
    }
    impl Device for Card {}
    impl ControlDevice for Card {}

    impl Card {
        pub fn open(path: &Path) -> Result<Self> {
            let f = OpenOptions::new()
                .read(true)
                .write(true)
                .open(path)
                .with_context(|| {
                    format!("open({})", path.display())
                })?;
            Ok(Card(f))
        }
    }

    pub struct ProbePick {
        pub path: PathBuf,
        pub driver: String,
        pub connector_id: connector::Handle,
    }

    /// Pick the vc4 display node. If `override_path` is Some, honor
    /// it (warning if it does not look like vc4); else enumerate
    /// /dev/dri/card0..card9 and return the first vc4 node with a
    /// connected connector.
    pub fn pick_display_node(
        override_path: Option<&Path>,
    ) -> Result<ProbePick> {
        if let Some(p) = override_path {
            return score_node(p, /*allow_non_vc4=*/ true)
                .with_context(|| {
                    format!("override {}", p.display())
                });
        }
        let mut last_err: Option<anyhow::Error> = None;
        for i in 0..10 {
            let p = PathBuf::from(format!("/dev/dri/card{i}"));
            if !p.exists() {
                continue;
            }
            match score_node(&p, /*allow_non_vc4=*/ false) {
                Ok(pick) => {
                    log::info!(
                        "[drm-probe] picked {} (driver={})",
                        pick.path.display(),
                        pick.driver
                    );
                    return Ok(pick);
                }
                Err(e) => {
                    log::warn!(
                        "[drm-probe] reject {}: {e:#}",
                        p.display()
                    );
                    last_err = Some(e);
                }
            }
        }
        Err(last_err.unwrap_or_else(|| {
            anyhow!("no /dev/dri/card* nodes found")
        }))
    }

    fn score_node(path: &Path, allow_non_vc4: bool) -> Result<ProbePick> {
        let card = Card::open(path)?;
        let driver = read_driver_name(&card)
            .context("DRM_IOCTL_VERSION (driver name)")?;
        if driver != "vc4" {
            if allow_non_vc4 {
                log::warn!(
                    "[drm-probe] override {} has driver {driver} (not vc4)",
                    path.display()
                );
            } else {
                bail!("driver={driver} != vc4");
            }
        }
        let res = card
            .resource_handles()
            .context("resource_handles()")?;
        if res.connectors().is_empty() {
            bail!("no connectors enumerated");
        }
        // Pick the first connected connector that has at least
        // one mode.
        let mut connected: Option<connector::Handle> = None;
        for &c in res.connectors() {
            let info = card.get_connector(c, false)
                .with_context(|| {
                    format!("get_connector({c:?})")
                })?;
            log::debug!(
                "[drm-probe] {:?} state={:?} modes={}",
                c,
                info.state(),
                info.modes().len()
            );
            if info.state() == connector::State::Connected
                && !info.modes().is_empty()
            {
                connected = Some(c);
                break;
            }
        }
        let connector_id = connected.ok_or_else(|| {
            anyhow!(
                "no connector in Connected state with a mode \
                 (HDMI cable unplugged?)"
            )
        })?;
        // Master-lock check: ensure we can actually acquire master.
        // If EBUSY, console fbcon or another KMS client owns it;
        // surface a remediation hint instead of letting the caller
        // see an opaque ioctl error later.
        card.acquire_master_lock().context(
            "DRM_IOCTL_SET_MASTER (try Ctrl-Alt-F2 / \
             `systemctl isolate multi-user.target` to release fbcon)",
        )?;
        // Release immediately; the real run grabs master via its
        // own Card instance, so we do not want to hold it from
        // here.
        card.release_master_lock().ok();
        Ok(ProbePick {
            path: path.to_path_buf(),
            driver,
            connector_id,
        })
    }

    /// Issue DRM_IOCTL_VERSION on the fd and return the `name[]`
    /// field as a String. drm-ffi does not expose a safe wrapper
    /// for this (it is consumed by drm-rs internally), so we do
    /// it by hand via nix-style ioctl on drm_version.
    fn read_driver_name(card: &Card) -> Result<String> {
        use std::os::fd::AsRawFd;

        // struct drm_version mirrors the kernel ABI. `Default`
        // can't derive over raw pointers, so init manually with
        // null name/date/desc on the first call.
        #[repr(C)]
        struct DrmVersion {
            version_major: i32,
            version_minor: i32,
            version_patchlevel: i32,
            name_len: usize,
            name: *mut libc::c_char,
            date_len: usize,
            date: *mut libc::c_char,
            desc_len: usize,
            desc: *mut libc::c_char,
        }

        // DRM_IOWR(0x00, struct drm_version) — see uapi/drm.h.
        // _IOWR('d', 0x00, sizeof(struct drm_version))
        nix::ioctl_readwrite!(drm_version_ioctl, b'd', 0x00, DrmVersion);

        let fd = card.0.as_raw_fd();
        let mut v = DrmVersion {
            version_major: 0,
            version_minor: 0,
            version_patchlevel: 0,
            name_len: 0,
            name: std::ptr::null_mut(),
            date_len: 0,
            date: std::ptr::null_mut(),
            desc_len: 0,
            desc: std::ptr::null_mut(),
        };
        // First call: query lengths.
        // SAFETY: fd is owned by Card; v is a valid local.
        unsafe { drm_version_ioctl(fd, &mut v) }
            .context("DRM_IOCTL_VERSION (size query)")?;
        if v.name_len == 0 {
            return Ok(String::new());
        }
        // Second call: fill the name buffer.
        let mut buf: Vec<u8> = vec![0u8; v.name_len + 1];
        v.name = buf.as_mut_ptr() as *mut libc::c_char;
        // date/desc still null + zero-length so kernel skips them.
        // SAFETY: name buffer is sized to v.name_len.
        unsafe { drm_version_ioctl(fd, &mut v) }
            .context("DRM_IOCTL_VERSION (name read)")?;
        buf.truncate(v.name_len);
        Ok(String::from_utf8_lossy(&buf).trim_end_matches('\0').to_string())
    }
}
