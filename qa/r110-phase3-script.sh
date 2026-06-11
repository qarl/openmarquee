#!/bin/bash
# r110-phase3-script.sh — paste-and-go Phase 3 glass-verify of
# c3.3.2 (commit a688966, binary md5 f700dd05c5f6e275eccb15d41889d77b)
# on openMarqueeDev at the chosen split from Phase 2 matrix.
#
# Pre-conditions (handed in from Phase 2):
#   - Bench booted on the WINNING split (≥50 MB CMA headroom on
#     full-mix re-measure).
#   - c3.3.2 renderer binary at /usr/local/bin/openmarquee-render
#     (md5 f700dd05c5f6e275eccb15d41889d77b).
#   - 1080p test content at /var/openmarquee/content/97ff88c1-9bfd-4c42-b004-cacff7e983e9
#     (asset.mp4 1920x1080 H.264 main + poster.png + asset.png).
#   - 720p videos at known ids (b343f16b et al) + posters.
#   - FYS-mix replicated as Phase 2 step b: a TextOverVideo slide
#     pointing at the 1080p video id (background_video_slide_id =
#     97ff88c1).
#
# Outputs:
#   - /tmp/phase3-c3_3_2-glass-verify-<timestamp>.log — full log
#   - /tmp/phase3-glass-A.png + /tmp/phase3-glass-B.png — two
#     kmsgrab captures 3 s apart for the motion test
#
# Pass criteria (ALL must hold):
#   1. RustRenderer never SIGTERMs / no MockRenderer fallback in
#      the 10-min soak window — IPC stays responsive
#   2. c3_3_2_recreate_spawn fires for 1080p transitions, NOT for
#      720p transitions
#   3. c3_3_2_wedged_decoder_dropped drop_us << 5 s (was 20 s on
#      starved-heap FYS; expect sub-second on the winning split)
#   4. c3_3_2_recreate_worker_done work_us << 5 s (was 21 s on
#      FYS; expect 0.5-2 s healthy)
#   5. c3_3_2_recreate_installed fires after each spawn
#   6. ZERO REQBUFS-EINVAL events in journal
#   7. ZERO vchiq ETIME events in journal
#   8. ZERO RespawnedError events in journal
#   9. Motion test: two kmsgrab captures 3 s apart, both during
#      the SAME 1080p slide's hold (post-transition), DIFFER
#      pixel-wise. Numeric metric: mean abs delta ≥ 5 of 255 on
#      any color channel = real motion (live video playing).
#      mean abs delta < 1 = frozen poster only (FAIL).
#  10. fps_30s ≥ 18 (matches your "fps sane" criterion from the
#      original c3.3.2 dispatch).
#
# Fail signals → which sub-mechanism to debug:
#   - SIGTERM / MockRenderer fallback → cache.load short-circuit
#     guard at SlideCache::load:828 area didn't fire OR
#     try_drain_finished_recreates didn't run fast enough → check
#     ipc_main.rs:828 + 1356
#   - REQBUFS-EINVAL → BLOCKER-2 (sync drop before spawn at
#     ipc_main.rs:~3217-3221) didn't happen / didn't free kernel
#     codec slot
#   - Motion test fail (poster only, no live video) →
#     try_drain_finished_recreates not installing OR PaintSlide
#     gate (ipc_main.rs:~2421+2557) holding indefinitely → check
#     install probe (c3_3_2_recreate_installed)
#   - c3_3_2_recreate_spawn fires for 720p → c3.3.1 dimension
#     gate at hdmi.rs poster fast-path bypassed (BLOCKER-1 from
#     c3.3.1) → check the poster_w/h gate in bake_a/bake_b
#
# Usage: bash r110-phase3-script.sh
set -u
TS=$(date +%Y%m%d-%H%M%S)
LOG=/tmp/phase3-c3_3_2-glass-verify-${TS}.log
SSH_OPTS="-o ConnectTimeout=10"
BENCH=openmarquee@openMarqueeDev

echo "=== r110 Phase 3 — c3.3.2 glass verify @ ${TS} ===" | tee "$LOG"

# Pre-flight: bench reachable, binary md5, no MockRenderer state
echo "--- pre-flight ---" | tee -a "$LOG"
ssh $SSH_OPTS $BENCH '
  echo "uptime: $(uptime)"
  echo "binary md5: $(md5sum /usr/local/bin/openmarquee-render | awk "{print \$1}")"
  echo "gpu_mem: $(vcgencmd get_mem gpu)"
  echo "cma_total: $(grep CmaTotal /proc/meminfo)"
  echo "cmdline: $(cat /proc/cmdline)"
  systemctl is-active openmarquee-backend
  echo "current backend pid: $(pgrep -fa uvicorn | head -1)"
' 2>&1 | tee -a "$LOG"

