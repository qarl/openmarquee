# r35 — FPS physics ceiling: static audit + root-cause candidate ranking

**Author lane:** code2 (static analysis only — no SSH, no live
perf-stats endpoint calls, no soak tests per
`[[feedback_no_soak_during_dev]]`). Same shape as r30/r31/r33/r34
audit recommendations.

**Audience:** code1 / whoever owns the renderer perf lane in a
future investigation dispatch.

**Why it exists.** Code1's r28 FYS deploy verification captured:

```
ipc.soak window_s=30 frames=606 transitions=123
         fps_avg=24.3
         paint_us=avg/5879/max/79107
         paint_us_p99=33335
```

`fps_avg=24.3` = 81% of the 30 fps target. Not catastrophic, but
the deferred perf-ceiling arc. Numbers framed against the 30 fps
budget (33333µs):

| Metric | Value | % of budget |
| --- | --- | --- |
| paint_us avg | 5879 µs | 18% — comfortable |
| paint_us p99 | 33335 µs | 100% — RIGHT AT budget |
| paint_us max | 79107 µs | **237% — single-frame blow-out 2.4× over** |
| fps_avg | 24.3 | 81% of target |

**Shape interpretation.** Most frames are fast. The 1% tail hits
budget exactly. Occasional outliers blow 2.4× over and cost ~2
frames of wall-time each. The fps_avg degradation is the
cumulative effect of those tail latencies + (per Section B.G
below) Python-side tick-budget breaches independent of Rust paint
cost.

---

## Section A — Capture inventory

### A.1 What we have

In `qa/captures/` + `qa/`:

**Perf-relevant:**
- `qa/perf-baseline-2026-05-10.json` — earliest perf baseline,
  pre-batch7
- `qa/perf-baseline-autorender-2026-05-10.json` — auto-render
  variant of same
- `qa/perf-batch7.1-after.json` + `qa/perf-batch7.2-after.json`
  — post-Batch-7 perf passes
- `qa/paint-slide-profile-2026-05-14.md` — paint-slide phase
  profile snapshot
- `qa/sidecar-sustained-smoke-2026-05-13.md` + the two
  post-cache-fix / post-transition-cache variants — sidecar
  smoke perf tracking the cache fixes
- `qa/slide-boundary-characterization-2026-05-14.md` — slide
  boundary cost characterization
- `qa/captures/phase-d-strict-30fps-recon-2026-05-17.md` — the
  Phase D criterion-shape recon. Documents the
  `ipc.soak` format contract + `profile.rs --profile-frames N`
  CLI gating + the 6-h soak harness at
  `scripts/renderer_pi_soak_ipc.sh`.

**Inner-repo perf instrumentation (still in source):**
- `renderer/src/ipc_main.rs:104-200` — `IpcPaintMetrics`
  per-Advance paint timing; emits `ipc.soak` lines every 30s.
- `renderer/src/ipc_main.rs:161` — `PERF_STATS_JSON_PATH =
  "/var/openmarquee/perf-stats.json"` — sidecar emits aggregated
  JSON {fps_avg, paint_us_{avg,max,p99}, ...} on every window
  flush.
- `renderer/src/profile.rs` — full per-frame phase histogram
  (mean/p50/p95/p99/max across ~40 phases). LOCAL-DIAGNOSTIC only,
  NOT IPC-wired. Enabled by `--profile-frames N` CLI flag.
- `backend/openmarquee/playback.py:302-339` — Python tick-budget
  bookkeeping. 33 ms threshold. Emits a warn-log
  `playback: tick over budget: %.1fms (slide_id=%s phase=%s)` to
  the journal, rate-limited to 5s.
- `backend/openmarquee/playback.py:341-377` — `get_loop_stats()`
  endpoint accessor with p50/p95/p99/max/ticks_over_budget.

### A.2 What we DON'T have

The capture format is rich enough to identify WHEN we breach
budget but not WHICH transition / slide / phase is the breach
mechanism:

- **Per-slide breakdown.** `ipc.soak` aggregates across all
  slides + transitions in the window. The Python "tick over
  budget" warn DOES log `slide_id` + `phase` — that's the
  attribution surface we already have.
- **Per-transition kind breakdown.** No instrumentation
  distinguishes `paint_us` for kind=fade vs kind=wipe vs
  kind=cut vs kind=glitch.
