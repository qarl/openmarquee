# r48 — V4L2 OUTPUT buffer rotation (the perf-night-r5 free-list refactor)

**Author lane:** code1 (closing the symptom-chase loop from
r46/r46.1/r46.2/r46.3/r46.4).

**Scope:** fix the root cause that r46/.../r46.4 chased above this
layer: `v4l2::Decoder::feed()` always used `buf_idx = 0` (single
OUTPUT buffer). Back-to-back feeds raced `drain_output_quiet`
against bcm2835-codec's decode pipeline → VIDIOC_QBUF OUTPUT
EINVAL on the second feed when the kernel still owned slot 0.
The perf-night r5 comment at `video_decode.rs:170-178`
(2026-05-26) explicitly anticipated this refactor.

**Origin/main HEAD at fix time:** `b3f932d2` (my r46.4).

**Recommendation (preview):** ship the OUTPUT free-list. Closes
the text-over-video EINVAL chase. Preserves r46.2/r46.3/r46.4
fixes. Demo + non-video paths unchanged. 3 subagent-flagged
hardening fixes folded in.

---

## §A — Root cause

Pre-r48 `feed()`:
```rust
let buf_idx = 0u32;  // SINGLE-SHOT-SAFE only
```

The docstring `v4l2.rs:1357-1364` was honest about the limitation:
> Piece 3's real driver loop will need a free-list (track which
> OUTPUT indices the kernel has handed back via DQBUF) and reject
> feed() with EBUSY if every OUTPUT buffer is in flight.

`drain_output_quiet` was the intended reclamation, but on
bcm2835-codec at 1280x720 with real H.264 content, the kernel's
decode latency reliably exceeded the time between two `feed()`
calls — and `drain_output_quiet` returned EAGAIN → next feed
QBUF'd slot 0 a second time → EINVAL.

The perf-night r5 comment at `video_decode.rs:170-178`
(2026-05-26) named this as "fragile" and anticipated the free-
list. r46/r46.1/r46.2/r46.3/r46.4 then chased the SURFACE
symptoms:
- r46.2: memoize bg-video decoder across BeginSlide (CMA)
- r46.3: STREAMOFF/STREAMON to clear capture_drained → rejected by
  bcm2835 with EINVAL on subsequent OUTPUT QBUF
- r46.4: replace STREAMOFF/STREAMON with V4L2_DEC_CMD_START
  (correct per V4L2 spec, but doesn't address the OUTPUT-side
  single-buffer race)

r48 fixes the actual race.

---

## §B — Fix shape

### B.1 — Add `free_output_slots: VecDeque<u32>` to DecoderInner

Tracks which OUTPUT buffer indices are userspace-owned (vs.
kernel-owned during decode). Every index is in EXACTLY ONE of
{deque, kernel} at any moment.

### B.2 — Pool lifecycle

- `Decoder::open`: empty deque (no buffers allocated yet).
- `allocate_buffers(QueueDirection::Output, N)`: seed deque
  with `(0..N).collect()`. All N indices userspace-owned.
- `feed()`: `pop_front()` → QBUF → slot is now kernel-owned (not
  in deque).
- `drain_output_quiet()`: DQBUF success → `push_back(buf.index)`.
  Slot is back in userspace. FIFO order so a freshly returned
  slot rotates to the back, giving kernel maximum time to decode
  before we re-use it.
- `stop_streaming_quiet()` on OUTPUT STREAMOFF: kernel returns
  all queued OUTPUT buffers. Repopulate deque with all N indices.

### B.3 — feed() empty-pool handling (subagent-caught hardening)

If `free_output_slots` is empty after the initial drain, do up to
5 more drains spaced 2ms apart (mirrors the perf-night-r5
next_frame EAGAIN sleep pattern; total budget 10ms). If pool is
still empty after that, return an explicit error — the decoder is
genuinely wedged.

Why bounded retry: returning an error immediately on transient
back-pressure would propagate as a hard slide/transition failure
via `bake_video_slide_to_current_fbo`. The 10ms wait lets the
kernel catch up before declaring the decoder dead.

### B.4 — Error paths (subagent-caught hardening)

- **Validation error** (NAL larger than plane): `push_front` —
  the slot is provably clean (never reached the kernel), so a
  retry on the same idx is safe + deterministic for diagnostics.
- **QBUF failure**: `push_back` — the slot may have a transient
  kernel-side issue; rotating it to the back of the queue means
  the next feed tries a DIFFERENT slot. `push_front` would re-pop
  the same bad slot and wedge the decoder on a 1-of-N persistent
  error.

---

## §C — CMA budget preservation

Zero new allocation. Free pool is a `VecDeque<u32>` with capacity
N (typically 4) — ~32 bytes. Existing 4-buffer OUTPUT pool
allocated in `allocate_buffers` is unchanged.

