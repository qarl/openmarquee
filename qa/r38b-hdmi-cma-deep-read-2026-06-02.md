# r38b — hdmi.rs CMA-allocator deep-read

**Author lane:** code1 (renderer-perf).

**Scope:** the dispatch's E.1 — deep-read the 4 SUSPECT
`lock_front_buffer` callsites identified in
[[../qa/r37-cma-allocator-leak-audit-2026-05-31.md]] §A.3, mirror
the canonical `external_nv12` release shape, identify missing
release sites.

**Conclusion (TL;DR):** all 4 SUSPECT paths PASS the canonical
pattern check. **No missing-release leak found in any GBM scanout
BO/FB path.** Recommend the next investigation step pivot to non-
A.3 allocators or to E.2 (BO-counter instrumentation).

**Origin/main HEAD at deep-read time:** `6b1446e` (code2 r37).

---

## §1 — How the 9 paths map at HEAD

Code2 r37's audit cites line numbers against code2 HEAD `8602317`;
HEAD `6b1446e` has 12 commits of drift on hdmi.rs (perf-night r1-
r25 + V1.0 close). Line numbers do not match but **function names
do.** Mapping table:

| r37 audit cite | code2 HEAD function | HEAD-`6b1446e` lock_front_buffer site | r37 confidence | r38b verdict |
| --- | --- | --- | --- | --- |
| `:1306` | render_one_frame_in_session | `:1324` | LIKELY | **PASS** |
| `:1604` | render_animated_slide_in_session | `:1621` | LIKELY | **PASS** |
| `:3126` | paint_and_present_one_frame_for_slide | `:3334` | LIKELY | **PASS** |
| `:3281` (B.1) | paint_and_present_one_image_slide_frame | `:3508` | SUSPECT | **PASS** |
| `:3382` (B.2) | paint_and_present_external_frame | `:3613` | SUSPECT | **PASS** |
| `:3485` (canon) | paint_and_present_external_nv12_frame | `:3716` | VERIFIED | **PASS** (canonical reference) |
| `:3664` (B.3) | finish_video_slide_swap_and_commit | `:3921` | SUSPECT | **PASS** |
| `:4113` (B.4) | paint_and_present_one_transition_frame | `:4370` | SUSPECT (HIGHEST PRIORITY) | **PASS** |
| `:12546` | render_animated_atomic frame-0 | `:13253` | VERIFIED | **PASS** |

Plus the scope-C lower-priority callsites flagged by r37's subagent:

| r37 audit cite | code2 HEAD function | HEAD-`6b1446e` lock_front_buffer site | r38b verdict |
| --- | --- | --- | --- |
| `:7723` | render_transition_animated_in_session | `:8052` | **PASS** |
| `:8200` | render_transition_single_pass_in_session | `:8529` | **PASS** |
| `:8740` | render_transition_scissored_bake_in_session | `:9069` | **PASS** |
| `:12595` | render_animated_atomic per-frame loop | `:13303` | **PASS** |

13 of 13 lock_front_buffer sites — including all 4 r37 SUSPECT
paths and all 4 r37-subagent-flagged scope-C sites — match the
canonical pattern.

---

## §2 — The canonical pattern (annotated reference at `:3716`)

`paint_and_present_external_nv12_frame` at hdmi.rs:3659-3746
(lock at `:3716`). The audit calls this VERIFIED; this section
documents the exact shape so the per-path verdicts below are
unambiguous.

The 11-step contract from `eglSwapBuffers` to "current installed,
prev rotated, prev-prev freed":