- **Per-frame phase histogram on FYS.** `profile.rs` exists but
  is `--profile-frames N` LOCAL only — never streamed off-device.
  Plumbing it into `ipc.soak` is the Phase D §3.4 gap from
  `qa/captures/phase-d-strict-30fps-recon-2026-05-17.md`.
- **Frame-precise root-cause attribution.** No instrumentation
  attributes a specific 79ms frame to its causal mechanism (cold
  shader compile? V4L2 keyframe decode? slide_caches
  invalidation? non-Latin glyph rasterize? Python sleep_until
  blip?).

### A.3 The cheapest existing-instrumentation cross-reference

`playback.py:333-339` LOGS `slide_id=<UUID>` + `phase=<str>` for
every tick-over-budget. journalctl on FYS already carries these.
A single `journalctl --no-pager -u openmarquee-backend |
grep "tick over budget"` produces the full set of FYS
budget-breach slide_ids without any new code.

**Step 1 of every recommended investigation below = parse that
log.**

---

## Section B — Candidate ranking

For each candidate, the mechanism, plausibility, investigation
cost, and provisional fix shape.

### B.A — Transition rendering cost (cold-start residual)

**Plausibility:** MEDIUM-LOW.

**Mechanism.** r25's `prewarm_sp_session` at
`renderer/src/hdmi.rs:11560-11815`:

- Pass 1: resolves every text slide's layers + populates
  `slide_caches` + ensures bg_cache for non-solid bgs (lines
  11569-11644).
- Pass 2: dedupes (kind, n_a, n_b) tuples + compiles SP /
  composite programs (lines 11646-11748). Walks all (i-1, i)
  pairs INCLUDING the wrap (last, first) pair.
- Atlas FBO warm: `ensure_bake_atlas` + clear + flush (lines
  11761-11788).
- Pre-link `bright_gamma` + `overlay_blend` + `blit` programs
  (lines 11799-11807).

Coverage is GOOD for the common-case reel. The lazy fallback paths
remain in place (every `if let Err(e) = ... lazy on first call`)
so an uncovered (kind, n_a, n_b) tuple OR a NotSinglePass /
ExceedsBakeCap dispatch still pays first-call cost.

**Plausible breach paths:**
1. `classify_prewarm_pair` returns `NotSinglePass` or
   `ExceedsBakeCap` → fall through to legacy 3-pass; prewarm
   skipped that pair. First runtime hit pays cold-link cost. Per
   `hdmi_logic.rs:2160-2170`: bake-cap is at 6 layers; reels with
   layer counts > 6 take this path.
2. Atlas FBO warm only fires when `composite_count > 0`. A reel
   of only-SP-tier transitions skips it → first non-SP
   transition pays cold-FBO cost.

**Static rule-out partial:** if `ipc.soak` `session_frames` for
the 30s window is the FIRST window after sidecar boot, the
prewarm savings show up as a much-better fps_avg in window 1+
vs window 0. r28's window_s=30 didn't anchor "window 0 vs
later"; can't distinguish from this single capture.

**Investigation cost.** CHEAP. Re-capture `ipc.soak` for windows
0, 1, 5, 30 — does fps_avg improve in window 1+? If yes,
prewarm is doing the work and B.A is largely closed.

**Fix shape if real:** extend `consider_pair` to also walk
`PrewarmTier::NotSinglePass` and `ExceedsBakeCap` — but those by
construction need the legacy 3-pass that has no prewarm hook.
The fix is the more expensive route: pre-link the legacy 3-pass
shaders too. ~30 LOC in prewarm_sp_session; hdmi.rs.

### B.B — V4L2 decode jitter (steady-state)

**Plausibility:** MEDIUM.

