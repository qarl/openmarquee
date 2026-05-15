# Phase 8 slice 0: non-text endpoint transition recon

**Date:** 2026-05-15
**Dispatch:** Close the Rust IPC transition fallback gap. Today
`paint_transition` validates both endpoints as TextSlide and refuses
non-text with `from/to non-text slide TBD`; Python's
AutoFallbackRenderer routes those refusals through PIL. Goal of
Phase 8 is to retire the gate so Image→Image, Image→Text, Text→Image,
Image→Video, Video→Video, etc., all go through the Rust shader.
**Status:** Recon only — no source change this slice.

## Current state (where the TextSlide-only is wired)

### Rust side

- **`renderer/src/hdmi.rs:3101` `paint_and_present_one_transition_frame`** — signature
  is `slide_a: &TextSlide, slide_b: &TextSlide`. Hardcodes both
  endpoints to text. Internally it:
  1. Resolves text layers + bg for slide_a (resolve_slide_layers).
  2. Resolves text layers + bg for slide_b.
  3. Two `make_slide_fbo` bakes (one per endpoint), each populating
     a session.slide_caches entry keyed by slide.id.
  4. Builds a textured-quad VBO + links the transition program from
     `fs_for_transition_kind(kind)` → `VS_TEXTURED_QUAD` + the kind's
     fragment shader.
  5. Binds `u_src_a` + `u_src_b` to tex_a + tex_b, sets `u_t =
     progress`, draws.
  6. Optionally routes through scene_fbo for brightness/gamma.
  7. Scanout rotation (swap, lock_front_buffer, addFB, commit_fb).

- **`renderer/src/hdmi.rs:4606` `make_slide_fbo`** — text-only. Takes
  `(bg_kind, text_layers, glyph_cache, tex_cache)` and returns
  `(NativeFramebuffer, NativeTexture)`. Calls `paint_slide` to render
  bg + text into a fresh FBO+tex. The transition path's two bakes go
  here.

- **`renderer/src/hdmi.rs:2639` `paint_and_present_one_image_slide_frame`** — direct-to-scanout. Uploads
  PNG → texture → `run_blit_pass` to the currently-bound framebuffer
  (default fb in the hold path) → swap+commit. **No FBO-bake variant
  exists**; the image path always writes to the active fb.

- **`renderer/src/hdmi.rs:2763` `paint_and_present_one_video_slide_frame`** — direct-to-scanout, similar
  shape: drains the next NV12 frame from the V4L2 decoder, runs the
  BT.601 NV12→RGB blit pass through `run_nv12_blit_pass` /
  `run_nv12_dmabuf_blit_pass` against the active fb, then
  swap+commit. **No FBO-bake variant exists.** Function is
  `#[cfg(target_os = "linux")]`-gated (V4L2 is Linux-only); slice 2's
  `bake_video_slide_to_fbo` inherits the gate.

- **`renderer/src/ipc_main.rs:740` `validate_paint_transition_endpoints`** — guards both
  endpoints:
  ```rust
  if !matches!(from, ContentItem::Text(_)) {
      return Err("paint_transition: from non-text slide TBD");
  }
  if !matches!(to, ContentItem::Text(_)) {
      return Err("paint_transition: to non-text slide TBD");
  }
  ```
  Called at ipc_main.rs:897 inside the paint_transition op
  handler. **The six cargo tests at ipc_main.rs:1408-1448 pin the
  wire-format error substrings** (one text/text ok-path + five
  err-paths: from_image, from_video, to_image, to_video,
  from-precedence-when-both); they will need updating in lockstep
  with the gate removal.

### Python side

- **`backend/openmarquee/rendering/rust_renderer.py:227-230`**
  `_UNSUPPORTED_SLIDE_WIRE_MARKERS` lists `"non-text slide TBD"`. The
  proxy's `_classify_op_error` promotes any sidecar error matching
  this substring to `RustRendererUnsupportedSlideError`, which
  `AutoFallbackRenderer` interprets as "this slide can't go through
  the Rust route → fall through to MockRenderer or PIL".

- **`backend/openmarquee/playback.py:_play_via_rust_ipc`** —
  `begin_transition` is dispatched through the Rust IPC route; on
  `RustRendererUnsupportedSlideError` (which the proxy promotes from
  the wire marker above), AutoFallbackRenderer swaps in the PIL
  renderer for that transition.

### Tests pinning the current behavior

- **Rust:** 4 cargo tests at ipc_main.rs:1408-1448 (text/text OK,
  image/text errs, video/text errs, text/image errs, text/video
  errs, image/video errs with from-precedence).
