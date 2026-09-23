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

## Reconstruction plan (when scheduled)

1. **Recover the source.** F-1 was deployed to FYS; the source may exist only as the deployed binary or in an uncommitted worktree/stash from that session. Options, in order:
   - Search fleet stashes / worktrees / branches for `spawn_async_to_prime_for_begin_slide` or a reaper thread.
   - Diff the FYS-deployed binary's strings against a tree build (source-pin markers) to confirm which fixes it carries.
   - If unrecoverable, re-derive from the design: (a) move `prime_video_decoder` for BeginSlide onto a background worker and have the paint thread poll for readiness (never block); (b) hand dropped `DecoderInner`s to a dedicated reaper thread via mpsc so STREAMOFF/REQBUFS/fd-close/EGLImage-destroy never runs on the paint thread. **Preserve the r101 destroy-image-BEFORE-close-fd ordering** in the reaper.
2. Regression test: BeginSlide/advance paint-thread time bounded (no synchronous prime/drop on the render thread).
3. aarch64 cross-build + real-HW verify on the dev Pi (hitch gone) before merge.

## Also possibly-drifted (verify during reconstruction)

The same session referenced a "combined stack" deployed/staged on FYS: **M-1** (LruMap memory cap), **W-2** (env cache), **Item-1** (flush cache). Confirm whether each is in-tree or is additional ON-SIGN≠IN-TREE drift while recovering F-1.
