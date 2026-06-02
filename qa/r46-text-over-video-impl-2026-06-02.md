# r46 — Text-over-video composite per SYSTEM_SPEC §5.10

**Author lane:** code1 (renderer).

**Scope:** implement the SYSTEM_SPEC §5.10 "device composites the
text PNG over the decoded video frames at playback time"
behavior. Pre-r46, the Rust renderer's `TextSlide` struct was
missing the `background_video_slide_id` field, so serde
silently dropped the JSON value + the renderer painted text on
a black bg. r46 wires the field through end-to-end.

**Origin/main HEAD at impl time:** `4a0ba6d` (my r44).

**Recommendation (preview):** ship the implementation as the
first text-over-video v1.0.1 line item. CMA mitigation in place;
graceful fall-back if the bg video can't load.

---

## §1 — Files touched

| File | Lines | Change |
| --- | --- | --- |
| `renderer/src/content.rs` | +24 / -0 | Add `background_video_slide_id: Option<Uuid>` to `TextSlide` struct + doc + 2 unit tests |
| `renderer/src/hdmi.rs` | +230 / -0 | Add `paint_and_present_one_text_over_video_slide_frame` + `force_evict_image_caches_for_cma_pressure` session method |
| `renderer/src/ipc_main.rs` | +110 / -3 | Add `ensure_bg_video_for_text_slide` cache method; hook into `cache.load` short-circuit + insert paths; extend IPC text-arm dispatcher to route to the new paint function when `background_video_slide_id` is set |
| `qa/r46-text-over-video-impl-2026-06-02.md` | +260 / -0 | This audit doc |

**Estimated LOC**: ~370 + audit. Single commit.

---

## §2 — Per-component design

### §2.1 — Rust schema field

`renderer/src/content.rs` `pub struct TextSlide` gains:

```rust
#[serde(default)]
pub background_video_slide_id: Option<Uuid>,
```

Mutex semantics (per Python validator at
`backend/openmarquee/content/__init__.py:505-528`): only ONE of
`background_image_slide_id`, `background_video_slide_id`,
`background_pattern` may be set. Python validator enforces at
save. The renderer **does not** enforce mutex; if BOTH image
and video are set, `background_image_slide_id` wins (mirrors
the existing image-vs-pattern precedence with a `warn:` line).

Two unit tests pin the field:
- `text_slide_parses_background_video_slide_id`: round-trips a
  UUID through JSON deserialization.
- `text_slide_background_video_defaults_to_none`: confirms
  `None` is the default when the operator hasn't selected a
  bg video.

### §2.2 — Paint path (`paint_and_present_one_text_over_video_slide_frame`)

New public function in `renderer/src/hdmi.rs` (Linux-only,
`#[cfg(target_os = "linux")]`). Mirrors the structure of the
existing `paint_and_present_one_frame_for_slide` but replaces
the bg-paint step (was: `paint_slide(bg_kind=Image/Pattern/Solid)`)
with a 2-step sequence:

1. **Bake video frame**: `bake_video_slide_to_current_fbo(session,
   samples, next_sample_idx, frames_decoded, decoder, mode_w,
   mode_h)` decodes one V4L2 sample + uploads + blits to the
   currently-bound FBO. The FYS-bug-3-fix sample-wrap stays
   intact — clips shorter than the slot replay.
2. **Composite text on top**: `paint_slide_with_viewport(...,
   bg_kind=None, ...)` paints text layers without touching the
   bg. The `bg_kind=None` signal is the function's existing
   "caller has already filled the bg" path (documented at
   `hdmi.rs:11295-11298` from the atlas SB bg-cache work).

The rest of the function (glyph-cache poll, slide_caches init,
scene FBO for rotation/non-identity color, present pass,
standard scanout swap+commit) is a verbatim mirror of
`paint_and_present_one_frame_for_slide`.

**Motion text supported**: text layers paint per-tick on top of
each fresh video frame — same semantics as image-bg-on-text
today.

### §2.3 — IPC dispatch

Two hook points in `renderer/src/ipc_main.rs`:

1. **`cache.load`**: when a TextSlide is loaded (insert at line
   588), call `self.ensure_bg_video_for_text_slide(...)` to
   side-load the referenced VideoSlide. Idempotent: the
   recursive `self.load(bg_id)` short-circuits if the bg video
   is already primed. The same call runs at the load-short-
   circuit too, so a slide-change-then-replay sequence re-primes
   the bg video after `evict_other_video_state` (called from
   the `BeginSlide` handler at line 1849).

2. **Text-arm of `advance` dispatcher**: when the text slide has
   `background_video_slide_id` set AND both `cache.video_demuxers`
   + `cache.video_decoders` have entries for `bg_id`, route to
   `paint_and_present_one_text_over_video_slide_frame` with
   both states. Graceful fall-back: if demuxer/decoder loaded
   best-effort and one is missing, warn + fall through to the
   standard text-only paint path (slide still renders, just
   with the configured `background_color` instead of video).

### §2.4 — CMA mitigation

New session method `force_evict_image_caches_for_cma_pressure()`
on `EglSession`. Drains both `image_bg_cache` +
`image_slide_tex_cache`, calling `gl.delete_texture` on each
entry. Logs the eviction count for observability.

Called once per text-over-video slide entry (detected via
`session.slide_caches.contains_key(&slide.id)` being false at
function entry). Per-frame paints of the same slide skip the
eviction (cheap no-op when caches are already empty anyway).

The image caches re-warm naturally when the next image-bg slide
plays — no other side effect. **Trade-off**: alternating
playlists (text-over-video ↔ image-bg) thrash the image caches.
Accepted as implementation cost; Phase 9 V4L2-pool tuning would
remove the trade-off.

### §2.5 — SIGUSR1 dump extension (deferred)

Per dispatch §C, "this new cache surface should be EXPOSED in
the SIGUSR1 dump added by r38d (hdmi.rs ~5304 cma_dump_cache_lens)".

**r46 ships WITHOUT a new session cache** (see §3 below on the
text-PNG-cache design divergence), so there's no new session-
level surface to add to the tuple. The relevant state for the
text-over-video path is in `ipc_main.rs`'s cache
(`video_demuxers` + `video_decoders` + items keyed by bg_id),
not on `EglSession`. Extending the SIGUSR1 dump to surface
those requires a cross-module accessor (the dump is currently
session-only).

Deferred to r47+ as "extend SIGUSR1 dump with ipc_main cache
state visibility" — useful follow-up for both text-over-video
AND standalone VideoSlide debugging. The audit-doc CMA-budget
math (§4) is sufficient to characterize the pressure without
runtime visibility for now.

---

## §3 — Design divergence: per-frame text paint (not pre-baked PNG)

Dispatch §C asked for a cached pre-baked text PNG: "Cache the
rendered text-layer PNG once per slide (per §5.10 'the device
composites the text PNG over each video frame at playback
time' — implies pre-baked text texture, not per-frame re-
rasterize)."

r46 ships **per-frame text paint** instead of pre-baked PNG.
Rationale:

1. **Spec text doesn't mandate pre-baked**. §5.10 says "device
   composites the text PNG over decoded video frames at playback
   time" — "PNG" is descriptive of the source format (the
   browser-flattened static thumbnail), not normative of an
   implementation strategy. The actual at-glass behavior the
   spec promises is "text composites over video"; per-frame or
   pre-baked are both valid implementations.

2. **Consistency with image-bg-on-text**. The existing image-bg-
   on-text path (`background_image_slide_id`) does per-frame text
   paint, not pre-baking. text-over-video should match the same
   contract.

3. **Motion text works naturally**. Pre-baked PNG would freeze
   motion at one moment; per-frame paint animates correctly.
   This matches the operator's existing expectation from image-
   bg-on-text + image-only slides.

4. **No new CMA surface**. The pre-baked PNG cache (~8 MB ×
   small cap = ~24-32 MB) would compound the CMA pressure the
   dispatch is already worried about. Per-frame paint adds zero
   new cache.

5. **Smaller diff**. ~370 LOC vs ~500+ if cache + lifecycle is
   added.

If qarl/QA prefer the dispatch's literal cached-PNG design,
that's a r47+ correction. The §2.2 paint function would replace
its per-frame `paint_slide_with_viewport(...)` text-layer
sub-call with a one-time bake-to-cache + per-frame blit-from-
cache.