- **Python:** `backend/tests/rendering/test_rust_renderer.py` covers
  `_classify_op_error` against `"non-text slide TBD"` → promotes to
  `RustRendererUnsupportedSlideError`. `backend/tests/test_playback_rust_route.py`
  exercises the AutoFallbackRenderer swap path on that error.

## Design choices that need calls before slicing

### Q1 — Image endpoints: bake-per-frame or upload-once?

The transition pipeline today re-bakes both sides on every Advance
tick (hdmi.rs:3095-3099 comment notes this is borderline at 1080p).
For text bakes, that's because motion frozen during transition + auto
layers re-rendering wall-clock means the bake genuinely changes per
frame. **For image slides the asset is static**; uploading the PNG
once at transition start and reusing the FBO is strictly cheaper
than re-uploading per frame.

Two options:

- **Option A — bake-per-frame for image too.** Each tick: upload PNG
  → blit to FBO. Mirrors text's shape; minimal refactor; ~5 ms/tick
  for a 1080p PNG (texImage2D dominates).
- **Option B — bake-once-at-transition-start for image.** Cache the
  rendered FBO+tex in `session.slide_caches` (extend the cache to
  hold image-bake artifacts). Per-tick is just sampling the cached
  texture.

**Recommendation:** Start with Option A (bake-per-frame). It keeps
the slice boundary clean — the unified `bake_slide_to_fbo` helper
treats every endpoint kind uniformly. Option B is a follow-up perf
slice if Pi-side smoke shows the per-frame upload is over budget.

### Q2 — Video endpoints: snapshot-at-start, or feed during transition?

Video endpoints during transitions have a real semantic question
that text/image don't:

- The V4L2 decoder advances by sample. Each `paint_video_frame` call
  feeds the next sample → drains the next decoded NV12 frame.
- If the transition lasts 500 ms at 30 fps = 15 frames, and we call
  `bake_video_to_fbo` per tick, we'd advance the decoder 15 samples
  — meaning the transition shows the video PLAYING THROUGH the
  transition, then jumping forward at transition end.
- The natural semantic is **the source slide is "frozen" at the
  moment the transition starts; the destination slide starts from
  its first frame at transition end**. That matches how broadcasters
  composite video transitions.

Three options:

- **Option C — Snapshot-once at transition start.** Drain one frame
  from the source decoder, blit NV12→RGB into a stable FBO+tex,
  reuse the texture for every transition tick. Destination video
  similarly: snapshot the first frame (it's already primed at
  BeginSlide). Decoder is NOT advanced during the transition.
- **Option D — Feed through transition.** Drain a new frame per
  tick on both sides. Video plays through transition; jumps at
  transition end. Cheap to implement but semantically wrong.
- **Option E — Pause source decoder, play destination.** Snapshot
  source at start; destination decoder advances normally so the new
  slide is "already playing" when transition ends.

**Recommendation:** Option C (snapshot-once on both sides).
Matches broadcast semantics; lets the decoder cleanly own its
sample-advance discipline; the transition path doesn't touch the
decoder at all. The "destination video starts from sample 0 at
transition end" is the natural V4L2-prime-on-BeginSlide behavior
already in place.

This is the **biggest design call of the phase**. If qarl wants
option E instead, that's a different decoder lifecycle and a new IPC
op to mark the source as "frozen for transition out."

### Q3 — Should the IPC paint_transition signature change?

Today the wire format passes `from: SlideId, to: SlideId, kind:
String, progress: f32`. The Python side looks up the slides in
`SlideCache` by id. No wire-format change is needed; the gate-removal
is purely a server-side validation drop. **No IPC version bump
needed.** Cargo tests already cover the from-precedence ordering and
the marker substrings.

## Proposed slice sequence

