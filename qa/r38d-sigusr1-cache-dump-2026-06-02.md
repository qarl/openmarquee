# r38d — SIGUSR1 cache-dump handler in renderer

**Author:** jimmy:openmarquee-code2
**Date:** 2026-06-02
**Status:** PENDING — flagged hdmi.rs touch authorization
**Predecessors:**
  - [r38b hdmi.rs CMA deep-read](r38b-hdmi-cma-deep-read-2026-06-02.md) (REFUTED leak hypothesis)
  - [r38c CMA-pressure watchdog](r38c-cma-pressure-watchdog-2026-06-02.md) (stopgap shipping)
  - [Cron enable-guard follow-up](cron-enable-guard-followup-2026-06-02.md)

## A. Why this dispatch exists

QA's post-r38c FYS time-series trace shows CMA usage swinging
**229-254 MB on per-minute intervals over 15 min** — a noisy band
~25 MB wide, NOT a stable steady state.

This means:
- D.2's "187 → 255.8 MB over 6h drift" was likely mostly variance +
  bounded cache fill, NOT a real ~70 MB leak.
- r38b's PASS verdict on all 13 GBM scanout BO release paths is
  reinforced.
- A real leak (if any) is sub-noise: ≤ 5 MB/6h, well below the
  noise floor.

The missing observation is **which allocator surface drives the
229 → 254 MB swing**. The candidates:

1. **Cache fill-and-evict** — image_bg_cache (cap 6 entries) +
   image_slide_tex_cache (cap 6 entries) at ~8-16 MB per entry =
   96-192 MB ceiling. Per-slide entries evict on LRU; per-paint
   churn pulls fresh bytes through.
2. **Scanout BO churn** — GBM dumb buffers (16 MB at 1080p ARGB)
   rotate per-frame between front/back. CMA accounting tracks
   reserved pages, so a healthy rotation looks like steady high
   usage with brief drops at buffer-release moments.
3. **Render-pipeline transient** — shader FBOs, scratch buffers,
   transition bake textures. Each is short-lived but high-water.
4. **Atlas page allocation** — MSDF atlas pages (2048x2048 = 16 MB
   each); static + dynamic. Documented as fixed-page-count.

The cache-dump handler surfaces (1) + (3) + (4) directly so QA can
visually correlate dump readings to the swing band.

## B. Signal choice — why SIGUSR1

### B.1 Alternatives considered

| Option        | Pros                                | Cons                                                                                   |
| ------------- | ----------------------------------- | -------------------------------------------------------------------------------------- |
| SIGUSR1       | POSIX standard, simple, free; kill(1) drives it | Process-scoped; needs the right PID                                                    |
| SIGUSR2       | Same as USR1                        | Reserved for future use (e.g. log-rotate trigger we may want later)                    |
| HTTP endpoint | Browser-friendly                    | Renderer has no HTTP surface; would need a new server thread                           |
| IPC poke      | Reuses existing stdin channel       | Backend would need to relay; couples backend lifecycle to debug visibility             |
| /proc poke    | Zero process change                 | Misses the cache-internal numbers — same data QA already has                           |

**Chosen: SIGUSR1.** Simplest, async-signal-safe pattern,
operator-direct via `pkill -USR1 -f openmarquee-render`.

### B.2 Async-signal-safety

POSIX signal handlers MUST be async-signal-safe — only a tiny
subset of libc functions is safe to call from inside one (no
`malloc`, no `printf`, no Rust `eprintln!` because it uses
`stderr` via stdio buffering which is not signal-safe).

**Implementation:**
- Signal handler sets `static AtomicBool SIGUSR1_RECEIVED` only.
  No I/O, no allocation, no formatting in the handler.
- The IPC inner loop checks the flag between Advance commands and
  performs the dump in the regular execution context (where
  `eprintln!`, `mem::MemSnapshot::read`, and session method calls
  are all safe).
- Atomic flag pattern is the canonical approach for "I want to do
  work in response to a signal but not from inside the handler."

## C. Dump format

Single line on stderr (journald-visible via openmarquee-backend.
service log capture), with `[cache-dump]` prefix and TAB-separated
key=value pairs:

