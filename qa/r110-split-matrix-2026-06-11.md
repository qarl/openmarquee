# r110 gpu_mem/cma split matrix — 2026-06-11

QA-dispatched bench campaign on `openMarqueeDev` (Pi Zero 2 W, 512 MB) to
pick a `gpu_mem` / `cma=` boot-config pair that supports 1080p decoder
allocation **and** leaves ≥50 MB measured CMA headroom above peak working
set under FYS-shaped content mix.

Driver: 2nd CMA-resize incident in this codebase (8f64621 cma=256M starved
FYS 720p; r38a-era cma=384M previously bricked it). See
`feedback_cma_working_set_must_be_measured.md`.

## Method (per split)

1. `/tmp/apply-split.sh <gpu_mem_MB> <cma_MB>` strips existing
   `gpu_mem=` lines and appends `[all]/gpu_mem=N` in config.txt;
   awk-field-prefix strips existing `cma=` tokens and appends
   `cma=NM` in cmdline.txt (single-line-invariant guard). Reboots.
2. After reboot, `/tmp/bench-measure.sh <label> 5` samples
   `/proc/meminfo` `CmaFree` + `vcgencmd get_mem reloc` +
   `vcgencmd get_mem arm` every 5 s for 5 min while playlist runs.
   Captures `min_cma_free_kb` (→ peak CMA used) + `min_reloc_M` +
   `min_arm_M`. Greps journal for `RustRendererTimeout` /
   `REQBUFS-EINVAL` / `vchiq ETIME` / `RespawnedError` /
   `c3_3_2_*` / `poster_*_sourced`.
3. Two playlists swapped between runs: 720p loop (single
   1280×704 video × 3 repeats) and 1080p loop (single synthetic
   1920×1080 H.264 main-profile ref=2 1s-IDR × 10 repeats).
4. **Light-loop only for matrix rows.** Per QA "(c) Record both
   numbers per split (light-loop + full-mix for the finalist)" —
   only the finalist re-measures on the FYS-shaped full mix (Phase 2
   continuation). The ≥50 MB headroom rule applies to the full-mix
   number, not the light-loop number.

## Caveats

- Bench HDMI auto-negotiated to **1024×768** (panel native, not
  1920×1080). Decoder still runs at source dims, so the decode-side
  physics + CMA / reloc allocation costs hold. Compositor-side
  numbers (paint_us / GPU bandwidth) have a 1024×768 / 1920×1080
  scaling caveat — not measured here.
- Light-loop content set: 1 unique 720p video (b343f16b 1280×704
  H.264 BT.709), 1 synthetic 1080p H.264 main-profile (97ff88c1,
  ref=2, 1 s IDR interval, 8 Mb/s, 10 s duration, BT.709 limited
  per c3.1.1 recipe). Both have posters generated per c3.1.1
  recipe.

## Matrix

| split (gpu_mem / cma) | non-CMA ARM (MB) | 720p result | 1080p result | min_cma_free 720p (MB) | peak CMA used 720p (MB) | min_cma_free 1080p (MB) | peak CMA used 1080p (MB) | min_reloc (MB) | min_arm (MB) | journal errors | notes |
|--|--|--|--|--|--|--|--|--|--|--|--|
| **stock** (64 / 256) | **192** | ✅ painted | ❌ REJECTED | 60 | 195 | (n/a) | (n/a) | 43 | 448 | RustRenderer exhausted → MockRenderer on 1080p open op; matches QA's vchiq-ETIME / reloc-starved-at-64M hypothesis | 0 c3_3_2 probes on 720p (dim gate gates poster sourcing on 1080p only) |
| **128 / 320** | **64** | ❌ DOES NOT BOOT | ❌ DOES NOT BOOT | — | — | — | — | — | — | bench unreachable from 14:55 reboot; 3 probes at 16:45 all "Operation timed out" port 22 | matches r38a / June cma=384M brick shape — Pi Zero 2 W cannot run with ≤64M non-CMA ARM after gpu_mem + cma carveouts |
| **128 / 288** | **96** | BLOCKED-ON-HANDS | BLOCKED-ON-HANDS | — | — | — | — | — | — | pending bench SD recovery | expected to boot per QA |
| **96 / 288** | **128** | BLOCKED-ON-HANDS | BLOCKED-ON-HANDS | — | — | — | — | — | — | pending bench SD recovery | expected to boot |

## Findings to date

1. **Stock 64M gpu_mem CANNOT support 1080p decode**. Empirically
   pinned on bench, second box confirmed (FYS was first). Upgrades
   from "FYS observation" to **platform fact**: stock gpu_mem=64
   firmware reloc heap is too small for `ril.video_decode` MMAL
   component creation at 1920×1080.
2. **128/320 leaves only 64 MB non-CMA ARM and bricks boot.**
   Confirms QA's pre-test prediction. Same OOM-during-init shape
   as the 2026-06-02 r38a `cma=384M` incident. **Pi Zero 2 W
   minimum non-CMA ARM budget appears to be ~96 MB** —
   confirmable via 128/288 row when it lands.
3. **Reloc heap appears as constant 43 M at stock** (vcgencmd
   `get_mem reloc` returns the total, not free). Free portion at
   idle is presumably close to total. Need a different probe to
   measure peak reloc-used under 1080p workload; current method
   only sees the total.

## Watch items (carry to morning)

- **`vcgencmd get_mem reloc` returns total, not free**. The matrix
  rows that "boot and 1080p decode succeeds" need a measurement of
  reloc free under 1080p sustained — `vcgencmd mem_reloc_stats`
  shows alloc / compaction history but not "free now". May need
  to grep `/proc/vc-mem` or `vcdbg malloc` (root-only, intrusive)
  to get a meaningful reloc free floor. **Action**: try
  `sudo vcdbg malloc reloc` on a successful boot; if it gives a
  free count, swap into bench-measure.sh.
- **HDMI 1024×768 vs 1920×1080**: compositor-side paint_us
  numbers won't transfer 1:1 to FYS's 1920×1080 panel.
  Decode-side numbers DO transfer (decoder works in source dims).
  Phase 3 glass-verify on bench HDMI will be visually correct but
  smaller — kmsgrab capture still valid for pixel-compare against
  c3.2.2 poster source path.
- **FYS-mix full-mix re-measure on finalist** (per QA protocol
  step b): replicate TextOverVideo + 2 image slides + 2 video
  assets minimum. Bench has 27 text_slides + 9 image slides
  available + the 720p videos + 1080p test video. Will need to
  set a `background_video_slide_id` on one of the text_slides to
  exercise the c3.2.2 TextOverVideo path. Pre-written in
  `qa/r110-phase3-script.sh` (see).

## Pending bench-side action

Pi at openMarqueeDev is unbootable (128/320 brick). Physical SD
recovery required:

1. Cold-pull SD card from bench.
2. Mount bootfs (FAT32, native macOS).
3. Strip the trailing `[all]\ngpu_mem=128` block I appended to
   `/boot/firmware/config.txt`. Strip `cma=320M` from the
   single-line `/boot/firmware/cmdline.txt`. (Or skip directly to
   the next candidate: edit gpu_mem=128 → no change OR change to
   96, cma=320M → cma=288M.)
4. Reinsert + power.
5. I detect via ssh probe and continue the matrix.

ETA to matrix completion once bench is back: ~30 min wall-clock
(2 splits × ~15 min each: reboot + 5 min 720p + 5 min 1080p +
journal grep + table row write).
