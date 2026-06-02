# r39 — VideoSlide doc hygiene sweep

**Author lane:** code1 (renderer-perf has been my main lane;
this is a rest-cycle hygiene pass).

**Scope:** verify and fix the primary `VideoSlide.duration_ms`
docstring drift qarl flagged, sweep adjacent video-doc surfaces
for related drift, audit-doc the findings. Doc-only.

**Conclusion (TL;DR):** 2 inner-repo drift sites found, both
fixed in this commit. 0 outer-repo relays needed — `SYSTEM_SPEC.md`
§5.10 already correctly describes the looping behavior; the
inner-repo docstrings were the outliers.

**Origin/main HEAD at sweep time:** `5ac3ca2` (my r38b).

---

## §1 — The dispatch's primary find: verified

qarl's read this morning surfaced
`backend/openmarquee/content/__init__.py:574-577` (the dispatch
cited `:521-528`; that was off by ~50 lines from current HEAD,
but the file + paragraph match). The pre-r39 text:

> `duration_ms` is informational: the playback engine reads the
> actual runtime from the file. Keeping it present so the schema
> parallels TextSlide / ImageSlide and a single ContentItem union
> works.

**This is wrong.** Verified against current HEAD `5ac3ca2`:

| Cite | File:line | Behavior |
| --- | --- | --- |
| dispatch said `playback.py:1013` | `playback.py:1036` | `duration_ms = int(item.duration_ms)` — direct read from the model |
| dispatch said `playback.py:1048` | `playback.py:1071` | `end_at = t0 + duration_ms / 1000` — slot ends per user-set duration |
| dispatch said `api.py:555-573` | `api.py:595-624` | `VideoSlide(**payload.model_dump(...))` — no auto-compute from mp4 |
| dispatch said `hdmi.rs:6440-6447` | `hdmi.rs:6744-6745` (in `bake_video_slide_to_current_fbo` body starting `:6703`) | `if *next_sample_idx >= samples.len() { *next_sample_idx = 0; }` — FYS bug 3 loop fix |

Dispatch line numbers were all stale (probably from a code2 HEAD
or an older snapshot) but the substance is correct at every site.
The actual contract is the one qarl identified: **operator's
`duration_ms` sets slot length; renderer loops samples back to 0
if the clip is shorter than the slot.**

---

## §2 — Sweep findings

Audited every place the codebase mentions VideoSlide or video
duration. Per-claim verdict:

### §2.1 Pydantic model docstrings (backend/openmarquee/content/__init__.py)

| Class | Lines | Verdict | Note |
| --- | --- | --- | --- |
| TextSlide | 357-374 | CORRECT | Describes duration_ms as a "slide-level field"; no semantic claim |
| ImageSlide | 531-539 | CORRECT | No duration claim |
| **VideoSlide** | **574-577** | **DRIFT — FIXED** | The dispatch's primary find |
| StreamSlide | 599-614 | CORRECT | Explicitly: "`duration_ms` is the fixed slot length, like every other slide type" |
| WebSlide | 660-674 | CORRECT | Explicitly: "`duration_ms` is the fixed slot length, like every other slide type" |

**Both StreamSlide AND WebSlide explicitly say "fixed slot
length, like every other slide type" — which contradicts
VideoSlide's pre-r39 "informational" claim.** The drift was
asymmetric only on VideoSlide.

### §2.2 Rust schema-mirror doc (renderer/src/content.rs)

| Lines | Verdict | Note |
| --- | --- | --- |
| **427-431** | **DRIFT — FIXED** | Same stale claim "duration_ms is informational per the Python schema -- the playback engine reads actual runtime from the file" propagated to the Rust mirror. The same sentence pivots to "but the renderer honors it for hold-time hints and the reel driver's pass_ms gate" which is correct — the rewrite keeps that part. |

### §2.3 Renderer doc-comments (`//!` headers + `///` blocks)

| File:line | Verdict | Note |
| --- | --- | --- |
| `hdmi.rs:2954-2972` (`render_video_slide_in_session`) | CORRECT | Explicitly describes the looping behavior — "The video LOOPS during the hold — when `next_sample_idx` wraps past `samples.len()`, `reprime_video_decoder_for_loop` re-feeds the SPS+PPS+IDR primer ... a 2s video in a 5s hold plays through 2.5x" |
| `hdmi.rs:3748` (`paint_and_present_one_video_slide_frame` docstring) | CORRECT | Per-Advance-tick comment, no duration claim |
| `hdmi.rs:6683-6697` (FYS bug 3 inline comment) | CORRECT | "A video clip shorter than the slide's slot must replay for the full slot, not stall" |
| `mp4_demux.rs` header | CORRECT | Describes demuxer scope (baseline H.264), no duration claim |
| `video_decode.rs` header | CORRECT | Describes V4L2 decoder state cache, no duration claim |
| `ipc_main.rs` (5 VideoSlide refs) | CORRECT | All about marker handling / cache.load skip / Mp4Demuxer-per-VideoSlide; no duration claim |
| `main.rs` (VideoSlide thumbnail handling) | CORRECT | About asset.png contract |
| `hdmi_logic.rs:2907-2909` | CORRECT | About NV12 cover-fit shader |