# Confirm binary is c3.3.2 (md5 f700dd05c5f6e275eccb15d41889d77b)
md5_actual=$(ssh $SSH_OPTS $BENCH "md5sum /usr/local/bin/openmarquee-render" 2>/dev/null | awk '{print $1}')
if [ "$md5_actual" != "f700dd05c5f6e275eccb15d41889d77b" ]; then
    echo "FAIL: binary md5 mismatch (got $md5_actual; want f700dd05c5f6e275eccb15d41889d77b)" | tee -a "$LOG"
    echo "      redeploy via: scp /tmp/openmarquee-main/renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render $BENCH:/tmp/" | tee -a "$LOG"
    echo "      then ssh $BENCH 'sudo cp /tmp/openmarquee-render /usr/local/bin/ && sudo systemctl restart openmarquee-backend'" | tee -a "$LOG"
    exit 1
fi
echo "binary md5 OK (c3.3.2 a688966)" | tee -a "$LOG"

# Phase 3a: set the FYS-mix-shaped playlist (TextOverVideo
# pointing at 1080p bg + 2 image slides + cycle)
echo "--- Phase 3a: configure FYS-mix-shaped playlist ---" | tee -a "$LOG"
ssh $SSH_OPTS $BENCH 'python3 << PY
import json, os, sys
PL = "/var/openmarquee/playlist.json"
CD = "/var/openmarquee/content"
data = json.load(open(PL))
target = next(p for p in data["playlists"] if p["id"].startswith("00000000"))
# Find a text_slide id + image ids
texts = []
images = []
for d in sorted(os.listdir(CD)):
    j = os.path.join(CD, d, "item.json")
    if not os.path.exists(j): continue
    raw = json.load(open(j))
    it = raw.get("item", raw)
    if it.get("type") == "text_slide": texts.append(d)
    if it.get("type") == "image":      images.append(d)
TEXT_ID = texts[0] if texts else None
IMG_IDS = images[:2]
BG_VIDEO_ID = "97ff88c1-9bfd-4c42-b004-cacff7e983e9"
print(f"text_slide id: {TEXT_ID}")
print(f"image ids: {IMG_IDS}")
# Mutate the text slide to add background_video_slide_id
if TEXT_ID:
    j = os.path.join(CD, TEXT_ID, "item.json")
    raw = json.load(open(j))
    raw["item"]["background_video_slide_id"] = BG_VIDEO_ID
    with open(j + ".tmp", "w") as f: json.dump(raw, f, indent=2)
    os.replace(j + ".tmp", j)
    print(f"set background_video_slide_id on {TEXT_ID}")
# Build a playlist mimicking FYS shape:
# img → 1080p_video → text_over_video → img → 1080p_video → text_over_video → ...
items = []
seq = []
if IMG_IDS:    seq.append(IMG_IDS[0])
seq.append(BG_VIDEO_ID)
if TEXT_ID:    seq.append(TEXT_ID)
if len(IMG_IDS) > 1: seq.append(IMG_IDS[1])
seq.append(BG_VIDEO_ID)
for _ in range(4):  # 4 cycles ~ 5+ minutes
    for sid in seq:
        items.append({"item_id": sid, "transition": "iris", "transition_ms": 800, "duration_ms": 8000})
target["items"] = items
target["name"] = "bench-fysmix-c33verify"
with open(PL + ".tmp", "w") as f: json.dump(data, f, indent=2)
os.replace(PL + ".tmp", PL)
print(f"playlist set: {len(items)} items, sequence={seq[:5]}")
PY
sudo systemctl restart openmarquee-backend
echo "restarted; waiting 75 s for prewarm + open"
sleep 75
systemctl is-active openmarquee-backend
' 2>&1 | tee -a "$LOG"

# Phase 3b: 10-min soak with full instrumentation
echo "--- Phase 3b: 10-min soak ---" | tee -a "$LOG"
SOAK_START=$(date +%s)
ssh $SSH_OPTS $BENCH "
echo \"soak_start_unix=\$(date +%s)\"
echo \"soak_start_iso=\$(date -Iseconds)\"
END=\$((\$(date +%s) + 600))
while [ \$(date +%s) -lt \$END ]; do
    cma_free=\$(awk '/CmaFree/{print \$2}' /proc/meminfo)
    reloc=\$(vcgencmd get_mem reloc 2>/dev/null | sed -E 's/[^0-9]+//g')
    arm=\$(vcgencmd get_mem arm 2>/dev/null | sed -E 's/[^0-9]+//g')
    echo \"t=\$(date +%s) cma_free_kb=\${cma_free} reloc_M=\${reloc} arm_M=\${arm}\"
    sleep 10
done
" 2>&1 | tee -a "$LOG"

