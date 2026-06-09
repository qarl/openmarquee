# Hardware ceilings — bcm2835-codec dual-decode (Pi Zero 2 W class)

**Last updated:** 2026-06-09 (r97)
**Empirical source:** FYS Sanity Check playlist, 2026-06-09

## TL;DR

The Pi Zero 2 W (BCM2710 SoC) runs the bcm2835-codec H.264 block at
roughly half the clock of a Pi 4 (BCM2711). Empirically the H.264
hardware block on this tier sustains:

- **One 1080p30 H.264 decode** comfortably (the spec'd ceiling).
- **Two ≤720p30 H.264 decodes in parallel** — dual text-over-video
  transitions work.
- **Two simultaneous 1080p30 decodes EXCEEDS the budget** —
  transitions silently degrade to cuts because the new decoder gets
  ~0 frames during preload while the old decoder is still streaming.

This is **not** enforced by the Linux `bcm2835-codec` driver. The
limit lives in the closed-source VPU firmware (`start.elf`) H.264
block scheduler. Higher-tier hardware (Pi 4, Pi 5) lifts the ceiling
proportional to VPU clock; **do not** bake "1080p is forever banned"
into platform-agnostic code paths.

## Empirical characterisation

2026-06-09 FYS test, Sanity Check playlist (2 text slides with
video backgrounds; iris + wipe transitions; 1.5s transition windows):

| Resolution | Encoding | Result                              |
| ---------- | -------- | ----------------------------------- |
| 1920×1080  | High p., B-frames=2, ~2.8 Mbps, 30fps | `transitions=0 fps_avg=2.6` over 33s |
| 1280×720   | matched seed encoding (same profile / bitrate scaled) | `transitions=10 fps_avg=13.5` over 33s |

Resolution is the dominant driver; encoding profile is a minor
confounder. The 1080p case shows:

```
[perf] preload_handoff slide_id=... frames_drained=0 \
  prime_only_us=~55000 drain_us=~503000 budget_ms=500
[perf] vpu_mmal_components=2 delta=+1
[perf] transition_endpoint_b_unconsumed elapsed_ms=~5400 \
  reason=marker_overwritten_by_new_BeginTransition
```

every cycle. The 720p case shows `frames_drained ≥ 1`,
`vpu_mmal_components` oscillating between 1 and 2 cleanly within
the preload+transition window, and zero `endpoint_b_unconsumed`
warnings.

## Why the driver doesn't catch this

Source: `drivers/staging/vc04_services/bcm2835-codec/bcm2835-v4l2-codec.c`
(rpi-6.12.y). The `MAX_*` constants are compressed-input-buffer
sizing (the 720p threshold at L142-150 is just a buffer-size
heuristic, NOT a concurrency gate). There is no
`MAX_DECODER_INSTANCES`, no pixel-rate budget, no QoS arbiter.
`open(2)` on `/dev/video10` from a second process / second
file-descriptor will succeed, `S_FMT` will negotiate, `STREAMON`
will return ok. The scheduler is purely first-come-first-served
on VPU cycles, and the cycle budget on BCM2710 happens to be
insufficient for two 1080p30 streams.

## What r97 ships (graceful degrade)

The `PreloadSlide` IPC arm (`renderer/src/ipc_main.rs`) now defers
preload IFF:

1. At least one V4L2 decoder is currently live
   (`cache.video_decoders.len() >= 1`), AND
2. The incoming preload's slide has a video background
   (`ContentItem::Video(_)` or `ContentItem::Text(s)` with
   `s.background_video_slide_id.is_some()`).

When deferred, the renderer skips opening decoder #2 ahead of the
transition. The subsequent `BeginSlide` runs `cache.load` synchronously
AFTER `evict_other_video_state` STREAMOFFs the previous decoder —
decoder #2 then opens into an empty codec and gets immediate VPU
service. The cost is the first ~150ms of the transition where
endpoint_b has no frame; bake_b's Path B poll (r94) catches the
first frame in that window. Visible: slide-A holds ~150ms longer
than the spec'd 1.5s; iris/wipe then animate normally for the
remaining ~1.35s.

When EITHER condition is false: existing 500ms-ahead preload runs
unchanged. Specifically:
- Solid-bg on incoming slide → no defer (no decoder needed)
- Video-bg on incoming but no active decoder (first video slide
  entering, or coming from a solid-bg slide) → no defer
- Solid→video, video→solid, solid→solid all unchanged

Telemetry:

```
[perf] preload_deferred_for_codec_contention slide_id=... \
  active_decoder_count=1 active_decoder_ids=[...] deferral_us=...
[perf] preload_handoff slide_id=... frames_drained=0 \
  prime_only_us=0 drain_us=0 budget_ms=0 was_deferred=true
```

Normal-path preloads carry `was_deferred=false` so QA can grep
either way.

## Operator guidance

When authoring playlists where two **consecutive** slides both have
video backgrounds, keep the videos **≤720p** on Pi Zero 2 W class
hardware. r97's graceful-degrade path prevents the catastrophic
"transition silently jump-cuts" failure mode for dual-1080p, but
the result remains a degraded experience vs ≤720p:

| Playlist shape                    | Outcome                                                   |
| --------------------------------- | --------------------------------------------------------- |
| Two consecutive ≤720p video bgs   | Transitions animate normally                              |
| Two consecutive 1080p video bgs   | First ~150ms held; remaining ~1.35s animates              |
| Mix (video+solid alternating)     | Unchanged behavior at any resolution                      |
| Single video slide                | Unchanged behavior at any resolution                      |

## What r97 explicitly does NOT do

- Does NOT bake a `1080p_forbidden` constant or assert into
  platform-agnostic code paths.
- Does NOT cap max-pixels-per-decoder.
- Does NOT reject 1080p uploads.
- Does NOT refuse to render dual-1080p — it degrades gracefully.

Higher-tier hardware (Pi 4, Pi 5) is expected to lift the ceiling
proportional to VPU clock. If a future port runs there, the
graceful-degrade path will simply rarely fire (the contention
condition may not appear at all if the second decoder always gets
its preload window).

## Forward references

- `renderer/src/ipc_main.rs` — `should_defer_preload_for_codec_contention`
  predicate + `PreloadSlide` IPC arm
- `renderer/src/video_decode.rs` — `preload_handoff` probe (all 5
  emit sites carry `was_deferred=false`)
- `qa/r97-...` — investigation transcripts
