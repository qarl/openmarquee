//! SIGINT / SIGTERM / SIGHUP handler.
//!
//! Async-signal-safe: the handler does NOTHING but flip an
//! `AtomicBool`. The destructive cleanup (DRM master release,
//! CRTC restore) runs on the main thread once the run loop
//! observes `EXIT_REQUESTED` and returns.
//!
//! SIGPIPE is blocked so a broken stderr pipe (e.g. a dying
//! tail|less) does NOT kill us mid-restore.

use std::sync::atomic::AtomicBool;

pub static EXIT_REQUESTED: AtomicBool = AtomicBool::new(false);

#[cfg(target_os = "linux")]
mod imp {
    use super::EXIT_REQUESTED;
    use anyhow::{Context, Result};
    use nix::sys::signal::{
        sigaction, SaFlags, SigAction, SigHandler, SigSet, Signal,
    };
    use std::sync::atomic::Ordering;

    extern "C" fn handler(_: libc::c_int) {
        EXIT_REQUESTED.store(true, Ordering::Relaxed);
    }

    pub fn install() -> Result<()> {
        let act = SigAction::new(
            SigHandler::Handler(handler),
            SaFlags::empty(),
            SigSet::empty(),
        );
        for sig in [Signal::SIGINT, Signal::SIGTERM, Signal::SIGHUP] {
            // SAFETY: SigAction is well-formed; sig is valid.
            unsafe { sigaction(sig, &act) }
                .with_context(|| format!("sigaction({:?})", sig))?;
        }
        // SIGPIPE: ignore. A dying stderr/stdout pipe must not kill
        // us; we need the cleanup path to run.
        let ign = SigAction::new(
            SigHandler::SigIgn,
            SaFlags::empty(),
            SigSet::empty(),
        );
        unsafe { sigaction(Signal::SIGPIPE, &ign) }
            .context("sigaction(SIGPIPE, SIG_IGN)")?;
        Ok(())
    }
}

#[cfg(not(target_os = "linux"))]
mod imp {
    use anyhow::Result;
    pub fn install() -> Result<()> {
        Ok(())
    }
}

pub use imp::install;
