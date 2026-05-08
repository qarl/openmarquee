//! v1-spec-delta #12 (slice b-1): memory snapshot.
//!
//! Reads the four per-process and two system-wide values the
//! soak gate (slice c) needs to assert §8.1 (steady-state
//! within budget) and §8.2 (no-monotonic-growth across the
//! soak): `VmRSS`, `VmData`, `VmSwap` from `/proc/self/status`
//! and `CmaTotal`, `CmaFree` from `/proc/meminfo`. Linux-only;
//! returns zeros on every other platform so host-side callers
//! still link.
//!
//! Parsing is split out from I/O so the host tests can exercise
//! it on canned strings — `read_linux` itself stays untested
//! since it's a thin file-read wrapper.

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct MemSnapshot {
    pub vm_rss_kb: u64,
    pub vm_data_kb: u64,
    pub vm_swap_kb: u64,
    pub cma_total_kb: u64,
    pub cma_free_kb: u64,
}

impl MemSnapshot {
    pub fn cma_used_kb(&self) -> u64 {
        self.cma_total_kb.saturating_sub(self.cma_free_kb)
    }

    /// Read the current process + system memory state. Returns
    /// `MemSnapshot::default()` (all zeros) on non-Linux or if
    /// either /proc file is unreadable. Caller logs the zero
    /// case as "instrumentation unavailable" rather than
    /// treating it as a real measurement.
    pub fn read() -> Self {
        #[cfg(target_os = "linux")]
        {
            read_linux().unwrap_or_default()
        }
        #[cfg(not(target_os = "linux"))]
        {
            MemSnapshot::default()
        }
    }
}

#[cfg(target_os = "linux")]
fn read_linux() -> Option<MemSnapshot> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    let meminfo = std::fs::read_to_string("/proc/meminfo").ok()?;
    let (vm_rss_kb, vm_data_kb, vm_swap_kb) = parse_status(&status);
    let (cma_total_kb, cma_free_kb) = parse_meminfo(&meminfo);
    Some(MemSnapshot {
        vm_rss_kb,
        vm_data_kb,
        vm_swap_kb,
        cma_total_kb,
        cma_free_kb,
    })
}

/// Parse VmRSS / VmData / VmSwap (in KB) out of /proc/self/status
/// content. Lines look like "VmRSS:    12345 kB". Missing keys
/// stay at 0 so a kernel without one of these (rare; ancient)
/// degrades to a partial sample rather than failing.
pub fn parse_status(s: &str) -> (u64, u64, u64) {
    let mut rss = 0_u64;
    let mut data = 0_u64;
    let mut swap = 0_u64;
    for line in s.lines() {
        let mut it = line.split_whitespace();
        let key = match it.next() {
            Some(k) => k.trim_end_matches(':'),
            None => continue,
        };
        let val = match it.next().and_then(|v| v.parse::<u64>().ok()) {
            Some(v) => v,
            None => continue,
        };
        match key {
            "VmRSS" => rss = val,
            "VmData" => data = val,
            "VmSwap" => swap = val,
            _ => {}
        }
    }
    (rss, data, swap)
}

/// Parse CmaTotal / CmaFree (in KB) out of /proc/meminfo content.
pub fn parse_meminfo(s: &str) -> (u64, u64) {
    let mut total = 0_u64;
    let mut free = 0_u64;
    for line in s.lines() {
        let mut it = line.split_whitespace();
        let key = match it.next() {
            Some(k) => k.trim_end_matches(':'),
            None => continue,
        };
        let val = match it.next().and_then(|v| v.parse::<u64>().ok()) {
            Some(v) => v,
            None => continue,
        };
        match key {
            "CmaTotal" => total = val,
            "CmaFree" => free = val,
            _ => {}
        }
    }
    (total, free)
}