```
[cache-dump]	ts=1717286400	image_bg_cache_len=4/6	image_slide_tex_cache_len=2/6	vm_rss_kb=12048	vm_data_kb=8200	vm_swap_kb=44800	cma_total_kb=262144	cma_free_kb=12288	cma_used_kb=249856	cma_used_mb=243
```

### C.1 Fields

| Key                          | Source                                       | Notes                                                                                                       |
| ---------------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `ts`                         | `SystemTime::now()` epoch seconds            | Lets QA cross-reference with their per-minute trace                                                          |
| `image_bg_cache_len`         | `Session::cma_dump_cache_lens()` first elem  | `len/cap` formatted; cap from `IMAGE_BG_CACHE_CAPACITY = 6`                                                  |
| `image_slide_tex_cache_len`  | `Session::cma_dump_cache_lens()` second elem | `len/cap` formatted; cap from `IMAGE_SLIDE_TEX_CACHE_CAPACITY = 6`                                           |
| `vm_rss_kb`                  | `/proc/self/status` VmRSS                    | Existing `mem::MemSnapshot` parser                                                                          |
| `vm_data_kb`                 | `/proc/self/status` VmData                   | Heap-resident; doesn't include CMA allocations                                                              |
| `vm_swap_kb`                 | `/proc/self/status` VmSwap                   | Cold-page swap accounting                                                                                   |
| `cma_total_kb`               | `/proc/meminfo` CmaTotal                     | Static — 262144 on Pi Zero 2 W with default cma=                                                            |
| `cma_free_kb`                | `/proc/meminfo` CmaFree                      | Dynamic — instantaneous available                                                                           |
| `cma_used_kb`                | derived                                      | `cma_total_kb - cma_free_kb`                                                                                |
| `cma_used_mb`                | derived                                      | `cma_used_kb / 1024`                                                                                        |

### C.2 Byte-estimate decision

Dispatch suggested "estimated bytes per entry" — rejected as
out-of-scope for r38d. Reasoning:

- Per-cache byte estimates would require per-entry size
  introspection (each ImageSlide entry's GLES texture dim, mipmap
  level, format). Not currently tracked.
- The cache `len/cap` ratio + observable `cma_used_mb` lets QA
  compute the effective per-entry bytes by inverting: when cache
  is at cap (6/6) and cma steady, `(cma_used - baseline) / 12 ≈
  per-entry-bytes-avg`.
- Adding precise bytes is more code (+ a per-cache-entry size
  accessor in hdmi.rs) without changing what answers the dispatch's
  question.

### C.3 Why TAB-separated key=value

- Greppable: `journalctl | grep '\[cache-dump\]'` returns one line
  per dump.
- Parseable: `awk '{for (i=1; i<=NF; i++) print $i}'` splits cleanly.
- Diff-friendly: pasting two dumps side-by-side in a terminal
  visually shows which keys changed.
- Doesn't pretend to be JSON (which would need quoting and bracket
  hygiene).

## D. Implementation plan

### D.1 Files touched

| File                          | Change                                                                       | LOC |
| ----------------------------- | ---------------------------------------------------------------------------- | --- |
| `renderer/src/main.rs`        | New `sigusr1.rs` module declared; or inline in main.rs                       | ~5  |
| `renderer/src/sigusr1.rs`     | NEW: signal handler + AtomicBool flag + dump-format helper                   | ~80 |
| `renderer/src/ipc_main.rs`    | Install handler at run_ipc_sidecar entry; check flag in inner loop          | ~15 |
| `renderer/src/hdmi.rs`        | **PENDING** Add pub `Session::cma_dump_cache_lens(&self) -> (usize, usize)` | ~6  |
| `renderer/tests/sigusr1.rs`   | Unit test: dump-format helper produces parseable output                      | ~40 |

### D.2 hdmi.rs touch — REQUIRED, authorization pending

The cache fields on Session are private (hdmi.rs:276 + :282).
Without a public accessor, ipc_main.rs cannot read cache lens.

**Proposed minimal touch:**

```rust
// On Session impl block in hdmi.rs (location: near gpu_counters() at
// the existing pub-methods cluster):
//
// r38d cache-dump SIGUSR1 surface. Cheap accessor — both caches
// already expose pub len() (lru.rs:69, image_slide_tex.rs:105).
// Used by ipc_main.rs's SIGUSR1 inner-loop dump.
pub fn cma_dump_cache_lens(&self) -> (usize, usize) {
    (self.image_bg_cache.len(), self.image_slide_tex_cache.len())
}
```