```rust
// 1. Swap — flush GLES + present pending. ?-bubble: no BO held yet, no leak.
session.egl_lib.swap_buffers(...).map_err(...)?;                                // :3712

// 2. Lock the new BO. ?-bubble: no BO held yet, no leak.
let new_bo = unsafe { session.gbm_surface.lock_front_buffer().context(...)? }; // :3716

// 3. Read BO metadata into a wrapper. ?-bubble: `new_bo` in scope, RAII-drops on unwind.
let fb_buf = GbmBufferAdapter::new(&new_bo).context(...)?;                     // :3719

// 4. Register a DRM framebuffer against the BO. ?-bubble: `new_bo` still in scope, RAII-drops.
let new_fb = card.add_framebuffer(&fb_buf, 32, 32).map_err(...)?;              // :3721

// 5. Commit (SetCrtc or page_flip). On FAILURE, explicit cleanup:
if let Err(e) = commit_fb(session, card, new_fb) {                              // :3724
    if let Err(de) = card.destroy_framebuffer(new_fb) { /* warn */ }            //   - rmFB the new FB
    drop(new_bo);                                                               //   - drop the new BO
    return Err(e);                                                              //   - prev pair STAYS (still on scanout)
}

// 6-7. On SUCCESS: rotate prev pair out (it was on scanout 2+ vblanks ago).
if let Some(fb) = session.scanout_prev_fb.take() {                              // :3733
    if let Err(e) = card.destroy_framebuffer(fb) { /* warn */ }
}
if let Some(bo) = session.scanout_prev_bo.take() { drop(bo); }                  // :3738

// 8-9. Slide current → prev.
session.scanout_prev_fb = session.scanout_current_fb.take();                    // :3741
session.scanout_prev_bo = session.scanout_current_bo.take();

// 10-11. Install new as current.
session.scanout_current_bo = Some(new_bo);                                      // :3743
session.scanout_current_fb = Some(new_fb);
```

**The invariants are**:

A. **Pre-commit failure (steps 1-4) → no leak.** Either no BO is
   held yet, or `new_bo` is owned by a local binding and Rust's
   RAII drops it on unwind via `?`. Prev pair is untouched and
   still on scanout — correct.

B. **Commit failure (step 5) → explicit cleanup is required.**
   The `new_fb` is a raw DRM handle (no Drop); explicit
   `destroy_framebuffer` is needed. `new_bo` is local — `drop`
   is required because the next post-failure path (caller's
   ?-bubble) drops it transitively, but an explicit drop makes
   the order deterministic. Prev pair STAYS (correct — still
   on scanout).

C. **Commit success (steps 6-11) → rotate the previous pair
   out.** The "prev" pair at this point was the one scanning
   out N-2 vblanks ago (kernel is now on the freshly committed
   "current"). Take both halves, destroy the FB, drop the BO,
   slide "current" → "prev", install "new" → "current".

The pattern's defensive shape (explicit `.take()` before destroy/
drop, then assign) is **idempotent and safe to re-run** — if any
step fails mid-way, the prev pair never gets stranded.

---

## §3 — Per-path verdicts

### §3.1 B.1 — `paint_and_present_one_image_slide_frame` (`:3508`)

**Verdict: PASS.**

Read range: hdmi.rs:3417-3541. Scanout cycle at lines 3501-3535.

Steps 1-11 match the canonical pattern verbatim with `(image_slide)`
context tags on each error message. Lines 3515-3522 (commit-fail
handler) mirror canonical lines 3724-3731. Lines 3524-3535 (happy-
path rotation) mirror canonical lines 3733-3744.

No missing release site on any code path. The pre-commit body
(lines 3463-3496) is GL-only (FBO bind, blit, present pass); none
of those allocate scanout BOs.

### §3.2 B.2 — `paint_and_present_external_frame` (`:3613`)

**Verdict: PASS.**

Read range: hdmi.rs:3555-3643. Scanout cycle at lines 3606-3641.

Identical structure to canonical with `(external_frame)` context
tags. Function-level docstring at line 3551-3554 explicitly says
"Structurally identical to paint_and_present_one_image_slide_frame
— same scene-FBO brightness/gamma routing and the same scanout-
rotation discipline."

### §3.3 B.3 — `finish_video_slide_swap_and_commit` (`:3921`)

**Verdict: PASS.** (FYS-irrelevant per dispatch but checked for
completeness.)

Read range: hdmi.rs:3910-3950. Pure scanout cycle — no pre-commit
body at all, just steps 1-11. Identical to canonical with
`(video_slide)` context tags. Caller (`paint_and_present_one_video_slide_frame`)
does the V4L2 decode + texture upload before calling this helper.

### §3.4 B.4 — `paint_and_present_one_transition_frame` (`:4370`) — HIGHEST PRIORITY

**Verdict: PASS.**

Read range: hdmi.rs:4065-4397. Scanout cycle at lines 4363-4395.