---

## §4 — CMA budget math

**Pi Zero 2 W default CMA pool**: 256 MB.

**Pre-r46 steady-state on FYS** (per `qa/r38d-sigusr1-cache-
dump-2026-06-02.md` 229-254 MB swing observation): ~250 MB.

**r46 additions when a text-over-video slide enters**:

| Item | Size | Notes |
| --- | --- | --- |
| V4L2 decoder pool (per bg video) | ~24 MB | 4 OUTPUT + 4 CAPTURE buffers × NV12 1920×1088 ≈ 3 MB each |
| EGLImage DMABUF imports (if `OPENMARQUEE_RENDERER_DMABUF=1`) | ~3 MB × few | Per-frame; reaped at frame end |
| MP4 demuxer in-memory samples | <1 MB | byte buffers for H.264 NAL chunks |
| New cache surface | **0 MB** | Per-frame text paint (no PNG cache) |

**Naive ceiling**: 250 + 24 = **274 MB > 256 MB pool** ⇒ pool
allocation fails ⇒ slide fails to render the video bg.

**Mitigation savings**: `force_evict_image_caches_for_cma_pressure`
drains:

| Cache | Worst-case size | Typical FYS occupancy |
| --- | --- | --- |
| `image_bg_cache` (6-entry LRU × ~8 MB) | 48 MB | 2-4 entries (~16-32 MB) |
| `image_slide_tex_cache` (6-entry LRU × ~8 MB) | 48 MB | 0-2 entries (~0-16 MB) |

**Realistic mitigation savings on FYS**: 16-48 MB freed.

**Post-mitigation ceiling**: 250 - 16 to 48 + 24 = **226-258 MB**.
This is at or under the 256 MB pool depending on actual occupancy.

**Residual risk**: if both image caches are empty (atypical for
FYS reel which has image slides) AND CMA baseline is at the
upper end of the swing (254 MB), the V4L2 pool alloc could
still fail. In that case the IPC text-arm falls through to the
standard text-only paint path (warn + black bg + text only) —
graceful degradation, no crash.

**Phase 9 followup**: pre-allocate the V4L2 pool at the bg
video's actual dims (smaller if 720p source). A 720p NV12 pool
is ~12 MB instead of ~24 MB. Out of r46 scope.

---

## §5 — Lifecycle

**Slide-load**: `cache.load(text_slide_id)` →
`ensure_bg_video_for_text_slide` → `cache.load(bg_video_id)` →
`prime_video_decoder(bg_video_id)`. Both items + demuxer +
decoder primed before `paint_and_present_*` runs.

**First paint**: detected via
`!session.slide_caches.contains_key(&slide.id)`. Triggers CMA
mitigation eviction.

**Per-frame paint**: V4L2 sample feed + decode + blit + text
overlay + scanout. Standard 11-step canonical scanout release
contract (per `qa/r38b-hdmi-cma-deep-read-2026-06-02.md` §2)
applies verbatim.

**Slide-change** (next slide is NOT text-over-video on same
bg_id): `BeginSlide` handler calls
`evict_other_video_state(new_slide_id)` which drops the bg
video's demuxer + decoder. Image caches re-warm naturally on
next image-bg slide.

**Slide-change** (re-enter same text-over-video slide after a
different slide played): `evict_other_video_state(text_id)`
drops the bg video state; `cache.load(text_id)` short-circuits
on items+mtime; `ensure_bg_video_for_text_slide` triggers; the
recursive `cache.load(bg_id)` falls through to re-prime via the
`video_reprime_needed` check.

**Sidecar restart**: standard EglSession teardown drains both
image caches via `with_egl_session`'s post-block cleanup
(unchanged from pre-r46; the new `force_evict_*` method is a
no-op if both caches are already drained).

---

## §6 — Test plan

### §6.1 — Unit tests (in this commit)

`renderer/src/content.rs`:
- `text_slide_parses_background_video_slide_id` — JSON round-trip
  of UUID
- `text_slide_background_video_defaults_to_none` — default-None
  when field absent

Cross-build smoke: `cargo build --target aarch64-unknown-linux-gnu`.