/// v1-spec-delta #12 (slice b-2): GPU-side counters derived by
/// inspecting EglSession state at emit-time (no allocation-path
/// instrumentation needed). Caller fills these from the session;
/// mem.rs stays decoupled from hdmi-internal types.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct GpuCounters {
    /// scanout buffer objects held in the N-2 rotation (gbm BOs)
    pub bo: u32,
    /// DRM framebuffer attachments (drmModeAddFB handles)
    pub fb: u32,
    /// GL framebuffer objects (scene FBO + transition FBOs when alive)
    pub fbo: u32,
    /// GL textures alive in the session (scene tex + image-bg cache + glyph atlas).
    /// Soak parser should treat unknown columns as informational.
    pub textures: u32,
}

/// Emit the canonical `[mem]` line. The soak gate (slice c) parses
/// these out of stderr so the format is contract: keep the keys
/// stable across slices. New fields go on the right; soak parsers
/// regex by key (vm_rss=, cma_used=, etc) rather than positional-
/// parse, so adding columns is non-breaking.
///
/// FORMAT CONTRACT — see scripts/renderer_soak_parse.py PAT regex.
/// The parser anchors on `[mem]\s+<label>\s+vm_rss=NN.NMB...
/// cma_used=NN.NMB` and ignores any trailing tokens. Reordering
/// the four /proc keys WILL break the parser. Adding new keys
/// after `cma_used=` is safe (parser regex is non-greedy through
/// cma_used, then optional bo=N etc come after; new fields just
/// extend the trailing tokens that the parser ignores). When in
/// doubt: extend to the right, never reshuffle.
pub fn log_mem_snapshot(label: &str, gpu: Option<GpuCounters>) {
    let s = MemSnapshot::read();
    let suffix = match gpu {
        Some(g) => format!(
            " bo={} fb={} fbo={} textures={}",
            g.bo, g.fb, g.fbo, g.textures,
        ),
        None => String::new(),
    };
    eprintln!(
        "[mem] {label} vm_rss={:.1}MB vm_data={:.1}MB swap={:.1}MB cma_used={:.1}MB{suffix}",
        s.vm_rss_kb as f64 / 1024.0,
        s.vm_data_kb as f64 / 1024.0,
        s.vm_swap_kb as f64 / 1024.0,
        s.cma_used_kb() as f64 / 1024.0,
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_status_picks_three_keys() {
        let s = "\
Name:\trenderer\n\
State:\tR (running)\n\
VmPeak:\t  123456 kB\n\
VmSize:\t  123000 kB\n\
VmData:\t   45000 kB\n\
VmRSS:\t   30000 kB\n\
VmSwap:\t       0 kB\n\
Threads:\t1\n";
        let (rss, data, swap) = parse_status(s);
        assert_eq!(rss, 30000);
        assert_eq!(data, 45000);
        assert_eq!(swap, 0);
    }

    #[test]
    fn parse_status_missing_keys_default_zero() {
        let s = "Name:\trenderer\nThreads:\t1\n";
        assert_eq!(parse_status(s), (0, 0, 0));
    }

    #[test]
    fn parse_meminfo_picks_two_cma_keys() {
        let s = "\
MemTotal:        426000 kB\n\
MemFree:         103000 kB\n\
CmaTotal:        262144 kB\n\
CmaFree:          70000 kB\n\
Slab:             42000 kB\n";
        let (total, free) = parse_meminfo(s);
        assert_eq!(total, 262144);
        assert_eq!(free, 70000);
    }

    #[test]
    fn parse_meminfo_no_cma_returns_zeros() {
        let s = "MemTotal: 426000 kB\nMemFree: 100000 kB\n";
        assert_eq!(parse_meminfo(s), (0, 0));
    }

    #[test]
    fn cma_used_subtracts_free_from_total_saturating() {
        let s = MemSnapshot {
            cma_total_kb: 262144,
            cma_free_kb: 70000,
            ..Default::default()
        };
        assert_eq!(s.cma_used_kb(), 192144);
        let inverted = MemSnapshot {
            cma_total_kb: 0,
            cma_free_kb: 70000,
            ..Default::default()
        };
        assert_eq!(inverted.cma_used_kb(), 0);
    }

    #[test]
    fn parse_status_garbled_value_skips() {
        let s = "VmRSS:\twhoops kB\nVmData: 100 kB\n";
        let (rss, data, _) = parse_status(s);
        assert_eq!(rss, 0);
        assert_eq!(data, 100);
    }
}
