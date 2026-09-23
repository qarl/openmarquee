# Tech debt: F-1 async-prime hitch-kill deployed to FYS, never committed

**Status:** OPEN — tracked debt (admin-flagged 2026-09-22)
**Severity:** HIGH — the in-tree renderer LACKS a perf fix the production sign (FYS / JasonsSign1) HAS. Every *fresh* build (e.g. fireplacesign) ships without it.
**Owner:** Jimmy-openmarquee-code

## What's missing

During an earlier perf session, a renderer change called **"F-1 hitch-kill"** was developed and **deployed to FYS** (reported **1043 ms → 54 ms** BeginSlide/transition hitch; image PASS on glass) but **was never committed to the git tree.** Discovery on 2026-09-22 confirmed the tree lacks it:

- `spawn_async_to_prime_for_begin_slide` (or any equivalent that moves the decoder **prime** off the render/paint thread) — **ABSENT** from `renderer/`.
- **`DecoderInner::Drop` off-thread reaper** — **ABSENT**. Today `DecoderInner::Drop` (STREAMOFF + REQBUFS(0) + fd close + EGLImage destroy loop, `renderer/src/v4l2.rs:~1343`) runs **synchronously on the render/IPC thread**; eviction is synchronous in the BeginSlide handler (`ipc_main.rs:~4299` `video_decoders.remove`, `~4350` `evict_other_video_state`).

What the tree HAS instead: the `PreloadSlide` IPC op (Python fires it ahead of BeginSlide; worker at `ipc_main.rs:~4914`, joined at `ensure_preload_complete`). On a preload **miss**, BeginSlide primes **synchronously on the render thread** (`cache.load` → `prime_video_decoder`, `ipc_main.rs:~1245`) — i.e. the exact ~500 ms paint-thread stall F-1 was meant to remove.

## Why it matters (the inverse of merged-but-not-deployed)

This is drift in the dangerous direction: **the deployed binary is AHEAD of the repo.** A fresh image built from `main` (fireplacesign) is missing the FYS hitch-kill, so the hitch silently re-bites every new burn. `MERGED≠ON-SIGN` (memory `project_verify_audit_sweep_2026_07_15`) is the usual failure mode; this is its mirror — **ON-SIGN≠IN-TREE** — and just as costly.

## Relationship to Bug 4 (fireplacesign burn, 2026-09-22)

Bug 4 ("~500 ms reload stall on video loop") reframed to: the intra-hold loop already seeks cheaply; the real cost is the **synchronous prime at slide-advance on a preload miss**. Admin's call: **gate Bug 4 on a deploy-window measurement** — on the ≤720p (post-Bug-1a) image, does BeginSlide ever hit the synchronous `cache.load` prime on the paint thread, or does preload always win?

- **preload lands reliably** (≤720p prime is far cheaper than the 1080p prime that produced the ~500 ms figure) → Bug 4 is resolved by Bug 1a; close it. **This debt still stands** (recommit F-1 regardless — admin's explicit instruction — so the next burn isn't exposed).
- **still stalls** → reconstruct F-1 (async-prime-off-thread + Drop-reaper), profiling-informed.

## Source LOCATED (2026-09-22)

The F-1 source is **committed on `origin/task/perf-gl-2026-06-15`** (deployed to FYS, never merged to main). It searched clean under the name `spawn_async_to_prime_for_begin_slide` because that symbol was a guess — the actual commit is:

- **`e68ecb3`** — `perf-decode F-1 — off-thread V4L2 prime at BeginSlide for cold video paths (kills 1.5-2.6s render-thread freezes)` ← the core F-1.
- **`445766c`** — `LOAD-NEXT off-thread — F-1 BLOCKER-3 mitigation (cap-frozen WIP carried to ship)`.
- **`2ead796`** — `eviction-timing fix — evict from-side V4L2 state at end-of-transition` (the free-old / Drop-timing half; ~92% 2-decoder pressure reduction).
- **`fc2b7e6`** F-3 (gate BeginTransition from-side cache.load on O(1) presence check), **`ff616d9`** F-1 follow-up instrument.
- Combined-stack siblings on the same branch: **`3af121f`** M-1 (slide_caches → LruMap), **`dccddc0`** W-2 (thread_local env-var cache), **`6de14c3`** Item-1 (bake_offscreen_flush thread_local cache).

**Recommit is a manual PORT, not a cherry-pick.** `origin/task/perf-gl-2026-06-15` is **40 commits ahead / 282 behind** current main (a stale 2026-06-15 renderer base). `e68ecb3` lives in `ipc_main.rs`'s BeginSlide handler, which has been rewritten many times across those 282 commits (r104 serialize-decoders, r104.1 defer-eviction, CMA arc, etc.), so a cherry-pick conflicts hard. It must be re-applied against today's `ipc_main.rs`/`v4l2.rs` shape.

## Reconstruction plan (when scheduled)

1. **Source recovered** (above). Read `e68ecb3` + `445766c` + `2ead796` as the design reference; re-apply the shape onto current main: (a) move `prime_video_decoder` for BeginSlide onto a background worker, paint thread polls for readiness (never blocks); (b) move `DecoderInner` drop/eviction off the paint thread (2ead796's end-of-transition timing + a reaper). **Preserve the r101 destroy-image-BEFORE-close-fd ordering.** Cross-check whether M-1/W-2/Item-1 are also still missing from the tree while here.
2. Regression test: BeginSlide/advance paint-thread time bounded (no synchronous prime/drop on the render thread).
3. aarch64 cross-build + real-HW verify on the dev Pi (freeze gone) before merge.

**Timing:** this is a large, conflict-prone renderer change needing real-HW verify — schedule it as its own task in a deploy window, informed by Bug 4's V4 measurement (if ≤720p preload lands reliably the render-thread-freeze urgency is lower, but admin's call stands: recommit regardless so the next burn isn't exposed). Do NOT rush it as a side-job.

## Also possibly-drifted (verify during reconstruction)

The same session referenced a "combined stack" deployed/staged on FYS: **M-1** (LruMap memory cap), **W-2** (env cache), **Item-1** (flush cache). Confirm whether each is in-tree or is additional ON-SIGN≠IN-TREE drift while recovering F-1.
