# r42 — V4L2 EXPBUF fd-leak (the §F.1 deferred from r41)

**Author lane:** code1 (closing the r38b → r40 → r41 → r42
allocator-defense arc).

**Scope:** the single SUSPECT site my r41 sacred subagent
surfaced + parked because it was structurally identical to
r41 Fix 2 but for a different resource type. r42 ships the
fix.

**Origin/main HEAD at fix time:** `30039bd` (my r41).

---

## §1 — Fix: `allocate_buffers` EXPBUF loop cleanup

### Location

`renderer/src/v4l2.rs:1071-1102` (pre-fix) — the EXPBUF loop in
`allocate_buffers`. Inside the `dir == QueueDirection::Capture
&& capture_buffer_type == CaptureBufferType::DmaBuf` branch.

### Failure mode

```
// REQBUFS + QUERYBUF + mmap completed for all buf_idx 0..N-1
// above. Now per-iteration:
let mut fds: Vec<RawFd> = Vec::with_capacity(allocated_count);  // line 1071
for buf_idx in 0..allocated_count {
    // V4l2Exportbuffer struct setup ...
    vidioc_expbuf(inner.fd(), &mut expbuf)?  // line 1089 -- FAILS on iter N>0
    // ?-bubble: fds 0..N-1 already pushed at line 1100, never
    // closed. inner.capture_dmabuf_fds assignment (line 1102)
    // only runs after loop completes.
    //
    // Per-leak: one open dma_buf fd per pushed iteration.
    // DecoderInner::drop only closes self.capture_dmabuf_fds
    // which stays empty on this error path.
    if expbuf.fd < 0 { return Err(...) }  // line 1094-1099 -- same shape
    fds.push(expbuf.fd);
}
inner.capture_dmabuf_fds = fds;  // line 1102 -- only reached on full success
```

### Trigger surface

- Only fires on V4L2 H.264 decode + DmaBuf capture mode
  (`capture_buffer_type == CaptureBufferType::DmaBuf`, which
  requires `OPENMARQUEE_RENDERER_DMABUF=1` env var).
- `allocate_buffers` is called once per `Decoder` session — a
  decoder is constructed per VideoSlide encountered. Per-session
  leak that compounds across video-session retries.
- `vidioc_expbuf` mid-loop failure is rare in practice (the
  kernel either has all the buffers or none after REQBUFS), but
  ENOMEM under severe FD pressure or a misbehaving kernel
  driver could trip it.

### FYS-relevance

**None.** FYS has no VideoSlides + no DMABUF env var. Real bug
for any deployment running V4L2 video with DMABUF mode enabled.

### Sibling-pattern reference

The r41 `sdf_atlas_gl.rs:upload_all` cleanup_partial closure
(SHA `30039bd`, lines 61-67):

```rust
let cleanup_partial = |gl: &glow::Context, out: &mut Vec<MsdfAtlasGl>| unsafe {
    for entry in out.drain(..) {
        gl.delete_texture(entry.tex);
    }
    gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
    gl.bind_texture(glow::TEXTURE_2D, None);
};
```

r42 mirrors this for `RawFd`:

```rust
let cleanup_partial_fds = |fds: &mut Vec<std::os::fd::RawFd>| {
    for fd in fds.drain(..) {
        unsafe { libc::close(fd); }
    }
};
```

Plus the bare `?`-bubble at line 1089 converts to an `if let
Err(e) { cleanup_partial_fds(...); return Err(...) }` shape
(same as r40 Fix 1+2+3 and r41 Fix 1). The explicit `if expbuf
.fd < 0 { return Err... }` branch at line 1094 also calls
cleanup_partial_fds before propagating.

LOC: ~30 (closure def + 2 call sites + the bare-`?` conversion).

### Safety notes

- `libc::close(fd)` is `unsafe` because closing an arbitrary
  RawFd can be UB if another thread is using it (TOCTOU). In
  this code path each fd came from a successful VIDIOC_EXPBUF
  on the prior iteration, is owned exclusively by the local
  `fds: Vec<RawFd>`, and has NOT yet been handed off to
  `inner.capture_dmabuf_fds` or any other consumer. No
  concurrent use possible.