This was the audit's highest-priority candidate ("Most likely
common path for FYS text+image slides"). On deep-read the label
in the audit is misleading: `paint_and_present_one_transition_frame`
is NOT the common per-frame path for static slides — it is the
TRANSITION-frame path (between slide A and slide B). The
common-path for static text+image slides is
`paint_and_present_one_frame_for_slide` (line 3151) +
`paint_and_present_one_image_slide_frame` (line 3417); both PASS.

Within the transition function itself the scanout cycle (lines
4363-4395) is canonical. The pre-commit body (lines 4147-4346) is
a `let work: Result<bool> = (|| unsafe { ... })()` closure that
bakes both endpoints (A + B) into intermediate FBOs + textures
then composites them via a per-call program/VBO. The closure has
extensive `cleanup_static` calls in error branches that release
the per-call GL state (FBOs at `fbo_a` / `fbo_b`, textures at
`tex_a` / `tex_b`, the program, the VBO). **These are GL handles,
not scanout BOs** — they do not consume the scanout BO pool.

The only scanout-BO-relevant code is steps 1-11 at lines
4363-4395, and they are byte-for-byte canonical.

### §3.5 Re-verification: `paint_and_present_one_frame_for_slide` (`:3334`)

The r37 audit marked this LIKELY (not SUSPECT), but I re-read it
since it is the actual FYS per-frame text-slide path.

**Verdict: PASS.**

Read range: hdmi.rs:3151-3402. Scanout cycle at lines 3326-3370.

Identical structure to canonical. The pre-commit body has slide-
cache management + glyph-cache poll + paint_slide call, none of
which touch scanout BOs.

### §3.6 Scope-C re-checks

The r37 audit's scope-C subagent-flagged paths at hdmi.rs:7723 /
:8200 / :8740 / :12595 (mapped to HEAD lines 8052 / 8529 / 9069 /
13303):

- **render_transition_animated_in_session** at `:8052` — local
  `prev_fb` / `current_fb` rotation with explicit
  `end_of_in_session_render_call(...)` cleanup hand-off at line
  8135. PASS.
- **render_transition_single_pass_in_session** at `:8529` — same
  shape. PASS.
- **render_transition_scissored_bake_in_session** at `:9069` —
  same shape. PASS.
- **render_animated_atomic** at `:13253` (frame 0) and `:13303`
  (per-frame loop) — uses a `VecDeque<(BufferObject, FB-handle)>`
  with explicit `pop_front + destroy_framebuffer + drop` while
  `bos.len() > 2` (lines 13344-13350); final cleanup loop at
  line 13391+ unconditionally destroys all queued FBs and drops
  all queued BOs. CLI-only diagnostic path. PASS.

---

## §4 — held_scanout_fb / _bo (r37 §B.6)

The audit flagged `session.held_scanout_fb` / `_bo` at hdmi.rs:317-
318 as a SUSPECT-for-early-return-paths leak vector. On deep-read:

- The ONLY assignment site is line 1237-1238 in
  `end_of_in_session_render_call`.
- The matching `.take()` of the prior held value is at line
  1231-1236 in the same function, BEFORE the new assignment.
- The session-teardown drain at line 909-916 also takes + frees.
- `end_of_in_session_render_call` is called by exactly four
  functions: `render_one_frame_in_session`, `render_animated_slide_in_session`,
  `render_transition_animated_in_session`, `render_transition_single_pass_in_session`
  / `render_transition_scissored_bake_in_session` (all CLI /
  standalone-bake paths).
- The production sidecar's `paint_and_present_*` family does NOT
  go through `end_of_in_session_render_call`. It uses
  `scanout_prev_fb` / `scanout_current_fb` instead. So
  `held_scanout_fb` stays at its initial `None` for the entire
  production session.

**Verdict: PASS.** `held_scanout_fb` is not a leak vector on the
FYS production sidecar path.

---

## §5 — What this means for the leak hunt

**The r37 audit's HIGH-priority candidate C.1 — GBM scanout BO/FB
leak in a SUSPECT path — is REFUTED by the deep-read.** All 13
`lock_front_buffer` sites in `renderer/src/hdmi.rs` at HEAD
`6b1446e` use the canonical 11-step release pattern.

This narrows the search. The remaining open candidates from r37
are:

- **C.2 (held_scanout leak)** — REFUTED in §4 above.
- **C.3 (EGLImage destroy fall-through)** — RULED OUT for FYS by
  the r37 audit (DMABUF env-var gating + no VideoSlide on FYS).