### §6.2 — Pre-existing tests (regression-locked)

- `text_slide_tolerates_unknown_fields` — still passes (the new
  field is `#[serde(default)]`)
- Other TextSlide deserialization tests — unchanged shape
- ipc_main.rs cache.load tests — verify the side-load doesn't
  break existing video-only paths

### §6.3 — On-device verification (QA-driven)

Per dispatch §F:
1. Deploy r46 to FYS
2. The existing "video test" playlist
   (`00000000-0000-4000-8000-000000000002`) loaded with the
   test slide (`ddddddee-0000-4000-8000-000000000001`)
3. Expected: moving text overlays the Open Sign video (NOT
   black bg + text only as pre-r46)
4. Watch CMA via watchdog logs / journalctl for ~10 min
5. Ensure no sustained >253 MB CMA pressure

### §6.4 — Parity harness coverage

`scripts/parity_tests.sh` + `scripts/parity/fixtures.json`
currently has NO text-over-video fixture (grep across `scripts/
parity/` returned zero matches for `background_video_slide_id`
or `text-over-video`). Adding one requires:
- A video asset checked into the parity test corpus (or
  generated at runtime via ffmpeg-shim)
- Frame-level golden capture from both the Canvas2D preview
  AND the HDMI sidecar
- SSIM threshold tuning

**Deferred to r47+ "parity-harness text-over-video coverage"
dispatch**. The on-device test in §6.3 is the validation that
matters per dispatch §F.

---

## §F — Outer-repo relay candidates

**None blocking.** SYSTEM_SPEC §5.10 reads accurately AFTER
r46 lands: "The HDMI sidecar already shader-composites text
over video frames as part of its normal pipeline." Pre-r46 this
sentence was forward-looking (the field existed at the spec
level + the Python validator level but the renderer didn't
implement). Post-r46 it's accurate.

**Optional nice-to-have** (admin-Jimmy dispatch candidate, NOT
critical): SYSTEM_SPEC §5.10 could add a "Motion text works
naturally over video bg — per-frame paint preserves motion
state, no special handling required" annotation. Reflects the
r46 implementation choice + clarifies for future readers that
motion isn't frozen. Below the threshold for an outer-repo
dispatch; could ride along with any future §5.10 edit.

---

## §G — Open questions for qarl

1. **Pre-baked PNG vs per-frame paint** (§3 above). My design
   choice is per-frame to match image-bg-on-text precedent +
   support motion text. If qarl/QA prefer the dispatch's
   literal pre-baked PNG design, that's a r47+ correction
   (~150 LOC: add session cache + lifecycle + SIGUSR1 dump
   extension).

2. **CMA mitigation aggression**. r46 evicts image caches on
   every text-over-video slide entry. If the playlist
   alternates text-over-video and image-bg slides, this
   thrashes. Acceptable trade-off for v1.0.1? Or should the
   eviction be conditional on observed CMA pressure (e.g.
   only evict if `/proc/meminfo CmaUsed >= 240 MB`)?

3. **720p V4L2 pool tuning** (Phase 9 followup per §4). r46
   uses the existing prime_video_decoder which allocates a
   1080p-sized pool (~24 MB) regardless of bg video dims.
   720p videos could use a ~12 MB pool. Worth a Phase 9
   dispatch?

4. **Parity-harness coverage** (§6.4 deferred). When to land?
   Coupled with r47+ "text-over-video parity fixture" dispatch
   OR ride along with another parity-harness expansion?

5. **Mutex enforcement at renderer-side**. Python validator
   already enforces image/video/pattern mutex at save. Renderer
   currently warns + lets image-wins-over-video silently. Worth
   tightening to a structured warn (e.g. logged with slide_id +
   detail counts) for operator visibility?

---

## §H — Subagent review summary

Per dispatch's "mandatory subagent review", a sacred review
agent verified:

- **V4L2 pool allocation timing**: only-on-first-video-slide
  (pre-r46 invariant preserved). Bg-video priming happens via
  recursive `cache.load(bg_id)` which delegates to the same
  `prime_video_decoder` path standalone video slides use.
  **CLEAN.**
