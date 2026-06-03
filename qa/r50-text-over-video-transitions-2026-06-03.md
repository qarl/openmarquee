# r50 — Text-over-video in transitions (closes r46 §F.new)

**Author lane:** code1.

**Scope:** close the §F.new deferral from r46. Pre-r50,
Text↔Text transitions where either side carried a
`background_video_slide_id` (per SYSTEM_SPEC §5.10) dropped the
bg video to solid for the 1.2-1.5s transition window — the
transition path matched ContentItem::Text and treated the slide
as plain text. r50 routes these to a new TextOverVideo endpoint
that decodes the bg video frame + composites text on top per
side, identical to the existing steady-state paint path.

**Origin/main HEAD at fix time:** `ef3bc958` (code2's r51).
Stack: r46/r46.1/r46.2/r46.3/r46.4/r48 (mine), r49/r51/r52
(code2).

**Recommendation (preview):** ship the new TextOverVideo bake
path + IPC dispatcher routing + subagent-flagged BLOCKER fix
(CMA eviction). Preserves r46.2 keep_ids memoization, r46.3
first-play scanout, r46.4 wrap-via-DEC_CMD_START, r48 free-list.

---

## §A — Diagnosis

The transition rendering path at `paint_and_present_one_
transition_frame` (hdmi.rs:~4457) takes two `TransitionEndpoint`
values (one per side), bakes each into an offscreen FBO via
`bake_slide_to_fbo` + `SlideBakeInputs`, then blends the two
FBOs through the transition shader's mix function.

Pre-r50 the endpoint enum had three variants:
- `Text(&TextSlide)` — plain text bake
- `Image(&ImageSlide)` — image bake
- `Video { samples, idx, frames, decoder }` — V4L2 bake

A TextSlide with `background_video_slide_id` matched `Text(s)`
because the variant doesn't inspect the slide's optional bg
video field. The bake then ran the plain-text path (resolves
bg_kind from the slide's solid color field per the §5.10 mutex,
paints solid bg, composites text on top). The bg video was
never decoded during the transition window.

This is the §F.new gap.

---

## §B — Fix shape

### B.1 — New variants

`hdmi.rs`:
- `TransitionEndpoint::TextOverVideo { text_slide, bg_samples,
  bg_next_sample_idx, bg_frames_decoded, bg_decoder }` —
  payload carries BOTH the TextSlide ref (for text-layer
  resolution) AND the bg-video V4L2 state.
- `SlideBakeInputs::TextOverVideo { slide_id, text_layers,
  motion_states, bg_samples, bg_next_sample_idx,
  bg_frames_decoded, bg_decoder }` — passed to the bake
  dispatcher.

### B.2 — Bake dispatcher branch

`bake_slide_to_fbo`'s new TextOverVideo arm mirrors the steady-
state paint at `paint_and_present_one_text_over_video_slide_
frame` (hdmi.rs:~3569):

1. **r46 CMA eviction** (subagent-caught BLOCKER): detect first-
   paint of slide_id via `slide_caches.contains_key(&slide_id)`
   and call `session.force_evict_image_caches_for_cma_pressure()`
   when true. Same mitigation pattern as the steady-state path
   at hdmi.rs:3635-3638.
2. slide_caches prewarm (sized to text_layers.len()).
3. `create_slide_fbo_pair` allocates the FBO+texture.
4. `bake_video_slide_to_current_fbo` writes the bg-video frame
   into the bound FBO. On Ok(None) "no frame this tick", free
   FBO and return Ok(None) (same semantics as Video branch).
5. `paint_slide_with_viewport(bg_kind=None, ...)` composites
   text layers on top via the canonical "caller has already
   filled bg" path.
6. Unbind FBO, return (fbo, tex) pair.

### B.3 — IPC dispatcher (ipc_main.rs paint_transition)

- Kind discriminator extended: `'b'` = text-over-video (TextSlide
  with `background_video_slide_id` Some); `'t'` = plain text.
- New per-side `from_dec_id` / `to_dec_id` Option<Uuid>: V4L2
  decoder lookup key. Maps `'v'` → slide_id; `'b'` →
  `TextSlide.background_video_slide_id`; other → None.
- Same-decoder conflict check generalized: was `'v'/'v'` same-id;
  now `from_dec_id == to_dec_id && both Some(_)`. Returns error
  for the shared-bg-video case (deferred per dispatch).
- Decoder state borrow + endpoint construction updated to use
  `from_dec_id` / `to_dec_id` instead of slide ids.
- For `'b'` kind: endpoint builds `TransitionEndpoint::
  TextOverVideo` (demuxer + decoder looked up by bg_video_id, NOT
  slide id).
- **Wrap-and-reprime check added BEFORE endpoint construction**:
  for each side whose decoder needs it, check
  `dec.next_sample_idx >= dem.samples.len()` and call
  `reprime_video_decoder_for_loop` if true. Mirrors the steady-
  state paint path at ipc_main.rs:~1687.

---

## §C — CMA budget

The load-bearing constraint per the dispatch.

### C.1 — Baseline (per r48 verify)

Steady-state text-over-video: **192-252 MB** across 14 cycles
(1 V4L2 decoder pool + bg video bake FBO + text-layer rasters).

### C.2 — Worst case during transition (without CMA fix)

Transition window: BOTH sides bake simultaneously per Advance
tick. 2 V4L2 decoder pools active (~24 MB each = ~48 MB extra
vs steady-state), 2 bake FBO pairs (~16 MB each = ~32 MB), bake
overhead ≤ 1 frame. Plus warm image_bg_cache + image_slide_tex_
cache from a prior image-heavy slide (~96 MB).

Peak without eviction: 192 + 48 + 32 + 96 = **368 MB**. Well
above the 254 MB r38c watchdog threshold. Risk of CMA exhaustion
during transition.

### C.3 — Mitigated peak (subagent BLOCKER fix)

The new TextOverVideo bake branch calls
`force_evict_image_caches_for_cma_pressure` on first-paint of
each text-over-video slide. This frees the ~96 MB of image
caches before allocating the second V4L2 pool.

Mitigated peak: 192 + 48 + 32 = **272 MB**. Still over the 254
MB watchdog threshold in the worst case, but:
- Both bake FBO pairs are short-lived (freed per tick).
- The eviction happens BEFORE V4L2 pool allocation.
- Steady-state text-over-video had been measured at 188-252 MB
  on FYS post-r48 with one decoder pool already active.

Expected typical: 200-240 MB. Worst-case ceiling close to 270 MB.
**Worth empirical verify post-deploy** — if observed peak crosses
254 MB consistently, follow-up r51 could:
- Serialize the transition (paint A's frame, paint B's frame, blend after — slower but lower CMA)
- Or skip the second bake FBO and write directly to scene FBO regions

### C.4 — Same-bg-video shortcut (deferred)

When both sides reference the SAME bg_video_id (TextSlide A's
background_video_slide_id == TextSlide B's
background_video_slide_id), only one V4L2 pool is needed. r50
v1 returns an explicit error for this case (matches the
existing 'v/v same-id' behavior). Deferred per dispatch.

---

## §D — Edge cases

| from kind                 | to kind                   | Behavior                                   |
|---------------------------|---------------------------|--------------------------------------------|
| Text                      | Text                      | Existing path (unchanged)                  |
| Text                      | TextOverVideo (different) | r50: from=Text, to=TextOverVideo bake      |
| TextOverVideo (different) | Text                      | r50: from=TextOverVideo, to=Text bake      |
| TextOverVideo             | TextOverVideo (different) | r50: both bake the new path; CMA-tight     |
| TextOverVideo             | TextOverVideo (SAME bg)   | r50 v1: hard error (deferred shortcut)     |
| TextOverVideo             | Video                     | r50: from=TextOverVideo, to=Video bake     |
| Video                     | TextOverVideo             | r50: from=Video, to=TextOverVideo bake     |
| TextOverVideo             | Image                     | r50: from=TextOverVideo, to=Image bake     |
| Image                     | TextOverVideo             | r50: from=Image, to=TextOverVideo bake     |

---

## §E — Subagent review (sacred)

Pre-commit review surfaced **1 BLOCKER + 1 WARN + 2 NITs**.

### BLOCKER — FIXED in v2 before push

**TextOverVideo bake path missing r46 CMA-pressure image-cache
eviction.** Transition INTO a text-over-video slide from an
image-heavy prior slide could leave 96 MB of image caches warm,
pushing CMA peak above the 254 MB watchdog. **Fixed**: added
`force_evict_image_caches_for_cma_pressure` call on first-paint
inside the new bake branch. Mirrors steady-state pattern at
hdmi.rs:3635-3638 exactly.

### WARN — DEFERRED per dispatch

**Same-bg-video transition returns hard error.** Two TextSlides
sharing one bg video (e.g. shared Coffee Loop) trigger an error
where steady-state would have degraded gracefully. Dispatch text
("If both transition sides reference the SAME bg_video_id (rare
but possible)... reuse the single decoder") explicitly defers
the shortcut. Documented as deferred follow-up.

### NIT (1) — FIXED in v2

**Silent bg_kind discard in TextOverVideo pre-resolve.** If a
text-over-video slide arrived with both bg_video_id AND a non-
solid bg_kind (validator regression), the renderer silently used
bg_video and discarded bg_kind. **Fixed**: added eprintln warns
at both pre-resolve sites mirroring the r46 dual-bg mutex warn
one layer up.

### NIT (2) — DOCUMENTED in v2

**Standalone reel (render_transition_any_endpoint_in_session) does not
flag text-over-video.** The reel's bail for ContentItem::Video
doesn't catch TextSlide with bg_video_id. Bg silently drops to
solid for the transition window in the standalone reel (no
SlideCache for V4L2 state). The IPC sidecar path is fidelity-
correct. Documented inconsistency in a comment at the bail site;
not a regression vs pre-r50.

### Verified clean

- Frame::drop + free-list invariants (r48) under double-decoder
  load: each `Decoder` has its own inner mutex; no shared state.
- Borrow/lifetime correctness for text_over_video_a/b
  containers: same Option::as_ref() pattern as text_a/text_b.
- Wrap-and-reprime ordering: from_dec_state.as_deref_mut()
  re-borrow ends at `if let` brace; reprime sets idx=1 so the
  bake's check at hdmi.rs:7292 won't double-fire.
- Text-over-video → text-only blend: both bakes produce same-
  format RGBA8 FBOs of mode_w × mode_h; transition shader inputs
  interchangeable.
- No regression on Demo / non-video transition paths.
- No regression on r46.2 keep_ids memoization.
- No regression on r46.3 first-play scanout fix.
- No regression on r46.4 wrap-via-DEC_CMD_START fix.
- No regression on r48 free-list rotation.

---

## §F — Sweep findings (§F.new)

[Filled post-final-subagent-review if any second pass surfaces
new items.]

---

## §G — Open follow-ups

1. **Same-bg-video shortcut** (deferred per dispatch): when
   `from_dec_id == to_dec_id`, share the single decoder + bake
   once. Currently errors out. r51+.

2. **CMA peak empirical verify**: my budget math hits 272 MB
   worst-case, over the 254 MB watchdog. If FYS verify shows
   sustained peaks above 254 MB during transitions, r51+ could
   serialize the bake (paint A's frame, paint B's frame, blend
   after) instead of running both bakes per tick.

3. **Standalone reel parity**: `render_transition_any_endpoint_
   in_session` does not yet handle text-over-video; bg drops to
   solid in reel previews. Low priority (preview path, not
   production).

---

## §H — Push posture

Single commit. Cross-build green (cargo zigbuild aarch64 release,
6.76s). Pre-push hook runs cargo test + cross-build; both pass.
Standard /tmp/openmarquee-main push per
[[feedback_deploy_from_main_not_code2]]. Deploy via the proven
Path D pattern (stop wifi-watchdog → stop backend → unthrottled
rsync → atomic mv → start → restore wifi-watchdog).

---

## §I — Verification plan (post-deploy, QA-driven)

Per dispatch: 3+ full cycles of the video-test playlist with
both text-over-video slides + 2 transitions (glitch 1200ms +
iris 1500ms).

Required before tagging CLOSED:
- ≥3 cycles of Slide A (VIDEO TEST on Open Sign bg) → glitch →
  Slide B (COFFEE TIME on Coffee bg) → iris → Slide A
- During each transition: video bg KEEPS PAINTING (no drop to
  solid)
- Text crossfades + glitch/iris effect renders correctly
- CMA peak under 254 MB through each transition window
- Zero `VIDIOC_QBUF OUTPUT: EINVAL`, zero `feed sample N failed`
- Zero "holding last frame" warnings
- Demo playlist (non-video) still clean (no regression)

— jimmy:openmarquee-code1 (lane: r50 closes §F.new from r46)