- **C.4 (static atlas baseline pressure)** — not a leak; an
  unmeasured baseline (r37 §G.4 open question — still useful to
  capture from FYS journalctl).
- **C.5 (texture cache cap simultaneity)** — not a leak; r37
  ruled out for current FYS workload (< 6 unique image bg, < 6
  unique image slides). **Worth re-confirming with a FYS-side
  print of `image_bg_cache.len()` + `image_slide_tex_cache.len()`
  at peak load.**
- **C.6 (V4L2 + DMABUF)** — RULED OUT for FYS (no VideoSlide).
- **C.7 (atlas page growth)** — RULED OUT by static reading
  (pages fixed at 2048×2048).

**Surviving candidates for the ~70 MB leak:**

1. **A.5 cache approach-to-cap** — image_bg_cache + image_slide_tex_cache
   start at 0 entries on sidecar boot and grow toward their caps
   (6 entries each × ~8 MB = 96 MB combined). If FYS reel has
   even 4-5 unique image slides + 4-5 unique image bgs, the
   caches can account for ~70 MB of "growth from boot to steady-
   state" that LOOKS like a leak but is actually the LRU caches
   warming up to cap. *This is the leading non-A.3 candidate.*
2. **Static atlas size (A.6)** — `sdf_atlas_gl.rs:104-110`
   already logs total size at session boot. One `journalctl -u
   openmarquee-backend.service | grep -i atlas` on FYS would
   resolve r37 §G.4 instantly.
3. **CMA pool fragmentation** — CMA pages get allocated +
   released over time but pool can fragment such that
   `cma_used` accounts pages-in-pool as "used" even when
   logically free. This is a kernel-side effect orthogonal to
   the renderer's release-correctness. `/sys/kernel/debug/cma/*/
   alloc_count` + `free_count` deltas would surface it.

---

## §6 — Recommendation: next dispatch shape (r38c?)

Given E.1 finds no actionable leak, the cheapest next steps are:

### r38c Option A — Cache pressure measurement (cheapest, no code change)

1. SSH to FYS during normal operation; capture
   `cat /proc/meminfo | grep Cma` baseline.
2. `journalctl -u openmarquee-backend.service --since "1 hour
   ago" | grep -iE "atlas|cma|bg_cache|tex_cache"` to surface
   existing logs.
3. Add a SIGUSR1 handler (~10 LOC in main.rs) that dumps:
   `image_bg_cache.len()`, `image_slide_tex_cache.len()`,
   `slide_caches.len()`, `cma_used`. SSH, send `kill -USR1
   $(pgrep openmarquee-render)`, read the dump from stderr.
4. If `image_bg_cache.len() + image_slide_tex_cache.len()` is
   near cap (6+6=12) → C.5 (cache approach-to-cap) is the cause;
   not a true leak but a baseline cost. Trim the cache caps or
   accept the higher baseline.

### r38c Option B — BO-counter instrumentation (r37's E.2)

The r37 audit's §E.2 — add an atomic counter pair around
`lock_front_buffer` / `drop`. Cost: ~30 LOC across hdmi.rs;
runtime: one atomic add per scanout commit (negligible). 5-10
min of normal operation surfaces a leak rate IF one exists.

Given the deep-read PASS verdict in this doc, the prior
probability of E.2 surfacing a leak is now LOW. Option A is
cheaper + targets the highest-prior-probability remaining
candidate.

### r38c Option C — sysfs probing (r37's E.3)

Zero code change. Capture `/sys/kernel/debug/dri/0/state` +
`/proc/meminfo` + `/sys/kernel/debug/cma/*/{alloc_count,free_count,used}`
at 30-second intervals for 5 minutes. Surfaces:
- DRM FB count trajectory (refutes / confirms FB leak)
- CMA alloc / free deltas (catches fragmentation)
- Independent confirmation against the userspace numbers.

**Recommended order**: A first (cheapest, highest-prior-probability
hit). If A finds the cache caps near saturation, the "leak" is
just baseline cost + can be tuned. If A finds the caches well
below cap, escalate to C (sysfs) before C2 (BO counter).

---

## §7 — What r38b ships

### §7.1 Audit doc (this file)

The deep-read findings, the negative finding for the audit's
A.3 hypothesis, and the recommendation pivot to r38c per §6.

