//! r38d SIGUSR1 cache-dump handler.
//!
//! Async-signal-safe POSIX handler that sets an AtomicBool flag;
//! the IPC inner loop checks the flag between Advance commands and
//! performs the actual dump (eprintln + mem::MemSnapshot::read +
//! session.cma_dump_cache_lens) in normal Rust execution context
//! where formatting + allocation are safe.
//!
//! Use case: SSH to the Pi, `sudo pkill -USR1 -f openmarquee-render`,
//! then `journalctl -u openmarquee-backend.service -n 30 --no-pager`
//! to see the dump line. Confirms whether bounded CMA pressure is
//! cache-driven (image_bg_cache + image_slide_tex_cache fill-and-
//! evict) vs render-pipeline-driven (scanout BO churn).
//!
//! See qa/r38d-sigusr1-cache-dump-2026-06-02.md.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static SIGUSR1_RECEIVED: AtomicBool = AtomicBool::new(false);

/// POSIX signal handler. Must be async-signal-safe: only sets the
/// flag, no I/O, no allocation, no Rust panics. Calls libc::signal
/// via the install_handler entry point.
#[cfg(target_os = "linux")]
extern "C" fn handle_sigusr1(_: libc::c_int) {
    SIGUSR1_RECEIVED.store(true, Ordering::SeqCst);
}

/// Install the SIGUSR1 handler. Best-effort -- if it fails the
/// process continues (signal-handler unavailability is not fatal
/// to rendering). Linux-only; on macOS the function is a no-op so
/// host `cargo test` links clean.
#[cfg(target_os = "linux")]
pub fn install_handler() -> std::io::Result<()> {
    unsafe {
        let prev = libc::signal(
            libc::SIGUSR1,
            handle_sigusr1 as libc::sighandler_t,
        );
        if prev == libc::SIG_ERR {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub fn install_handler() -> std::io::Result<()> {
    Ok(())
}

/// Returns true exactly once per SIGUSR1 received (atomic swap).
/// Idempotent across threads -- only one caller observes true per
/// signal. The IPC inner loop calls this at the top of each
/// iteration; if true, it dumps + continues.
pub fn take_pending() -> bool {
    SIGUSR1_RECEIVED.swap(false, Ordering::SeqCst)
}

/// Build the dump line + emit to stderr. Called in the regular
/// execution context (NOT from inside the signal handler).
/// `eprintln!` is signal-unsafe but we are not in a signal context
/// here. Caller supplies the cap constants (imported from the
/// canonical hdmi:: + image_slide_tex:: pub consts) so the dump
/// auto-flows future cap bumps.
pub fn emit_dump_line(
    bg_cache_len: usize, bg_cap: usize,
    tex_cache_len: usize, tex_cap: usize,
) {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let mem = crate::mem::MemSnapshot::read();
    let cma_used_kb = mem.cma_used_kb();
    let line = format_dump_line(
        ts,
        bg_cache_len, bg_cap,
        tex_cache_len, tex_cap,
        &mem,
        cma_used_kb,
    );
    eprintln!("{line}");
}

/// Pure formatter -- host-testable on macOS without signal
/// infrastructure. Returns the single TAB-separated key=value
/// line that gets eprintln'd. Format spec is documented in
/// qa/r38d-sigusr1-cache-dump-2026-06-02.md §C.
pub fn format_dump_line(
    ts: u64,
    bg_len: usize,
    bg_cap: usize,
    tex_len: usize,
    tex_cap: usize,
    mem: &crate::mem::MemSnapshot,
    cma_used_kb: u64,
) -> String {
    format!(
        "[cache-dump]\tts={ts}\timage_bg_cache_len={bg_len}/{bg_cap}\t\
         image_slide_tex_cache_len={tex_len}/{tex_cap}\t\
         vm_rss_kb={vm_rss}\tvm_data_kb={vm_data}\tvm_swap_kb={vm_swap}\t\
         cma_total_kb={cma_total}\tcma_free_kb={cma_free}\t\
         cma_used_kb={cma_used_kb}\tcma_used_mb={cma_used_mb}",
        vm_rss = mem.vm_rss_kb,
        vm_data = mem.vm_data_kb,
        vm_swap = mem.vm_swap_kb,
        cma_total = mem.cma_total_kb,
        cma_free = mem.cma_free_kb,
        cma_used_mb = cma_used_kb / 1024,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mem::MemSnapshot;

    #[test]
    fn format_dump_line_emits_all_keys_tab_separated() {
        let mem = MemSnapshot {
            vm_rss_kb: 12048,
            vm_data_kb: 8200,
            vm_swap_kb: 44800,
            cma_total_kb: 262144,
            cma_free_kb: 12288,
        };
        let cma_used_kb = mem.cma_used_kb();
        let s = format_dump_line(1717286400, 4, 6, 2, 6, &mem, cma_used_kb);
        assert!(s.starts_with("[cache-dump]\t"));
        assert!(s.contains("ts=1717286400"));
        assert!(s.contains("image_bg_cache_len=4/6"));
        assert!(s.contains("image_slide_tex_cache_len=2/6"));
        assert!(s.contains("vm_rss_kb=12048"));
        assert!(s.contains("vm_data_kb=8200"));
        assert!(s.contains("vm_swap_kb=44800"));
        assert!(s.contains("cma_total_kb=262144"));
        assert!(s.contains("cma_free_kb=12288"));
        assert!(s.contains("cma_used_kb=249856"));
        assert!(s.contains("cma_used_mb=244"));  // 249856/1024 = 244 exact
        // 10 key=value fields after [cache-dump] prefix -> 10 tabs.
        assert_eq!(s.matches('\t').count(), 10);
        // Single line.
        assert!(!s.contains('\n'));
    }

    #[test]
    fn format_dump_line_zero_mem_snapshot_doesnt_panic() {
        let mem = MemSnapshot::default();
        let s = format_dump_line(0, 0, 6, 0, 6, &mem, 0);
        assert!(s.contains("cma_used_kb=0"));
        assert!(s.contains("cma_used_mb=0"));
        assert!(s.contains("image_bg_cache_len=0/6"));
    }

    #[test]
    fn format_dump_line_cap_at_max() {
        let mem = MemSnapshot {
            cma_total_kb: 262144,
            cma_free_kb: 0,
            ..Default::default()
        };
        let s = format_dump_line(1, 6, 6, 6, 6, &mem, 262144);
        assert!(s.contains("image_bg_cache_len=6/6"));
        assert!(s.contains("image_slide_tex_cache_len=6/6"));
        assert!(s.contains("cma_used_mb=256"));
    }

    #[test]
    fn take_pending_consumes_flag() {
        // Note: this test mutates the shared static. Other tests
        // that read SIGUSR1_RECEIVED must not run in parallel.
        // cargo test serializes by default within a single test
        // binary; this is OK.
        SIGUSR1_RECEIVED.store(true, Ordering::SeqCst);
        assert!(take_pending(), "first take should observe true");
        assert!(!take_pending(), "second take should observe false (consumed)");
    }
}