- `libc::close` returns `c_int` (0 success, -1 + errno on
  failure). We don't check the return because errors on close
  during error-path cleanup are not actionable (process is
  unwinding) and per the r41 cleanup_partial precedent, the
  closure shape doesn't propagate cleanup failures.

---

## §2 — Pattern alignment

r42 is the third instance of the cleanup_partial closure pattern,
extending the cumulative allocator-defense arc:

- **r38b** (`5ac3ca2`) — `cleanup_static` closure pattern
  established (hdmi.rs:4245-4251); fixed transition-closure
  scanout-target leak (16 MB bake-FBO).
- **r40** (`f14c3b1`) — 3 match-arm cleanup fixes for NV12
  texture + EGLImage allocation paths.
- **r41** (`30039bd`) — `cleanup_partial` closure shape applied
  to upload loops (`sdf_atlas_gl.rs:upload_all`); also fixed
  the cap_tex create-fail leak in `capture_fullres_transition_mid_to_png`.
- **r42** (this commit) — `cleanup_partial_fds` for RawFd. Same
  closure shape, adapted to `libc::close` instead of
  `gl.delete_texture`.

The pattern is now consistent across the renderer: any multi-
step alloc/init sequence with mid-sequence `?`-bubbles in the
allocator-defense scope uses either a match-arm explicit-cleanup
or a cleanup_partial closure invocation before propagating.

---

## §F — Adjacent sweep findings (§F.new)

**Zero new sites surfaced.** The sacred subagent re-scanned
`/tmp/r42-work/renderer/src/` for:

- Other `Vec<RawFd>` accumulators with mid-loop `?`-bubbles.
- Other `OwnedFd` / `BorrowedFd` accumulators with similar shapes.
- Other `libc::close` / `nix::unistd::close` paths missing on error.
- Other V4L2 ioctl chains that leave file descriptors / mmap
  regions / kernel resources unreleased on mid-chain error.

Result: **zero** new SUSPECT or CONFIRMED-LEAK sites. The cumulative
r38b → r40 → r41 → r42 sweep is now complete across the renderer.

### §F.1 — Verified-clean sites checked in the r42 sweep

- **MMAP loop in same function (`allocate_buffers`, lines ~999-1043).**
  Accumulates `Vec<Vec<MmapRegion>>`. `MmapRegion` has Drop that
  calls `munmap` — RAII handles mid-loop failure. SAFE.
- **`Frame::dmabuf_fd`.** Borrowed-non-owned (looked up from
  `inner.capture_dmabuf_fds[idx]`). No ownership; nothing to
  close on Err. SAFE.
- **`ipc_main.rs` inherited fd wrapping.** Wraps the kernel-
  inherited fd in `std::fs::File` immediately on construction;
  `File::drop` closes. SAFE.

### §F.2 — Arc closure note

The r41 audit doc §F.3 predicted "All other GL allocator paths
verified clean across cumulative r38b + r40 + r41 sweep." r42's
sweep also covered non-GL allocators (RawFd / mmap / V4L2
ioctls) and confirmed the prediction holds. **The renderer's
`?`-bubble allocator-leak hypothesis space is fully audited.**

---

## §G — Open questions for qarl

**None expected.** This is the last known site from the
cumulative r38b → r41 sweep. Fix mirrors the r41 cleanup_partial
pattern verbatim (modulo resource type). No design decisions.

(Nice-to-have flagged: `allocate_buffers` itself could be
factored to a builder-style API where the fds Vec is built into
an RAII guard type that closes on Drop. That would eliminate
the entire cleanup_partial pattern for this site. Out of r42
scope; a future "RAII wrappers for V4L2 resources" refactor
candidate.)

---

## Push posture

Single commit. Pre-push hook will run cargo test +
cross-compile; both should pass (fix adds an Err-path closure
+ converts a bare `?` to an explicit `if let Err`; behavior on
the success path is unchanged).

— jimmy:openmarquee-code1 (lane: r38b→r42 allocator-defense
arc, this commit closes it)