### §2.4 Backend non-model files

| File | Verdict | Note |
| --- | --- | --- |
| `playback.py` | CORRECT | Reads `item.duration_ms` directly; computes `end_at`. The behavior matches the dispatch's claim. |
| `api.py` | CORRECT | POST /videos uses payload's `duration_ms` verbatim; no auto-compute. |
| `rendering/rust_renderer.py` | CORRECT | About marker handling (`_UNSUPPORTED_SLIDE_WIRE_MARKERS`) + Capture-VideoSlide-TBD note; no duration claim |
| `content/storage.py`, `flock_sync.py`, `seed.py`, `dependencies.py`, `_body_cap_middleware.py` | CORRECT | Various VideoSlide references for storage / sync / fixture seed / DI / body-cap; no duration claims |
| `seed_assets/README.md` | CORRECT | Asset README, no duration claim |

### §2.5 Backend tests

| File | Verdict | Note |
| --- | --- | --- |
| Multiple `backend/tests/...` files | CORRECT | Tests exercise VideoSlide through `VideoSlide(...)` constructors, the model's `duration_ms=N` default override, and round-trip schema serialization. None make doc claims about duration semantics. |

### §2.6 Inner-repo docs/

| File | Verdict | Note |
| --- | --- | --- |
| `docs/STREAM_VLC_PROPOSAL.md` | CORRECT | StreamSlide context, not VideoSlide; describes stream slot semantics matching `StreamSlide.duration_ms` cap (24h) |
| `docs/v4l2-decode.md` | CORRECT | Implementation notes for V4L2; no duration claim |
| `docs/renderer-rewrite-plan-rust.md` | CORRECT | Architectural description; VideoSlide refs about decoder pipeline, not duration semantics |

---

## §3 — What r39 ships

Two file edits, both inner-repo:

1. `backend/openmarquee/content/__init__.py:574-583` — rewrite the
   `duration_ms` paragraph in the `VideoSlide` docstring. Drops the
   "informational" / "actual runtime from the file" claim; adds the
   actual contract (operator-set slot + looping + SPEC + code
   citations). Keeps the existing "schema parallels..." framing
   implicit (the new text explicitly names the sibling types
   ContentItem already unifies).
2. `renderer/src/content.rs:427-431` — rewrite the `VideoSlide`
   doc-comment to mirror the Python rewrite. Drops the same stale
   "informational" claim; keeps the existing accurate "renderer
   honors it for hold-time hints" framing and expands with the
   FYS-bug-3-fix loop note.

No code changes (Python or Rust source-of-truth behavior unchanged).
Both edits are pure comment text.

### Why both files

The Rust schema mirror in `renderer/src/content.rs` propagated the
same Python claim that turned out to be wrong. Fixing only the
Python side would leave the Rust comment as a stale "per the Python
schema" reference — confusing for anyone reading the renderer.

### Subagent review

Independent subagent verified:
- The two rewrites describe code behavior accurately (cited
  playback.py:1036/1071 + api.py:595-624 + hdmi.rs:6683-6697
  match real source).
- No other stale "duration_ms is informational" / "actual
  runtime from the file" claims exist elsewhere in the repo
  (grep across all .py/.rs/.md/.js/.ts).
- The sibling slide types' "fixed slot length" framing is
  internally consistent and matches what VideoSlide now claims.

---

## §F — Outer-repo candidates (to relay to admin Jimmy)

**None.** `SYSTEM_SPEC.md` §5.10 (read at relay time):

> **Looping:** when the video is shorter than the slide's duration,
> playback loops the video; when longer, it truncates at the slide's
> end.

This is **correct** and matches both the actual implementation AND
the rewritten inner-repo docstrings. The spec already documented
the canonical behavior; the inner-repo Pydantic + Rust docs had
drifted away from the spec.

`IMPLEMENTATION_PLAN.md` was also scanned; its VideoSlide refs are
phase-history descriptions (commits landed, milestones, etc.) with
no duration-semantic claims to update.

**Recommendation to admin Jimmy: NO outer-repo edits needed for r39.**

---

## §G — Open questions for qarl

**None.** This is hygiene; the rewrite makes the inner-repo doc
match `SYSTEM_SPEC.md` §5.10 + the actual code. No decisions
needed.

(If anything, the rewrite could surface a one-line clarification
in `SYSTEM_SPEC.md` §5.10 — the spec says "truncates at the
slide's end" when the video is longer, but doesn't explicitly
clarify what happens to the audio-stripped / decoder state. That's
a NICE-TO-HAVE not a drift, and well below this dispatch's
hygiene bar — flagging only for completeness.)

---

## Push posture

Single commit; doc-only; no cross-build required (no .rs source
changes — only `//` comment text in `content.rs` which compiles
identically). Standard /tmp worktree push.

— jimmy:openmarquee-code1 (lane: rest-cycle hygiene)