- Pre-r46 CMA: ~211 MB stable
- r46.2-r46.4 measured: 180-247 MB across cycles
- r48 expected: identical to r46.4 (no allocation change)

---

## §D — Subagent review (sacred)

Pre-commit review surfaced 3 WARNs + 3 NITs. **No BLOCKERs.**

### WARNs — all FIXED in v2 before push

1. **feed() empty-pool error propagated as hard transition-
   abort**. Fixed: bounded retry loop with 5×2ms drains before
   declaring decoder wedged. Matches the existing perf-night-r5
   EAGAIN sleep pattern.

2. **QBUF-failure `push_front` could wedge on a transiently bad
   slot**. Fixed: changed to `push_back` so a bad slot rotates
   past for the next feed. Validation-error path stays
   `push_front` (slot is provably clean).

3. **`r48_streamoff_repopulates_pool` test name didn't match
   body**. Fixed: renamed to `r48_feed_consumes_pool_slots` and
   tightened docstring to say what it actually verifies. Note
   STREAMOFF-mid-life pool reset coverage is via the existing
   `drop_then_reopen_clean` (any pool leak would EBUSY REQBUFS
   on re-open).

### NITs — documented or deferred

- Fixed-size `[u32; 8]` plane array would panic if num_planes > 8.
  V4L2 hard cap is 8 (V4L2_MAX_PLANES); bcm2835 H.264 OUTPUT uses
  1 plane. Theoretical, no live blast radius. Pre-existing
  pattern.
- `drain_output_quiet`'s borrow scope is OK under NLL but fragile
  to future edits. Documented; refactor not in r48 scope.
- `r48_feed_rotates_through_pool_back_to_back` is a smoke test,
  not a deterministic regression-catcher (depends on kernel
  timing). Comment updated to say so; turning it into a true
  regression test would need a `#[cfg(test)]` no-drain feed
  variant — not in r48 scope.

### Verified clean by subagent

- Free-list invariant: every slot is in EXACTLY ONE of
  {deque, kernel-owned} across all paths
- `buf.index` set by kernel on DQBUF success (V4L2 UAPI contract)
- Lock contention: `drain_output_quiet` releases before `feed`/
  `next_frame` re-lock
- No other code paths assume `buf_idx = 0` for OUTPUT
- No regression on r46.2 CMA budget
- No regression on r46.3 first-play scanout fix
- No regression on r46.4 wrap-via-DEC_CMD_START fix
- Frame::drop CAPTURE-side r46.4 fix unaffected (separate queue)

---

## §E — Test coverage

4 new Linux-gated tests (skip cleanly without /dev/video10):

| Test                                          | What it verifies                                 |
|-----------------------------------------------|--------------------------------------------------|
| `r48_allocate_output_seeds_free_pool`         | Pool empty before, populated with [0..N) post-allocate |
| `r48_feed_rotates_through_pool_back_to_back`  | N back-to-back feeds don't surface EINVAL (smoke test) |
| `r48_feed_oversized_nal_restores_pool_depth`  | Validation-error path doesn't leak slot; push_front preserves head |
| `r48_feed_consumes_pool_slots`                | feed() actually consumes slots (sanity) |

Existing test `decode_test_fixture_320x240` still passes
unchanged — single-shot full-fixture-in-one-call pattern works
the same way (one feed → drain → one DQBUF cycle).

Host (non-Linux) tests still pass (7/7 confirmed: fourcc,
struct layouts, c_str decode, quantization checks).

---

## §F — Sweep findings (§F.new)

[Filled post-final-subagent-review if any second pass surfaces
new items.]

---

## §G — Push posture

Single commit. Pre-push hook runs cargo test + cross-build; both
pass. Standard /tmp/openmarquee-main push per
[[feedback_deploy_from_main_not_code2]]. Deploy via the proven
Path D pattern (stop wifi-watchdog → stop backend → unthrottled
rsync → atomic mv → start → restore wifi-watchdog).

---

## §H — Verification plan (post-deploy, QA-driven)

Required before tagging CLOSED:
- ≥10 consecutive cycles of the video-test text-over-video slide
- Zero `VIDIOC_QBUF OUTPUT: EINVAL` in journal
- Zero `feed sample N failed` errors
- Zero "holding last frame" warnings for the text-over-video
  slide
- CMA stable across the 10+ cycles (no leak)
- Demo playlist (non-video) still clean (no regression)
- First-play scanout still works (bo=2/fb=2 on initial slide)

If any of those fail: revert schedule to Demo, ping back with
diagnosis. The Path D deploy is reversible (rsync the prior
binary back).

— jimmy:openmarquee-code1 (lane: r48 closes the r46.x EINVAL chase)
