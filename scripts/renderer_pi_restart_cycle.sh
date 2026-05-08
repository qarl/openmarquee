#!/usr/bin/env bash
# v1-spec §8.6 process-restart lifecycle gate. Spec: closing the
# renderer releases all GPU/kernel resources so a subsequent open
# succeeds; a systemd restart (or any other re-instantiation in
# the same process) must not leak buffers, fds, or kernel objects
# across the gap.
#
# Implementation: drives N renderer cycles via --ipc-sidecar, each:
#   1. Open
#   2. BeginSlide
#   3. Advance (a few ticks)
#   4. Capture
#   5. Close
#   6. process exits cleanly
#
# Captures system-wide cma_used between cycles (process-local
# vm_rss isn't measurable across forks; cma_used catches kernel-
# side leaks since CMA buffers DON'T release across process
# boundaries unless explicitly destroyed by drmModeRmFB +
# gbm_bo_destroy). cma_used drift across cycles = renderer
# leaking kernel objects on close.
#
# Asserts: cma_used at cycle N+1 must not exceed cycle 0 by more
# than RESTART_MAX_CMA_DELTA_MB (default 15 MB; absorbs kernel
# slab variance + page allocator state).
#
# Usage:
#   scripts/renderer_pi_restart_cycle.sh [TARGET]
#
# Env:
#   RESTART_CYCLES (default 20): number of cycles
#   RESTART_MAX_CMA_DELTA_MB (default 15): per-cycle CMA drift cap

set -euo pipefail

TARGET="${1:-openmarquee@openMarqueeDev}"
CYCLES="${RESTART_CYCLES:-20}"
MAX_DELTA_MB="${RESTART_MAX_CMA_DELTA_MB:-15}"
BIN_HOST="renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
BIN_PI="/tmp/openmarquee-render"
LOG_DIR="/tmp/renderer-restart"
LOG="$LOG_DIR/cycles.log"

if [ ! -x "$BIN_HOST" ]; then
    echo "FAIL: missing host binary at $BIN_HOST"
    exit 1
fi

ssh "$TARGET" "mkdir -p $LOG_DIR; rm -f $LOG"

restore_backend() {
    ssh "$TARGET" "sudo systemctl start openmarquee-backend" >/dev/null 2>&1 || true
}
trap restore_backend EXIT

echo "==> deploying binary to $TARGET:$BIN_PI"
scp -q "$BIN_HOST" "$TARGET:$BIN_PI"

echo "==> stopping openmarquee-backend (DRM master grab)"
ssh "$TARGET" "sudo systemctl stop openmarquee-backend"
sleep 2

# Find a text_slide UUID for BeginSlide.
SLIDE_UUID=$(ssh "$TARGET" "python3 -c '
import json, pathlib
for d in sorted(pathlib.Path(\"/var/openmarquee/content\").glob(\"*/\")):
    ip = d / \"item.json\"
    if not ip.exists(): continue
    e = json.loads(ip.read_text())
    if e.get(\"item\", {}).get(\"type\") != \"text_slide\": continue
    print(d.name); break
'")
echo "    slide_uuid=$SLIDE_UUID"

# Bake the IPC script once.
SCRIPT_TMP=$(mktemp -t openmarquee-restart-script)
cat > "$SCRIPT_TMP" <<EOF
{"op":"open","params":{"output":"hdmi","content_root":"/var/openmarquee/content"}}
{"op":"begin_slide","params":{"slide_id":"$SLIDE_UUID","t0_ms":0,"duration_ms":2000}}
{"op":"advance","params":{"t_ms":100}}
{"op":"advance","params":{"t_ms":500}}
{"op":"advance","params":{"t_ms":1000}}
{"op":"capture","params":{"path":"/tmp/openmarquee-restart-cap.png"}}
{"op":"close"}
EOF
scp -q "$SCRIPT_TMP" "$TARGET:/tmp/openmarquee-restart-script.json"
rm -f "$SCRIPT_TMP"

# read_cma reads CmaUsed via /proc/meminfo on the Pi.
read_cma() {
    ssh "$TARGET" "awk '/CmaTotal/ {t=\$2} /CmaFree/ {f=\$2} END {print (t-f)/1024}' /proc/meminfo"
}

# Baseline (before any renderer cycle).
sleep 1
BASELINE_CMA=$(read_cma)
echo "==> baseline cma_used: ${BASELINE_CMA} MB"
echo "cycle,phase,cma_mb" > /tmp/restart-cycles.csv
echo "0,baseline,$BASELINE_CMA" >> /tmp/restart-cycles.csv

echo "==> running $CYCLES renderer cycles"
for i in $(seq 1 "$CYCLES"); do
    ssh "$TARGET" "$BIN_PI --ipc-sidecar < /tmp/openmarquee-restart-script.json >> $LOG 2>&1" || \
        { echo "FAIL: cycle $i exit non-zero"; tail -20 "$LOG"; exit 1; }
    sleep 1
    CYCLE_CMA=$(read_cma)
    DELTA_MB=$(python3 -c "print(f'{$CYCLE_CMA - $BASELINE_CMA:.1f}')")
    echo "    cycle $i: cma=${CYCLE_CMA} MB (Δ=${DELTA_MB} MB from baseline)"
    echo "$i,post_close,$CYCLE_CMA" >> /tmp/restart-cycles.csv
done

# Final cma + assertion.
FINAL_CMA=$(read_cma)
DELTA_MB=$(python3 -c "print(f'{$FINAL_CMA - $BASELINE_CMA:.1f}')")
echo
echo "==> final: baseline=${BASELINE_CMA} MB; final=${FINAL_CMA} MB; Δ=${DELTA_MB} MB across $CYCLES cycles"

OK=$(python3 -c "
delta = $FINAL_CMA - $BASELINE_CMA
print('ok' if delta <= $MAX_DELTA_MB else f'fail (delta {delta:.1f} MB > {$MAX_DELTA_MB} MB ceiling)')
")

if [ "$OK" != "ok" ]; then
    echo "FAIL: §8.6 process-restart leak ($OK)"
    cat /tmp/restart-cycles.csv
    exit 1
fi

echo
echo "PASS: §8.6 process-restart green ($CYCLES cycles, Δcma=${DELTA_MB} MB ≤ ${MAX_DELTA_MB} MB)"
echo "  log: $LOG"
echo "  csv: /tmp/restart-cycles.csv"