6 LOC including comment + blank line. Far from r38b's
transition-closure region (4245-4331); no conflict possible.

**Decision pending QA:** I've sent a flag requesting either:
- (A) Authorize this 6-LOC accessor → ship full r38d
- (B) Defer cache visibility → ship system-only dump now,
  cache-level in r38d-part-B post-r40

### D.3 ipc_main.rs touch — clean

```rust
// At entry of run_ipc_sidecar (line 782):
sigusr1::install_handler().ok();  // best-effort; non-fatal if it fails

// In run_open_and_inner_loop_linux, inside the run_in_egl_session
// closure (line 1143 onwards), per-iteration of the inner stdin
// loop:
if sigusr1::take_pending() {
    let (bg_len, tex_len) = session.cma_dump_cache_lens();  // PENDING hdmi.rs auth
    sigusr1::emit_dump_line(bg_len, tex_len);
}
```

### D.4 main.rs / sigusr1.rs

A new module under `renderer/src/sigusr1.rs`:

```rust
//! r38d SIGUSR1 cache-dump handler. POSIX signal → atomic flag
//! → inner-loop dispatcher. Async-signal-safe handler (sets one
//! AtomicBool); all I/O + formatting happens in the regular
//! execution context.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static SIGUSR1_RECEIVED: AtomicBool = AtomicBool::new(false);

extern "C" fn handle_sigusr1(_: libc::c_int) {
    SIGUSR1_RECEIVED.store(true, Ordering::SeqCst);
}

pub fn install_handler() -> std::io::Result<()> {
    #[cfg(target_os = "linux")]
    unsafe {
        let prev = libc::signal(libc::SIGUSR1, handle_sigusr1 as libc::sighandler_t);
        if prev == libc::SIG_ERR {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}

pub fn take_pending() -> bool {
    SIGUSR1_RECEIVED.swap(false, Ordering::SeqCst)
}

pub fn emit_dump_line(bg_cache_len: usize, tex_cache_len: usize) {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let mem = crate::mem::MemSnapshot::read();
    let cma_used_kb = mem.cma_used_kb();
    let line = format_dump_line(
        ts,
        bg_cache_len, IMAGE_BG_CACHE_CAP,
        tex_cache_len, IMAGE_SLIDE_TEX_CACHE_CAP,
        &mem,
        cma_used_kb,
    );
    eprintln!("{line}");
}

const IMAGE_BG_CACHE_CAP: usize = 6;
const IMAGE_SLIDE_TEX_CACHE_CAP: usize = 6;

/// Pure formatter — host-testable.
pub fn format_dump_line(
    ts: u64,
    bg_len: usize, bg_cap: usize,
    tex_len: usize, tex_cap: usize,
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
```

`IMAGE_BG_CACHE_CAP` + `IMAGE_SLIDE_TEX_CACHE_CAP` are duplicates
of the canonical constants in hdmi.rs:244 / image_slide_tex.rs:54.
Acceptable duplication (both are pub const in their modules; I
could import them, but the dump's contract is "what cap the
runtime believes" which is independent of the cache's internal
constant). Will import from the canonical modules.

### D.5 Test

`renderer/tests/sigusr1.rs`:

- `format_dump_line_emits_all_keys_tab_separated()` — assert the
  output contains every key + the right tab count.
- `format_dump_line_zero_mem_snapshot_doesnt_panic()` — degenerate
  MemSnapshot (all zeros) emits valid line.
- (Linux-only, behind cfg) `install_handler_returns_ok_on_linux()`
  — smoke test that the handler installs without error.

Signal-delivery test is NOT included — sending a real signal to
the test process risks killing the test runner if pre-empted.

## E. Failure modes

### E.1 SIGUSR1 already used by another part of the codebase

- Verified: zero existing `SIGUSR` references in renderer/src/.
- Backend (Python) doesn't currently install SIGUSR1 handlers
  either (would be visible via grep).
- Future: if anything else wants SIGUSR1, we'd need a chained
  handler — solvable, but not a concern today.

### E.2 Signal-handler-during-handler (re-entry)