**Mechanism.** `renderer/src/v4l2.rs` + `renderer/src/ipc_main.rs`
H.264 codec dispatch. Per `v4l2.rs:391-411` the MMAP path uses
LIM_RANGE FS_NV12_TO_RGB shader (BT.709 per r32). Per
`hdmi.rs:3520` (now corrected by r33's BT.709 fix), the
per-advance VideoSlide paint flow:

> feeds the next H.264 sample (if any), drains the next decoded
> NV12 Frame (short EAGAIN retry budget), uploads Y + UV planes
> to GLES textures, blits through the BT.709 NV12 -> RGB shader,
> swaps + commits

Steady-state P-frame decode on bcm2835-codec is ~10-15ms per
frame on Pi Zero 2 W (empirical from earlier reports). KEYFRAME
(I-frame) decode is harder, can spike 25-40ms. A reel containing
VideoSlide content hits a keyframe every GOP boundary.

r6 mitigated COLD-START with a warmup pre-feed + EAGAIN budget
bump. Steady-state keyframe variance is NOT mitigated; the
EAGAIN budget is finite (`assert_capture_quantization_compatible`
+ retry budget).

**Plausibility for 79ms max:** moderate. A keyframe-decode
collision with a transition (paint_slide running a SB-tier
transition while the V4L2 decoder is still draining a keyframe)
could plausibly produce a 50-80ms outlier.

**Investigation cost.** MEDIUM. Cross-reference the slide_ids
from the journalctl over-budget log against playlist content:
- If breach slide_ids are exclusively text/image → V4L2 not
  involved → rule out B.B
- If breach slide_ids include VideoSlide → look at frame_in_video
  for those breaches; correlate with GOP boundaries

**Fix shape if real:**
- Move H.264 decode off the paint loop's critical path by
  pre-feeding +1 frame ahead (decode buffer; draw last-decoded).
  ~50-100 LOC in hdmi.rs + v4l2.rs.
- OR shorten GOP (lock VideoSlide encodes to GOP ≤ 30 frames at
  30fps).

### B.C — Memory pressure / cache eviction

**Plausibility:** LOW.

**Mechanism.** Dispatch's framing references
`SLIDE_CACHE_CAP = 32` but **no such constant exists in the
codebase** (`grep -rn "SLIDE_CACHE_CAP" renderer/src/` returns
zero matches as of HEAD `c0b5fdd`).

The actual caches:
- `session.slide_caches` (hdmi.rs ~688): a `HashMap` with NO
  explicit capacity cap. Entries created on first use of a slide
  (line 11598-11609 in prewarm). Eviction (lines 11603-11605,
  free_slide_render_cache) triggers ONLY on layer-count mismatch
  for the same slide_id.
- `image_bg_cache: ImageBgCache::with_capacity(IMAGE_BG_CACHE_CAPACITY)`
  (hdmi.rs:688) — has a cap, evicts LRU.
- `image_slide_tex_cache: image_slide_tex::ImageSlideTextureCache::with_capacity(...)`
  (hdmi.rs:689) — has a cap, evicts LRU.
- `glyph_cache.rs::evict_lru_ready` (lines 425-... ): atlas-page
  level LRU eviction triggered on cache-miss-with-full-page.
  Atlas page is `ATLAS_DIM=2048` × 2048 at
  `atlas_page.rs:28`.

**Static rule-out:** `slide_caches` has no cap → no triggered
eviction in steady state. The image cache + glyph atlas DO evict
but their evictions are infrequent on a stable reel.

**The 512 MB Pi Zero 2 W is tight** — but the cache memory
footprint is bounded (image cache + glyph atlas have explicit
caps; slide_caches per-entry size is small). No evidence of
swap-thrashing or OOM-near in r28's capture (no OOM signal in
the verification log).

**Conclusion:** static rule-out. Re-flag if a future capture
shows /proc/meminfo MemAvailable < 20 MB during over-budget
ticks. Phase 9b's soak parser already gates on OOM signals; clean
to date.

**Investigation cost.** ZERO. Already statically ruled out.

### B.D — Pi system contention

**Plausibility:** MEDIUM-LOW.

**Mechanism.** ksoftirqd preemption, DRM/KMS atomic flip
scheduling on the vc4 driver, sd-card I/O blocking (`flock.json`
writes), network NIC interrupts (Tailscale keep-alive ~6/min),
the openmarquee-wifi-watchdog cron firing twice a minute. Any of
these can steal CPU from the renderer's tick for milliseconds.