### §7.2 Defense-in-depth fix (transition closure cleanup)

The sacred subagent review on the negative-finding audit doc
surfaced a real defense-in-depth bug in
`paint_and_present_one_transition_frame`'s closure: three `?`-
bubble sites between `cleanup_static` being **defined** and
`cleanup_static(...)` being **called** would leak the entire
bake-target resource set (fbo_a + tex_a + fbo_b + tex_b + vbo +
program ≈ 16 MB of GLES storage) on the failure path.

The three sites:

1. Line 4283 — `get_attrib_location("a_pos")?.ok_or_else(...)`.
   Fires only if the shader linker drops the attribute, which
   is effectively never for the stable VS_TEXTURED_QUAD vertex
   shader. Defensive.
2. Line 4285 — same pattern for "a_uv". Same firing profile.
3. Line 4303 — `ensure_scene_fbo(session, ...)?`. Lazy alloc
   on first call per session (or after a mode change).
   **Could fire under CMA pressure** — which is the exact
   scenario the dispatch investigates. One firing leaks
   ~16 MB until session teardown.

**Fix:** mirror the link_program / create_buffer match-arm
patterns immediately above (lines 4252-4258 and 4262-4269) —
explicit cleanup_static + delete_program before returning Err.
~30 LOC. No public API change.

**Is this the FYS root cause?** Almost certainly not. The
ensure_scene_fbo failure path is single-shot per session (once
scene_fbo is allocated, subsequent `ensure_*` calls return the
cached handle from `session.scene_fbo`). One firing leaks 16
MB; the observed FYS drift is 70 MB. To match 70 MB the bug
would need to fire ~4 times — only possible across multiple
session bring-ups, which the production sidecar doesn't do.

**Why ship it then?** Defense-in-depth. Mirrors existing
patterns. Low-risk + low-LOC. Closes the CMA pressure → scene-
FBO-alloc-fail → leak feedback loop, which is the exact
positive-feedback shape an audit cannot rule out without an
actual fix.

### §7.3 Out-of-scope fixes flagged for follow-up

Two additional latent bugs surfaced by the subagent review but
NOT shipped in r38b because they are gated out of the FYS code
paths. Worth a follow-up dispatch for non-FYS deployments:

- **`bake_external_nv12_to_current_fbo` at hdmi.rs:6583** —
  if `gl.create_texture()` for uv_tex fails after y_tex was
  successfully created, y_tex orphans (the
  `*nv12_tex = Some(...)` assignment never happens, so the
  next call re-enters dims_changed=true and allocates again).
  Per-leak: ~2 MB at 1080p. Fires only on the VLC NV12 HW-
  decode stream path (FYS has no streams).
- **`run_nv12_dmabuf_blit_pass` at hdmi.rs:10312** —
  if `gl.create_texture()` after `(eps.create_image)(...)` at
  line 10295 fails, the EGLImage (and its kernel-side
  dma_buf ref) leaks. Per-leak: ~3 MB of CMA per
  failed-but-imported NV12 frame. Fires only on the V4L2
  DMABUF VideoSlide path (gated by `OPENMARQUEE_RENDERER_DMABUF=1`
  AND VideoSlide presence; FYS has neither).

Both are real bugs but neither matches FYS's leak shape.
Recommend a future r39+ "defense-in-depth: GLES-create-failure
cleanup audit across video / stream paths" dispatch.

### §7.4 Outcome vs dispatch success criterion

The dispatch's tag was `r38b — CLOSED. CMA leak fix landed.`
On deep-read **no root-cause leak was found in the dispatch's
E.1 scope**. The 4 SUSPECT GBM scanout paths PASS; the C-scope
subagent-flagged sites PASS; held_scanout is also clean.

The shipped fix (§7.2) is a real but defense-in-depth bug, not
the FYS root cause. Recommend the pingback explicitly frame
this:
- "r38b deep-read CLOSED. No root-cause leak in E.1 scope."
- "Defense-in-depth transition-closure fix SHIPPED (mirrors
  existing canonical patterns; would close a latent CMA
  pressure → 16 MB leak feedback loop if it ever fires)."
- "Recommend r38c per §6 — cache pressure measurement first,
  sysfs probing second."

— jimmy:openmarquee-code1 (lane: code1 renderer-perf r38b)