| Slice | What | Files | Risk |
|-------|------|-------|------|
| 0 (this) | Recon doc + design calls | qa/captures/... | none |
| 1 | Extract `bake_image_slide_to_fbo` (refactor; image path unchanged at scanout) | hdmi.rs | low |
| 2 | Extract `bake_video_slide_to_fbo` snapshot helper (Option C; reuse the V4L2 blit-pass machinery against an FBO target) | hdmi.rs | medium (V4L2 decoder lifecycle interaction) |
| 3 | Unified `bake_slide_to_fbo` dispatcher accepting `ContentItem` | hdmi.rs | low |
| 4 | Refactor `paint_and_present_one_transition_frame` to accept `ContentItem` endpoints + dispatch through slice-3 helper | hdmi.rs, ipc_main.rs caller | medium (transition cache key shape; same-id case — preserve the existing text-path handling at hdmi.rs:3134-3170 where the second cache get-or-init sees needs_new=false and reuses the warm cache populated by slide_a's bake) |
| 5 | Drop `validate_paint_transition_endpoints` text gate; update the 4 cargo tests to assert the new ok-paths | ipc_main.rs | low |
| 6 | Drop `"non-text slide TBD"` from `_UNSUPPORTED_SLIDE_WIRE_MARKERS`; update pytest + AutoFallbackRenderer test cases | rust_renderer.py, tests | low |
| 7 | Pi-side smoke: TextSlide→ImageSlide + ImageSlide→TextSlide + ImageSlide→ImageSlide; verify visually + perf budget | (no source) | medium |
| 8 (optional) | Pi-side smoke: VideoSlide endpoints; verify decoder state | (no source) | medium |

Each slice is a separate commit with subagent review per AGENTS.md.

## Out of scope

- **Transition-cache LRU** (the slide_caches keying for transition
  pairs at hdmi.rs:3095-3099 noted as a Slice (e) follow-up). The
  per-frame re-bake cost is a known perf concern; closing it is
  orthogonal to the endpoint-kind gap.
- **Brightness/gamma post-pass** behavior on non-text endpoints —
  the existing scene_fbo bind path at hdmi.rs:3245-3293 wraps the
  transition output, structurally orthogonal to endpoint kind. No
  design call needed; verify in slice 7 smoke.
- **Decoder lifecycle on transition cancellation.** Option C parks
  source decoder at the snapshot instant. If the user navigates
  away mid-transition (e.g., playlist advance during a 500ms blend),
  the destination decoder — primed at BeginSlide — survives the
  cancelled transition's worth of unfed samples. No new action
  needed: BeginSlide of the next slide naturally tears down both
  prior decoders. Flag for slice 8 smoke verification, not a
  design-call.
- **The 16-transition shader deck** — `fs_for_transition_kind`
  already accepts all 16 kinds + FS_CUT fallback. No shader work
  required for Phase 8.
- **Capture-side VideoSlide TBD** — `"VideoSlide capture not
  implemented"` stays as a separate marker for the Capture op (slide
  thumbnails / screenshots), not paint_transition. The
  `_UNSUPPORTED_SLIDE_WIRE_MARKERS` tuple keeps that marker; only
  the `"non-text slide TBD"` entry is removed.

## Open questions for qarl (asked through QA dispatch)

1. **Q2 above — Option C (snapshot-once on video endpoints) vs
   Option E (snapshot source, play destination)?** Recommendation
   is Option C; flag in the slice-2 commit message if qarl wants E
   instead.

2. **Image bake-per-frame (Option A) vs cache-once (Option B)?**
   Recommendation is A; cheaper to revisit in a follow-up if Pi
   smoke shows budget pressure.

3. **Slice ordering** — should slices 1+2 land separately (clean
   refactor commits) or fold into slice 3's dispatcher introduction?
   Recommendation: separate. Each refactor commit is small + easy
   to subagent-review; folded together it's a 200+ LOC blob that's
   harder to review.

## Verification approach

- **Per-slice**: cargo test + pytest must stay green. The 4
  cargo tests at ipc_main.rs:1408-1448 will change in slice 5 (the
  text/image and text/video and image/video error paths become ok-paths).
- **Slice 7+8**: Pi-side smoke against the dev Pi (`openMarqueeDev`)
  with a hand-crafted playlist exercising each endpoint kind. Visual
  verification on glass (or, if glass is unavailable per the
  `project_phase7_pending_at_office` memo, defer slice 7+8 visual
  to next office time).
- **No goldens**: parity-fixture goldens cover the within-slide
  rendering, not transition frames. Transition correctness is
  verified by glass-time smoke, not pixel-compared.

## Cross-refs

- Phase 7 sidecar IPC mode (renderer-rewrite-plan-rust.md Step 7) +
  Phase 7 as-built doc 2026-05-14.
- f481794 — TextSlide→TextSlide transition IPC route (slice 4
  followup); this Phase 8 extends that to non-text endpoints.
- V4L2 pieces 3+4 — the NV12→RGB blit machinery that slice 2 will
  reuse.
- `feedback_no_soak_during_dev` — slice 7+8 smokes are short
  characterization runs, not soaks.
- `project_phase7_pending_at_office` — HDMI EDID stuck at 0 bytes
  on dev Pi at last check; if still unresolved, glass smokes wait
  for office time. Slices 1-6 are all Mac-side cargo + pytest work.