The brcmfmac WiFi firmware itself is known to spike CPU usage
(per the `[[reference_pi_zero_2w_brcmfmac_dual_mode_data_plane]]`
memory + the watchdog's existence). On signs that join customer
WiFi, brcmfmac handles both AP + STA on the single radio (Option
A topology); contention is real.

**Plausibility for 79ms max:** moderate but bounded. A
single-frame preemption of 50+ ms is possible but uncommon. Most
preemption events are < 10ms.

**Investigation cost.** HIGH. Would need:
- /proc/interrupts + /proc/stat captures at sub-second granularity
- perf record on the renderer process for a 5-min window
- Cross-correlation with the journalctl over-budget log times

Cannot do statically. The dispatch's no-soak rule probably blocks
the kind of capture needed.

**Fix shape if real:**
- Pin the renderer process to a CPU core (CPUAffinity in the
  systemd unit). ~3 LOC.
- IRQ affinity tuning (move brcmfmac IRQs off the renderer's
  core). ~5 LOC in install.sh.
- Real-time scheduling for the renderer (SCHED_FIFO with capped
  priority). ~3 LOC.

### B.E — Glyph cache cold paths despite atlas

**Plausibility:** HIGH.

**Mechanism.** The dispatch's framing was "non-ASCII codepoints
+ emoji are NOT in r25's prewarm set (only printable ASCII
U+0020..U+007E)" — **the actual mechanism is slightly different
but in the same direction**:

The MSDF atlas baked at build time covers **191 codepoints**:
Basic Latin printable (U+0020..U+007E) + Latin-1 Supplement
printable (U+00A0..U+00FF). Per `sdf_atlas.rs:284-287`:

> 191 codepoints per recon: Basic Latin printable + Latin-1
> Supplement printable. Some fonts skip a few codepoints if
> the .ttf doesn't carry them; lower bound is conservative.

Anything OUTSIDE that range (Cyrillic, CJK, full Unicode
punctuation, accented Latin-Extended, math, emoji) falls
through to the runtime `GlyphCache` worker pool
(`renderer/src/glyph_cache.rs:255-390`).

The runtime path:
1. `get_or_request(key)` → `SlotState::Requested` → worker pool
   picks it up
2. Worker rasterizes via `rasterize_msdf_cell`
   (`glyph_cache.rs:685`) for MSDF, `rasterize_colr_cell`
   (`glyph_cache_colr.rs`) for COLRv1 emoji
3. Worker pushes `Completion::Ready` (or `FontMissing` for
   tofu codepoints — per Bug 3 Slice 2D the noto-fallback chain
   handles this)
4. Render thread polls the completion channel; on completion
   count > 0, **invalidates slide_caches** so the next paint
   re-resolves with the now-Ready glyph

Per `glyph_cache.rs:340-351`:

> Channel signal for render-thread poll: paint_and_present
> invalidates slide_caches on completion count > 0. Without
> this, FontMissing state changes would only be observed on
> the next natural slide_caches eviction.

**This is the killer.** First frame on a slide with non-Latin
text or emoji pays:
- The worker's rasterization (msdfgen: ~5-15ms per glyph, COLRv1
  paint trees + tiny-skia: ~10-30ms per glyph)
- slide_caches drain → next paint re-resolves all layers
- paint_slide rebuilds glyph quads + uploads to atlas

**Plausibility for 79ms max:** HIGH. An emoji slide with even one
COLR codepoint can cost 30+ ms on first hit. The slide_caches
invalidation amplifies because the NEXT frame re-resolves layers.

**Investigation cost.** CHEAP. Two-step static check:
1. Cross-reference the journalctl over-budget slide_ids
   against playlist content. Any slide with non-Latin / emoji
   text in the breach set → candidate E confirmed for THAT slide.
2. `grep "glyph_cache worker: rasterize" /var/log/openmarquee-backend.log`
   surfaces worker activity. If worker rasterize lines correlate
   with budget breaches, candidate E confirmed system-wide.

**Fix shape if real:**

Option E1 (cheap): expand the build-time MSDF atlas to cover
more codepoint ranges (Latin Extended-A U+0100..U+017F, common
punctuation U+2000..U+206F, currency U+20A0..U+20CF). ~+200
codepoints × 191 in baseline = ~400 total. Build-time cost only;
zero runtime cost increase. ~5 LOC in atlas build script +
recon update.

Option E2 (medium): prewarm runtime GlyphCache on session bring-up
with the codepoints actually USED in the playlist. Walk every
TextSlide's text content, enumerate codepoints, `get_or_request`
each one BEFORE the paint loop opens. Bounded by playlist
content. ~30-50 LOC in `prewarm_sp_session`.

Option E3 (expensive): keep the worker async but PAINT a stale
placeholder (or skip the layer) until the glyph is ready, instead
of invalidating slide_caches. Removes the "invalidate cascade" cost
entirely. Larger refactor, ~150 LOC; introduces a layered-text
quality regression (placeholder pixels for ~1 frame).