- `libc::signal()` resets the handler to SIG_DFL after the first
  signal on some POSIX implementations. On Linux glibc it
  re-arms automatically. Pi OS Lite is Linux glibc — safe.
- If a re-arm is missed (rare), only the first SIGUSR1 dumps;
  subsequent are SIG_DFL → terminate. **Mitigation:** use
  `sigaction()` with `SA_RESTART` instead of `signal()`. Audit
  marks this as a follow-up; for r38d the simpler `signal()`
  path is OK because re-arming is glibc-guaranteed.

### E.3 Inner loop never iterates (stuck on stdin)

- The IPC sidecar reads stdin line-by-line. If the backend never
  sends another op, the inner loop is blocked on stdin and the
  SIGUSR1 flag is never polled.
- **In practice:** the backend sends Advance every 33 ms (30 fps),
  so the flag is polled at least that often during normal
  operation. If the backend is wedged, the renderer's flag won't
  fire — but if the backend is wedged, the system already needs
  attention.

### E.4 Dump fires during a critical render-loop section

- The flag is polled at the TOP of each inner-loop iteration,
  outside any GL context begin/end. Dump runs in normal Rust
  execution context (eprintln, mem read) → no GL state
  contamination.

## F. Verification on FYS (post-deploy, code1's lane)

```
# 1. Confirm SIGUSR1 handler is installed
ps -ef | grep openmarquee-render | grep -v grep   # find PID

# 2. Send SIGUSR1
sudo pkill -USR1 -f openmarquee-render

# 3. Read journal
journalctl -u openmarquee-backend.service -n 50 --no-pager | grep -A 1 cache-dump
# expect ONE line per signal:
#   [cache-dump]  ts=...  image_bg_cache_len=N/6  ... cma_used_mb=NNN

# 4. Send 10 SIGUSR1s at 30-second intervals; collect 5 minutes of
#    dumps to characterize the cache-vs-swing relationship.
for i in $(seq 10); do sudo pkill -USR1 -f openmarquee-render; sleep 30; done
journalctl -u openmarquee-backend.service --since "5 minutes ago" --no-pager | grep cache-dump
```

## G. Open questions

### G.1 hdmi.rs touch authorization (BLOCKING)

Per QA's dispatch + standing rule, an hdmi.rs touch needs flag-
before-commit. I've sent the flag. Three response paths:
- (A) Authorize 6-LOC Session::cma_dump_cache_lens() → full r38d
- (B) Defer cache visibility → system-only r38d-Part-A now, cache
  visibility lands as r38d-Part-B
- (C) Different shape

### G.2 sigaction() vs signal()

`signal()` is simpler + glibc auto-rearms. Should we use
`sigaction()` with SA_RESTART for portability? My recommendation:
ship `signal()` for r38d; if we ever target a non-glibc Linux
(musl, kernel sans glibc), revisit.

### G.3 Dump format JSON vs TAB-separated key=value

Audit chose TAB-separated. Should it be JSON instead? Pros: easier
programmatic consumption. Cons: needs quoting / bracket hygiene,
harder to read in a terminal. My recommendation: TAB-separated
unless QA wants pipeable consumption.

### G.4 Per-entry byte estimates

Out of scope per §C.2. Should they be in r38d after all? Per-entry
byte introspection needs a hdmi.rs accessor per cache type. ~25
more LOC. My recommendation: defer to r38e if the cache numbers
alone don't disambiguate the swing source.

### G.5 SIGUSR1 also dumps GBM scanout BO state?

Currently the dump only covers session-owned caches. GBM scanout
BOs are not tracked as a counted resource; the closest accessor
would be inside hdmi.rs's commit_fb / lock_front_buffer paths.
That's an r38f scope if needed.

## H. Lane discipline

- This dispatch authorizes renderer/src/ changes.
- hdmi.rs touch is FLAGGED — won't commit without authorization.
- audit doc + sigusr1.rs + ipc_main.rs hook + test are
  code2-shippable today.
- Standard /tmp clone + cherry-pick to main for push.
- Sacred subagent review before commit.

## I. Push posture

- Pre-push hook applies (renderer/ changes).
- Standard NFS-wedge recovery pattern.
- ~150 LOC total est (audit doc + sigusr1.rs + ipc_main hook +
  test + maybe hdmi.rs accessor).

---

End of r38d audit.
