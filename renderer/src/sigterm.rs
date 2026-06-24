//! Stability arc Layer 4 (2026-06-23): SIGTERM / SIGINT clean-shutdown
//! handler.
//!
//! Goal: when systemd kills the renderer (e.g. layer-2 watchdog
//! restart, operator `systemctl stop`, or graceful shutdown), give
//! the V4L2 decoder a chance to release `/dev/video10` cleanly via
//! Rust's normal Drop chain. Without this handler, default SIGTERM
//! disposition is process termination → Drop does NOT run →
//! `DecoderInner::Drop`'s STREAMOFF + REQBUFS(0) + fd close never
//! fires → next start can hit EBUSY on `/dev/video10` open or
//! stale-state EINVAL on REQBUFS, exactly the failure modes the
//! stability arc exists to prevent.
//!
//! Pattern is the same as the existing `sigusr1` cache-dump handler:
//! the signal handler is async-signal-safe (atomic-bool store only,
//! no I/O), and the IPC inner loop polls a `pending()` accessor
//! between requests. On the first non-zero poll, the loop breaks
//! cleanly + falls out of the per-session main → main() exits via
//! `Result::Ok(())` → all stack-allocated state (incl. EglSession,
//! SlideCache, all DecoderInner instances) drops in order, running
//! `DecoderInner::Drop` (= stop_streaming_quiet's STREAMOFF +
//! mapped_capture munmaps + capture_dmabuf_fds closes + File close
//! of `/dev/video10`).
//!
//! D-state caveat: if the renderer is currently blocked in a V4L2
//! ioctl that the kernel has stuck D-state (the prime-freeze this
//! arc targets), the SIGTERM handler is NOT delivered to that
//! thread until it exits kernel mode (which may never happen). For
//! that case, Layer 2's systemd `WatchdogSec` + `FailureAction=
//! reboot` is the catch-all. Layer 4 covers the COMMON case where
//! the renderer is in user mode (between IPC requests, idle hold,
//! awaiting next paint) — the typical state when systemd issues a
//! graceful restart.
//!
//! Unlike `sigusr1` (which uses `swap(false)` for fire-and-reset
//! semantics), this module uses `load()` only — once SIGTERM is
//! received, the flag stays set. The intent is one-shot shutdown,
//! not repeatable events.

use std::sync::atomic::{AtomicBool, Ordering};

static SHUTDOWN_REQUESTED: AtomicBool = AtomicBool::new(false);

/// POSIX signal handler. Async-signal-safe: atomic store + a small
/// async-signal-safe write(2) to stderr for breadcrumb. No
/// allocation, no formatting, no panic.
///
/// Captures SIGTERM (systemd graceful) and SIGINT (operator Ctrl-C
/// during dev).
#[cfg(target_os = "linux")]
extern "C" fn handle_shutdown(signum: libc::c_int) {
    SHUTDOWN_REQUESTED.store(true, Ordering::SeqCst);
    // Async-signal-safe breadcrumb: write(2) is on the
    // signal-safe list per signal-safety(7). The message is a
    // static byte string — no formatting, no allocation. Using
    // the literal signum-distinguishing strings vs a generic
    // "got signal" so journal grep can confirm WHICH signal
    // arrived (helpful for layer-2 watchdog timeout vs operator
    // ctrl-C diagnosis).
    let msg: &[u8] = match signum {
        libc::SIGTERM => b"[stability] SIGTERM received; flagging shutdown\n",
        libc::SIGINT => b"[stability] SIGINT received; flagging shutdown\n",
        _ => b"[stability] unknown signal received; flagging shutdown\n",
    };
    // SAFETY: write(2) is async-signal-safe. fd 2 is stderr.
    // Best-effort: if write fails (e.g., stderr closed), we
    // can't do much from a handler; ignore the result.
    unsafe {
        let _ = libc::write(
            2,
            msg.as_ptr() as *const libc::c_void,
            msg.len(),
        );
    }
}

/// Install handlers for SIGTERM + SIGINT. Best-effort: if either
/// install fails, log + continue (the renderer still runs, just
/// without graceful shutdown).
///
/// macOS no-op: the stability arc targets the Pi-side renderer.
/// macOS hosts the test suite + dev builds; signal handling there
/// has no production role.
#[cfg(target_os = "linux")]
pub fn install_handler() -> std::io::Result<()> {
    // Use the same libc::signal-based install as sigusr1 (simpler
    // than sigaction; the sa_flags we'd want — SA_RESTART to keep
    // syscalls restartable on EINTR — are already libc::signal's
    // BSD-compat default on Linux/glibc).
    unsafe {
        for sig in [libc::SIGTERM, libc::SIGINT] {
            let prev = libc::signal(sig, handle_shutdown as libc::sighandler_t);
            if prev == libc::SIG_ERR {
                return Err(std::io::Error::last_os_error());
            }
        }
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub fn install_handler() -> std::io::Result<()> {
    Ok(())
}

/// Returns true if a shutdown signal has been received. Non-
/// consuming: once set, stays set across calls. The IPC inner loop
/// polls this between request iterations + breaks on the first
/// true observation.
///
/// Use `Ordering::Relaxed` — the IPC loop's check is on the
/// happy path where we're polling for "have we been asked to
/// stop?". A delayed observation by one iteration is fine
/// (the loop iterates every few ms during steady-state and on
/// every IPC request otherwise). The handler's
/// `Ordering::SeqCst` store guarantees the write is visible
/// eventually; we don't need a synchronized fence here.
pub fn is_shutdown_requested() -> bool {
    SHUTDOWN_REQUESTED.load(Ordering::Relaxed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shutdown_default_is_false() {
        // This test runs after potentially other tests that set
        // the flag. Reset explicitly so we observe the default
        // for the assertion. (Test ordering is fragile around
        // shared static state; sigusr1 has the same caveat.)
        SHUTDOWN_REQUESTED.store(false, Ordering::SeqCst);
        assert!(!is_shutdown_requested());
    }

    #[test]
    fn shutdown_flag_is_observable_after_set() {
        SHUTDOWN_REQUESTED.store(true, Ordering::SeqCst);
        assert!(is_shutdown_requested());
        // Non-consuming: another check still sees true.
        assert!(is_shutdown_requested());
        // Cleanup so subsequent tests observe the default.
        SHUTDOWN_REQUESTED.store(false, Ordering::SeqCst);
    }
}