**Recommendation:** Option E1 + E2 stack. E1 covers the
common-case (any text without specific non-Latin/emoji), E2
covers the playlist-specific edge cases.

### B.F — Compositor pass count

**Plausibility:** MEDIUM.

**Mechanism.** Multi-layer slides (text + bg + maybe image fg)
→ multiple GLES blit passes. Transition types vary:
- SP-tier (single-pass, kind=fade/wipe ≤ 6 layers per side):
  one blit
- ScissoredBake (kind=fade/wipe > 6 layers per side, OR
  kind=glitch/checker/etc.): bake_a + bake_b + composite = 3
  passes

paint_us avg = 5.9 ms suggests STEADY-STATE composite is
comfortable. The 33 ms p99 is HARDER — does this correspond to
SB-tier transitions specifically?

**Plausibility for the p99 tail:** moderate. SB transitions are
~3× the work; reels with frequent kind switches between SP and SB
tiers would naturally produce a fat p99 from the SB transitions.

**Investigation cost.** CHEAP. r25's prewarm logging at
`hdmi.rs:11811-11813` emits
`"reel: prewarm complete -- N slide texts rasterized, X programs
compiled (sp=Y composite=Z), atlas_warmed=...":`
Y > 0 + Z > 0 means BOTH SP + SB tiers are in the reel. Pulling
this line from FYS journal answers "is this an SB-heavy reel?".

If SB-heavy: investigation B.F is HIGH plausibility for the p99
specifically. The 79ms max likely lives elsewhere (B.E or B.D).

**Fix shape if real:**
- Pre-bake the SB composite for the steady-state slide pairs at
  prewarm time. ~50 LOC in prewarm_sp_session.
- OR widen the SP eligibility (currently bake_cap = 6 layers per
  side) — moves more transitions to SP tier. ~10 LOC in
  hdmi_logic.rs::classify_prewarm_pair. Trade-off: SP-tier may
  not handle every kind cleanly.

### B.G — Python tick budget overhead

**Plausibility:** HIGH.

**Mechanism.** `backend/openmarquee/playback.py:302-339`
records each tick's WORK delta (excludes the `_wait` slack).
Threshold `TICK_BUDGET_NS = 33_000_000` (33 ms = 1/30s). Over-
budget warn log:
```
playback: tick over budget: 38.1ms (slide_id=<UUID> phase=<str>)
```

**FYS already saw this fire.** The dispatch directly cited
`"playback: tick over budget: 38.1ms"`. That breach is
Python-side — IPC roundtrip + readline + tick work. Even if Rust
paint takes 5 ms, if Python's wrapper + IPC takes 30+ ms, the
effective fps_avg is 1000ms / 33ms = ~30 → 1000ms / 38ms = ~26.
The 38ms warn → ~26fps directly. The r28 fps_avg=24.3 ≈ matches
a sustained ~38-40ms tick.

**Plausibility for the 24.3 fps_avg specifically:** HIGH. fps_avg
is the integrated effect of every tick, Python-side or
Rust-side. Even with Rust paint avg=5.9ms, if Python tick avg is
~40ms, the fps_avg is bound by Python.

**Investigation cost.** CHEAP. Parse the existing tick-budget
warn-logs from FYS journalctl. Each log line already carries the
breach magnitude + slide_id + phase. Get the distribution; if
Python ticks are sustained > 33ms across many slide_ids, B.G is
the dominant cause.

**Fix shape if real:**

- Reduce IPC roundtrip cost. Current shape is line-based
  text over stdin/stdout (per `ipc_main.rs`). A binary protocol
  + length-prefix framing would shave 1-2ms but isn't an
  order-of-magnitude win.
- Move the tick orchestration to Rust entirely. The Python
  PlaybackLoop is currently the orchestrator + state machine;
  moving to Rust eliminates IPC roundtrip per tick. Large
  refactor (~500+ LOC), high regression risk.
- Tighter `await self._wait` math. Looking at
  `playback.py:1112` + `:1195`: `await self._wait(min(tick_period,
  remaining))`. The wait grain is `tick_period` (~33ms). On a
  busy tick the wait collapses to 0 and the next tick fires
  immediately — but the WORK part still measures over budget.
  No fix shape here that doesn't move work out of Python.
- Profile the Python side directly. `cProfile` or `py-spy
  --duration 60` on FYS for 1 minute would attribute the ticks
  to specific Python frames. Hard to do without SSH (which is
  out of my lane). Code1's lane.