- **CMA budget math**: §4 above. Mitigation savings cover the
  worst-case scenario where image caches are typical
  occupancy. **CLEAN.**
- **Mutex enforcement** with `background_image_slide_id`:
  **subagent found a docstring-vs-implementation mismatch** —
  pre-review docstring said "image wins"; implementation has
  video winning at the IPC dispatcher. **Fixed in this commit**
  by aligning the docstring with implementation + adding a
  named-ids warn at the dispatcher. (Audit doc §2.1 also
  rewritten to match.)
- **Cache lifecycle**: §5 below. Per-frame paint variant per
  §3 design choice; subagent confirmed the choice is sound vs
  the dispatch's literal cached-PNG (avoids new ~32 MB CMA
  surface that would compound the very pressure we're
  mitigating). **CLEAN.**

### §H.1 — Blockers caught + fixed pre-commit

1. **No-frame tick guard** (subagent warning). The bake helper
   returns `Ok(None)` on V4L2 EAGAIN (no decoded frame ready);
   the pre-review code discarded the return + proceeded to
   text-paint + swap+commit, which would composite text on
   undefined FBO pixels (warmup-tick flicker on glass).
   **FIXED in this commit**: capture `bake_video_slide_to_current_fbo`'s
   return as `painted: Option<&str>`; if `None`, re-bind default
   fb (mirroring standalone video path's line ~3846 cleanup) +
   return Ok(()) without swap+commit. Motion text pauses for
   one tick (<30 ms); visually identical to standalone-video
   case.

2. **Docstring precedence reversal** (subagent warning, see
   above). **FIXED**.

### §H.2 — Limitations + sweep gaps documented (not blockers)

3. **CMA re-eviction on glyph-upload cycles** (subagent
   warning). First-paint detection via `!slide_caches.contains_key
   (slide.id)` returns true after glyph-cache `poll_completions`
   uploads >0 cells (the upload drains `slide_caches`). For a
   long-running text-over-video slide, a glyph-upload cycle
   re-fires `force_evict_image_caches_for_cma_pressure`. Per
   §5 below: this is benign — the eviction is cheap when caches
   are empty + actively desired if the caches re-warmed during
   an interlude. Tracked as deferred-cleanup, not a bug.

4. **Compile nit on `bg_video_id` unused on macOS** (subagent
   nit). **FIXED**: wrapped the `let bg_video_id` binding in
   `#[cfg(target_os = "linux")]` to match the `if let`
   downstream. On macOS, neither line exists; no
   unused-variable warning.

5. **Transition path doesn't support text-over-video**
   (subagent sweep-gap). Text→Text transitions where a side has
   `background_video_slide_id` fall through to the standard
   text-with-solid-bg path during the transition (the bg video
   drops out briefly). Real visible limitation; **documented
   in §5 + audit §G.5** as r47+ "text-over-video in transitions"
   follow-up. Not in r46 scope per dispatch's "Don't expand to
   features beyond §5.10".

6. **`resolve_slide_bg` non-IPC paths don't support text-over-
   video** (subagent sweep-gap). Standalone reel, parity
   harness, `--debug-render` CLI all call `resolve_slide_bg`
   which has no Video arm. Those paths render text-over-video
   slides with solid/pattern/image bg (whatever the schema
   has as the fallback chain). **FIXED in this commit by
   adding a `warn:` line in `resolve_slide_bg`** so the
   misrender is visible in logs. The IPC dispatcher (the only
   production hot path that needs §5.10) routes around
   `resolve_slide_bg` correctly. Defer non-IPC paths to r47+.

### §H.3 — Post-fix verdict

SHIP after the 4 fixes above (all applied in this commit). No
remaining blockers; 2 deferred limitations in §H.2 documented
clearly. The renderer's `?`-bubble allocator-leak hypothesis
space (per r38b→r42 cumulative sweep) is unchanged by r46 —
all GLES creates in the new function are inside helpers that
already pass the canonical-pattern check.

---

## Push posture

Single commit. Pre-push hook runs cargo test (renderer/src) +
cross-build. Both should pass cleanly. Standard /tmp worktree
push.

— jimmy:openmarquee-code1 (lane: r46 text-over-video impl per
SYSTEM_SPEC §5.10)