# Phase 3c: pull journal + extract c3.3.2 probes + errors
echo "--- Phase 3c: journal extraction ---" | tee -a "$LOG"
ssh $SSH_OPTS $BENCH "
echo '=== c3_3_2 probes (last 12 min) ==='
sudo journalctl -u openmarquee-backend --since '12 min ago' --no-pager 2>&1 | \
    grep -E 'c3_3_2_|poster_a_sourced|poster_b_sourced|c3_3_1_decoder_recreate' | head -40
echo '=== errors (last 12 min) ==='
sudo journalctl -u openmarquee-backend --since '12 min ago' --no-pager 2>&1 | \
    grep -iE 'RustRendererTimeout|MockRenderer|REQBUFS.*EINVAL|vchiq.*ETIME|ril\.video|recreate_failed|recreate_panicked|RespawnedError|capture_drained_early' | head -20
echo '=== ipc.soak final 5 ==='
sudo journalctl -u openmarquee-backend --since '12 min ago' --no-pager 2>&1 | \
    grep -E 'ipc\.soak' | tail -5
" 2>&1 | tee -a "$LOG"

# Phase 3d: motion test (kmsgrab pixel-diff)
echo "--- Phase 3d: motion test via kmsgrab ---" | tee -a "$LOG"
# Wait for the playlist to enter a 1080p video slot (the BG video id),
# then capture two frames 3 s apart, scp them to Mac, compare.
ssh $SSH_OPTS $BENCH "
# Find when the bg_video is currently playing — give it 60 s to land
for _ in \$(seq 1 30); do
    in_video=\$(sudo journalctl -u openmarquee-backend --since '5 sec ago' --no-pager 2>&1 | grep -c '97ff88c1' || true)
    if [ \"\$in_video\" -gt 0 ]; then break; fi
    sleep 2
done
# Capture A
sudo ffmpeg -loglevel error -f kmsgrab -i - -vframes 1 -vf 'hwdownload,format=bgr0' /tmp/phase3-glass-A.png -y
sleep 3
# Capture B
sudo ffmpeg -loglevel error -f kmsgrab -i - -vframes 1 -vf 'hwdownload,format=bgr0' /tmp/phase3-glass-B.png -y
ls -la /tmp/phase3-glass-A.png /tmp/phase3-glass-B.png 2>&1
" 2>&1 | tee -a "$LOG"

# scp captures to Mac for pixel diff
scp $BENCH:/tmp/phase3-glass-A.png /tmp/phase3-glass-A.png 2>&1 | tee -a "$LOG"
scp $BENCH:/tmp/phase3-glass-B.png /tmp/phase3-glass-B.png 2>&1 | tee -a "$LOG"
if [ -f /tmp/phase3-glass-A.png ] && [ -f /tmp/phase3-glass-B.png ]; then
    python3 << PY 2>&1 | tee -a "$LOG"
from PIL import Image
import numpy as np
a = np.asarray(Image.open("/tmp/phase3-glass-A.png").convert("RGB"))
b = np.asarray(Image.open("/tmp/phase3-glass-B.png").convert("RGB"))
if a.shape != b.shape:
    print(f"MOTION FAIL: shape mismatch {a.shape} vs {b.shape}")
else:
    delta = np.abs(a.astype(int) - b.astype(int))
    mean_delta = delta.mean(axis=(0, 1))
    max_delta = delta.max(axis=(0, 1))
    print(f"mean abs delta RGB: {mean_delta.tolist()}")
    print(f"max  abs delta RGB: {max_delta.tolist()}")
    if mean_delta.max() >= 5:
        print(f"MOTION TEST PASS — mean delta ≥5 on at least one channel; live video playing")
    elif mean_delta.max() < 1:
        print(f"MOTION TEST FAIL — mean delta <1; frozen poster (handoff didn't happen)")
    else:
        print(f"MOTION TEST INDETERMINATE — mean delta in [1, 5); review captures by eye")
PY
else
    echo "MOTION TEST CAPTURE FAILED — captures not pulled to Mac" | tee -a "$LOG"
fi

# Phase 3e: pass/fail summary
echo "--- Phase 3e: pass/fail summary ---" | tee -a "$LOG"
echo "review the log above against the criteria at the top of this script:" | tee -a "$LOG"
echo "  1. RustRenderer responsive (no MockRenderer fallback)" | tee -a "$LOG"
echo "  2. c3_3_2_recreate_spawn fires on 1080p only" | tee -a "$LOG"
echo "  3. drop_us << 5s" | tee -a "$LOG"
echo "  4. work_us << 5s" | tee -a "$LOG"
echo "  5. c3_3_2_recreate_installed follows each spawn" | tee -a "$LOG"
echo "  6. zero REQBUFS-EINVAL" | tee -a "$LOG"
echo "  7. zero vchiq ETIME" | tee -a "$LOG"
echo "  8. zero RespawnedError" | tee -a "$LOG"
echo "  9. motion test mean delta ≥5" | tee -a "$LOG"
echo " 10. fps_30s ≥18" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "log: $LOG" | tee -a "$LOG"
echo "done"