---

## Section C — Cheap static rule-outs

From the candidate ranking above, the following are CHEAP to
rule out from static reading alone:

### C.1 RULED OUT: SLIDE_CACHE_CAP eviction (B.C)

The dispatch framing references `SLIDE_CACHE_CAP = 32`. **This
constant does not exist** in the codebase (`grep -rn
"SLIDE_CACHE_CAP" renderer/src/` returns zero matches).
`session.slide_caches` is a HashMap with no cap; eviction only
on layer-count mismatch per-slide.

Image cache + glyph atlas DO have LRU evictions but their
miss-on-eviction cost is bounded:
- `IMAGE_BG_CACHE_CAPACITY` cap (hdmi.rs:688) → re-paint cost
  on miss
- `ATLAS_DIM = 2048` (atlas_page.rs:28) glyph atlas page size →
  miss triggers worker rasterize (covered by B.E)

Net: **B.C is not the dominant cause of the 79ms max or the 24.3
fps_avg.** Re-flag if a future capture shows /proc/meminfo
MemAvailable < 20 MB during over-budget ticks.

### C.2 RULED OUT (partial): cold-start residual (B.A subset)

The r25 prewarm covers:
- Every SP-tier (kind, n_a, n_b) tuple via consider_pair loop
  including the wrap (last, first) pair (hdmi.rs:11734-11747)
- Atlas FBO + clear + flush (hdmi.rs:11761-11788)
- bright_gamma + overlay_blend + blit pre-link
  (hdmi.rs:11799-11807)

What it does NOT cover (per `classify_prewarm_pair` in
`hdmi_logic.rs:2160-2170`):
- `PrewarmTier::NotSinglePass` — runtime falls through to legacy
  3-pass; nothing for prewarm to do
- `PrewarmTier::ExceedsBakeCap` — bake-cap is 6 layers per side;
  reels with ≥7 layers/side fall through to 3-pass

If the FYS reel has ≥7-layer slides AND lazy-3-pass tuples not
seen in earlier windows, the FIRST runtime hit pays cold-link
cost. A single capture from window 0 can confirm/reject by
comparing window-0 fps_avg to window-30 fps_avg.

### C.3 RULED OUT: OOM / swap thrashing

r28's `ipc.soak` capture contains no OOM signal +
no backend crash signal. Phase 9b's parser at
`scripts/renderer_pi_soak_ipc_parse.py:172-194` gates on both;
they would have surfaced.

### C.4 PARTIAL RULE-OUT: B.B V4L2 jitter

If the FYS reel contains NO VideoSlides (only Text + Image),
V4L2 jitter is impossible by construction. Static check on
the FYS playlist content. If VideoSlide present, candidate B.B
remains in scope.

---

## Section D — Top-3 candidates + cheapest-next-investigation

### D.1 Ranked top-3 (post-rule-out)

1. **B.G — Python tick budget overhead** (HIGH plausibility, FYS
   capture already shows the breach magnitude)
2. **B.E — Glyph cache cold paths for non-Latin / emoji** (HIGH
   plausibility for the 79ms max if reel has non-Latin or emoji
   text)
3. **B.F — Compositor pass count for SB-tier transitions**
   (MEDIUM plausibility for the 33ms p99 specifically)

Lower:
4. B.A — Cold-start residual (partial rule-out; recheck with
   window-0-vs-later capture)
5. B.B — V4L2 jitter (gated on whether VideoSlides are in the FYS
   reel)
6. B.D — Pi system contention (hard to verify without on-device
   capture, code1's lane)
7. B.C — Memory pressure / cache eviction (statically ruled out)

### D.2 Cheapest-next-investigation (CHEAP, free)

**One pass.** Cost: ~5 minutes of journalctl parsing.

```bash
# On code1's lane (SSH to FYS):
journalctl --no-pager -u openmarquee-backend --since "2 hours ago" \
    | grep -E "tick over budget|glyph_cache worker: rasterize|reel: prewarm complete" \
    > /tmp/r35-evidence.log

# Then locally:
# Step 1: enumerate over-budget slide_ids + phases
grep "tick over budget" /tmp/r35-evidence.log \
    | sed -E 's/.*slide_id=([^ ]+) phase=([^;]+).*/\1 \2/' \
    | sort | uniq -c | sort -rn

# Step 2: identify glyph_cache worker activity correlated with breaches
grep -B 1 -A 1 "tick over budget" /tmp/r35-evidence.log

# Step 3: confirm prewarm coverage
grep "reel: prewarm complete" /tmp/r35-evidence.log
# Look for sp=N composite=M — if M > 0, SB-tier in reel
```

**Output answers:**

- Which slide_ids breach budget? → If exclusively text/image,
  rule out B.B. If VideoSlide present, B.B stays in scope.
- Are glyph_cache worker rasterize lines correlated with
  breaches? → Confirms B.E for those slides.
- Is the reel SP-only or mixed SP/SB? → Bounds B.F.
- How frequent are tick-over-budget warns (rate-limited to 5s)?
  → Bounds B.G frequency.

This is a SINGLE-shell-session investigation. No new code, no
soak test, no production-flag changes. Cost is ~5 min of code1
SSH-then-parse work.

### D.3 Cheapest-next-investigation (BUDGET)

If D.2 is inconclusive (e.g., "tick over budget" warns are rare
but fps_avg is still 24.3), the next-cheapest is:

**Run renderer with `--profile-frames 300`** on FYS for ~10
seconds of motion. This dumps the full per-frame phase histogram
to stderr on exit (`renderer/src/profile.rs`). Cross-reference
the p95/p99 phases against B.A/B.E/B.F mechanisms.

Cost: requires a brief renderer-binary swap on FYS (the soak
binary doesn't have --profile-frames hot-pluggable). ~15 min
code1 SSH + binary swap + capture + revert.

### D.4 Investigation NOT recommended (per session charter)

- 6-hour soak test (`scripts/renderer_pi_soak_ipc.sh --duration
  6h`). Per `[[feedback_no_soak_during_dev]]`. Release-candidate
  gated.
- Adding new perf instrumentation (Phase D §3.4 gap: streaming
  the profile.rs histogram into ipc.soak). Out of scope until
  D.2/D.3 narrows the cause.

---

## Section E — Provisional fix shapes (DO NOT IMPLEMENT in r35)

For the top-3 candidates only.

### E.1 If B.G confirmed (Python tick budget)

Option 1 — IPC roundtrip optimization. Bin protocol + length
prefix. ~50-100 LOC across `ipc_main.rs` + `playback.py`. Shaves
maybe 1-2 ms per tick. NOT order-of-magnitude.

Option 2 — Move tick orchestration to Rust. Eliminates Python-
side IPC per-tick entirely. Backend keeps the REST API +
state-machine but the playback loop tick runs in the Rust
sidecar. ~500+ LOC refactor. High value, high regression risk.

Option 3 — Reduce IPC frequency. Currently each Rust paint
requires a Python tick to send the next PaintSlide / PaintTrans
command. Could batch N commands per IPC roundtrip; Rust executes
them locally. ~80 LOC. Reduces tick frequency but at cost of
state-machine fidelity.

**Recommendation:** profile Python first (py-spy) to confirm
which Python frames dominate. Then pick the matching option.

### E.2 If B.E confirmed (glyph cache cold paths)

Option E1: expand build-time MSDF atlas to cover Latin Extended-A
+ common punctuation + currency. ~+200 codepoints. Build-time
cost only. ~5 LOC in atlas build script.

Option E2: prewarm runtime GlyphCache with the codepoints USED
in the playlist. Walk every TextSlide's text content, enumerate
codepoints, `get_or_request` each BEFORE the paint loop opens.
~30-50 LOC in `prewarm_sp_session`.

Option E3: paint-stale-placeholder during async rasterize
(don't invalidate slide_caches on completion). ~150 LOC. Quality
regression (1-frame placeholder).

**Recommendation:** E1 + E2 stack. E1 covers common case; E2
covers playlist-specific.

### E.3 If B.F confirmed (SB compositor pass count)

Option F1: pre-bake SB composite at prewarm time for steady-
state slide pairs. ~50 LOC in `prewarm_sp_session`.

Option F2: widen the SP-tier bake_cap from 6 layers/side to 8 or
10. ~10 LOC in `hdmi_logic.rs::classify_prewarm_pair`. Trade-off:
SP-tier may not handle every kind cleanly past 6 layers.

**Recommendation:** F1 first (preserves SP/SB tier discipline);
F2 only if F1 doesn't move the needle.

---

## Section F — Open questions for qarl / QA

### F.1 Strict-30 ceiling vs operator-perceived

The phase-d-strict-30fps-recon doc surfaces that the SPEC says
"30 fps steady" + "no visible stutter" not "strict 30 fps." The
24.3 fps_avg is 81% of target. **Is the strict-30 the ship
target, or is "operator says it looks smooth" the actual
target?** Phase D recon recommended (b) windowed-strict
`p99 ≤ 33.33 ms AND fps_avg ≥ 30` over a rolling 10-min window —
that's not what r28 captured.

If the answer is "operator-perceived," the 24.3 fps_avg may be
acceptable for v1.x.x patch ship, and Section D's investigation
defers to a v1.x minor or v2.0.

### F.2 FYS reel composition

Does the FYS reel currently contain:
- VideoSlides? → gates B.B in/out of scope
- non-Latin or emoji text? → gates B.E plausibility
- > 6-layer slides? → gates B.A residual
- SB-tier transitions (kind ∈ {glitch, checker, custom}; or
  kind=fade/wipe at > 6 layers/side)? → gates B.F

A single read of the FYS playlist.json answers all four. Out of
my lane; code1's SSH gives the answer in <1 minute.

### F.3 New perf-stats schema

`/var/openmarquee/perf-stats.json` (per `ipc_main.rs:161`)
emits {fps_avg, paint_us_{avg,max,p99}}. Does it carry
per-slide-id breakdown? If yes, D.2 is even cheaper (no
journalctl tail needed). If no, adding {slide_id ->
paint_us_summary} would be a ~30 LOC plumbing change but
falls outside r35's static-analysis scope.

### F.4 r25 prewarm window-0 anchor

r25's `prewarm_sp_session` fires at sidecar boot. `ipc.soak`
window 0's fps_avg should be BELOW window 1's fps_avg if cold-
start residual (B.A) is real. **Did r28's verification capture
window 0 specifically, or a later window?** If a later window
(steady-state) and fps_avg is 24.3, cold-start is RULED OUT
and 24.3 is the steady-state ceiling.

### F.5 The 5-second tick-over-budget warn rate limit

`playback.py:330` rate-limits the warn to 5s. **How many
breaches are we missing?** A burst of 10 breaches in 1s shows up
as 1 log line. If `get_loop_stats()` over the same window reports
`ticks_over_budget: 50`, the journal undersells the breach rate
10x.

Recommendation: query `/api/playback/loop-stats` on FYS for the
live `ticks_over_budget` count over a 10-min window.
Cross-reference with the journalctl warn-log count over the same
window. If journal count = ticks_over_budget, rate-limit fires
once per breach. If journal count << ticks_over_budget, we're
seeing a small sample.

### F.6 Tick-over-budget phase attribution

`playback.py:334` logs `phase=<str>`. What are the canonical
phase strings? `_play_via_rust_ipc` is one. Are there others?
Phase-distribution of breaches would narrow the dominant cause.

---

## Hand-off shape

1. **qarl / QA reads this audit** + answers F.1-F.6.
2. **Code1 runs D.2** (the ~5-min journalctl parse) on FYS.
   Reports back: breach slide_id distribution, glyph_cache worker
   activity correlation, reel SP/SB composition.
3. **Code1 + QA pick** which of B.E / B.F / B.G is the dominant
   cause from D.2 output.
4. **Future dispatch** authorizes one of Section E's fix shapes
   based on D.2 output + qarl's F.1 ship-target call.
5. **Verification** is a follow-up `ipc.soak` capture after the
   fix, comparing fps_avg / paint_us_p99 / ticks_over_budget
   against r28's numbers.

---

## Out-of-scope items flagged for follow-up

- **Phase D §3.4 perf-stats schema extension** (streaming
  profile.rs histogram into ipc.soak). Needed before a robust
  ship-gate but out-of-scope of r35's static audit.
- **6-hour soak** per `[[feedback_no_soak_during_dev]]`. RC-
  gated.
- **B.D Pi system contention** investigation. Requires on-device
  profiling that code1 must run.
- **F.6 phase canonicalization** — survey of phase strings in
  `playback.py`. Cheap; can fold into D.2 output.
- **Build-time atlas codepoint coverage expansion** (Option E1).
  Easy to ship if QA wants it BEFORE D.2's investigation
  pin-points B.E specifically — defense-in-depth move.

— jimmy:openmarquee-code2 (lane: code2 static perf audit)
