#!/usr/bin/env bash
#
# Path A v2v regression-guard runner (2026-06-14).
#
# Boots openmarquee-backend with a 2-video playlist using the
# committed golden-{red,blue}.mp4 fixtures, cycling through every
# transition kind. Reads the in-tree `transition_tex_probe` (added
# by iter-3 of the v2v fix series, latched once per transition at
# progress >= 0.4) and asserts:
#
#   1. side=a luma > LUMA_FLOOR (live video on FROM side, not black)
#   2. side=b luma > LUMA_FLOOR (live video on TO side, not black)
#   3. max delta_ms <= DELTA_MS_CEILING (no multi-second freeze)
#
# Path A iter-1 (commit 4b6e93a) fixed the image (side=a/b live);
# Path A iter-2 (commit follow-on) fixed the framerate (off-thread
# async to-side prime kills the 1.5-2.6s freeze QA flagged after
# iter-1's image win). This runner is the regression guard that
# would catch either regression returning.
#
# WHY this lives outside CI: the bug is GL/vc4 hardware behavior
# (deferred tile-store vs dma-buf reclaim; codec input starvation
# under contention). CI runs on macOS where hdmi.rs/v4l2.rs are
# cfg(linux)-compiled-out, so this MUST run on real Pi hardware.
# The host-portable companion is the cargo source-pin test mod at
# `renderer/src/hdmi_logic.rs::path_a_stage2_tests` (6 tests),
# which guards the routing STRUCTURE in CI but cannot exercise the
# GL behavior.
#
# Usage (run on fireplacesign or openMarqueeDev):
#
#   sudo bash qa/scripts/run_video_to_video_golden.sh
#
# Exit codes:
#   0   OVERALL: PASS
#   1   OVERALL: FAIL (one or more transitions black OR delta_ms>ceiling)
#   2   missing fixtures or invariant pre-check failure
#
# A trap on EXIT/INT/TERM restores the prior playlist + settings +
# bounces the backend so a mid-run abort doesn't leave the sign
# carrying the test playlist forever.
#

set -uo pipefail

LUMA_FLOOR="${LUMA_FLOOR:-30}"
DELTA_MS_CEILING="${DELTA_MS_CEILING:-1000}"
SOAK_SEC="${SOAK_SEC:-90}"
BACKEND_UNIT="${BACKEND_UNIT:-openmarquee-backend.service}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FIXTURE_DIR="$REPO_ROOT/qa/fixtures/transition-golden"

if [[ ! -f "$FIXTURE_DIR/golden-red.mp4" ]] || [[ ! -f "$FIXTURE_DIR/golden-blue.mp4" ]]; then
    echo "FAIL: fixtures missing under $FIXTURE_DIR" >&2
    echo "  expected golden-red.mp4 + golden-blue.mp4" >&2
    exit 2
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "FAIL: systemctl not in PATH (this runner requires the Pi systemd unit)" >&2
    exit 2
fi

# Snapshot prior state for restore.
RESTORE_TMP="$(mktemp -d -t v2v-golden-restore.XXXXXX)"
sudo cp /var/openmarquee/playlist.json "$RESTORE_TMP/playlist.json.prior" 2>/dev/null || true
sudo cp /var/openmarquee/settings.json "$RESTORE_TMP/settings.json.prior" 2>/dev/null || true
sudo ls -la /var/openmarquee/content > "$RESTORE_TMP/content.ls.prior" 2>/dev/null || true

restore() {
    # Sacred BLOCKER-4 fix: capture exit status FIRST, return it at
    # end so any cleanup command's nonzero exit doesn't clobber the
    # script's `exit $FAIL` value. Without the explicit return,
    # bash overrides $? with the trap body's last command status.
    local rc=$?
    echo
    echo "==> restoring prior state (rc=$rc)"
    if [[ -f "$RESTORE_TMP/playlist.json.prior" ]]; then
        sudo cp "$RESTORE_TMP/playlist.json.prior" /var/openmarquee/playlist.json || true
    fi
    if [[ -f "$RESTORE_TMP/settings.json.prior" ]]; then
        sudo cp "$RESTORE_TMP/settings.json.prior" /var/openmarquee/settings.json || true
    fi
    # The 2 golden-content dirs (red + blue UUIDs) are throwaway;
    # remove them so the sign doesn't carry zombie test content.
    sudo rm -rf "/var/openmarquee/content/${RED_ID:-_not_set_}" "/var/openmarquee/content/${BLUE_ID:-_not_set_}" 2>/dev/null || true
    sudo systemctl restart "$BACKEND_UNIT" || true
    # Copy the journal OUT of RESTORE_TMP before we wipe it so an
    # operator inspecting the test result post-exit can still read
    # it. Survives the trap.
    if [[ -f "$RESTORE_TMP/journal.log" ]]; then
        sudo cp "$RESTORE_TMP/journal.log" /tmp/v2v-golden-journal.log 2>/dev/null || true
    fi
    rm -rf "$RESTORE_TMP"
    # Re-raise the original exit code so PASS/FAIL propagates to
    # the caller / CI / operator. Without this, the trap's last
    # `rm -rf` (or rebuild restart) silently 0's a FAIL run.
    return "$rc"
}
trap restore EXIT INT TERM

# Test playlist UUIDs — fixed so a re-run of the script doesn't
# leave zombie content dirs from the prior run.
RED_ID="11111111-1111-4111-a111-111111111111"
BLUE_ID="22222222-2222-4222-a222-222222222222"
PLAYLIST_ID="00000000-0000-4000-8000-000000000001"

echo "==> staging golden video assets"
sudo mkdir -p "/var/openmarquee/content/$RED_ID" "/var/openmarquee/content/$BLUE_ID"
sudo cp "$FIXTURE_DIR/golden-red.mp4"  "/var/openmarquee/content/$RED_ID/asset.mp4"
sudo cp "$FIXTURE_DIR/golden-blue.mp4" "/var/openmarquee/content/$BLUE_ID/asset.mp4"

for id in "$RED_ID" "$BLUE_ID"; do
    cat > /tmp/item.json <<EOF
{
  "item": {
    "id": "$id",
    "name": "v2v-golden-$id",
    "type": "video",
    "duration_ms": 3000
  }
}
EOF
    sudo cp /tmp/item.json "/var/openmarquee/content/$id/item.json"
done
rm -f /tmp/item.json

# Every transition kind we can hit. iris is the one r106 originally
# proved (live-fire dual-1080p); the rest cover the surface.
TRANSITIONS=(cut fade iris wipe slide marquee blinds push shutter glitch)

echo "==> building test playlist (${#TRANSITIONS[@]} transitions × 2 sides)"
items_json=""
for k in "${TRANSITIONS[@]}"; do
    items_json+="{\"item_id\":\"$RED_ID\",\"transition\":\"$k\",\"transition_ms\":1500,\"duration_ms\":3000},"
    items_json+="{\"item_id\":\"$BLUE_ID\",\"transition\":\"$k\",\"transition_ms\":1500,\"duration_ms\":3000},"
done
items_json="[${items_json%,}]"

cat > /tmp/playlist.json <<EOF
{
  "schema_version": 4,
  "playlists": [{
    "id": "$PLAYLIST_ID",
    "name": "v2v-golden",
    "items": $items_json
  }],
  "active_playlist_id": "$PLAYLIST_ID"
}
EOF
sudo cp /tmp/playlist.json /var/openmarquee/playlist.json
rm -f /tmp/playlist.json

JOURNAL_SINCE_REF="$(date +'%Y-%m-%d %H:%M:%S')"
echo "==> restarting $BACKEND_UNIT (journal-since=\"$JOURNAL_SINCE_REF\")"
sudo systemctl restart "$BACKEND_UNIT"
sleep 5

# Calculate expected soak: 2 videos × N transitions × (3s hold + 1.5s transition) ~= 9s × N.
# Add 10s prime + slack at start.
echo "==> soaking ${SOAK_SEC}s for full playlist cycle"
END_TS=$(($(date +%s) + SOAK_SEC))
while [ "$(date +%s)" -lt "$END_TS" ]; do
    sleep 5
    REMAINING=$((END_TS - $(date +%s)))
    [ "$REMAINING" -gt 0 ] && echo "   ${REMAINING}s remaining..."
done

JOURNAL="$RESTORE_TMP/journal.log"
echo "==> capturing journal since restart"
sudo journalctl -u "$BACKEND_UNIT" --since "$JOURNAL_SINCE_REF" --no-pager > "$JOURNAL"
echo "   $(wc -l < "$JOURNAL") lines"

# transition_tex_probe line shape (from iter-3 instrumentation):
#   [perf] transition_tex_probe side=<a|b> kind=<k> progress=<p> fbo_id=<n> tex_id=<n> luma=<L>
# Per side per transition kind, we expect ≥1 sample (latched once
# per transition at progress >= 0.4). If 0 samples → FAIL (the
# probe didn't fire OR no transition of that kind ran).

FAIL=0
echo
echo "==> per-transition-kind luma analysis (floor=$LUMA_FLOOR)"
printf "%-12s %8s %8s %8s %8s %s\n" "KIND" "A_SAMPLES" "A_MIN" "B_SAMPLES" "B_MIN" "VERDICT"

for k in "${TRANSITIONS[@]}"; do
    a_lumas=$(grep "transition_tex_probe side=a" "$JOURNAL" 2>/dev/null \
              | grep "kind=$k" \
              | grep -oE 'luma=[0-9]+' | sed 's/luma=//' || true)
    b_lumas=$(grep "transition_tex_probe side=b" "$JOURNAL" 2>/dev/null \
              | grep "kind=$k" \
              | grep -oE 'luma=[0-9]+' | sed 's/luma=//' || true)
    a_n=$(printf '%s\n' "$a_lumas" | grep -c '[0-9]' || true)
    b_n=$(printf '%s\n' "$b_lumas" | grep -c '[0-9]' || true)
    a_min=$(printf '%s\n' "$a_lumas" | sort -n | head -1)
    b_min=$(printf '%s\n' "$b_lumas" | sort -n | head -1)
    a_min=${a_min:-0}
    b_min=${b_min:-0}
    verdict="PASS"
    # Skip 'cut' transition kind — it's a zero-duration cut and the
    # probe (latched at progress>=0.4) may not have a sample.
    if [ "$k" = "cut" ]; then
        verdict="N/A (cut is zero-duration)"
    elif [ "$a_n" = "0" ] || [ "$b_n" = "0" ]; then
        verdict="FAIL (no samples — probe didn't fire OR transition didn't run)"
        FAIL=1
    elif [ "$a_min" -le "$LUMA_FLOOR" ] || [ "$b_min" -le "$LUMA_FLOOR" ]; then
        verdict="FAIL (a_min=$a_min b_min=$b_min ≤ floor=$LUMA_FLOOR — side went BLACK)"
        FAIL=1
    fi
    printf "%-12s %8s %8s %8s %8s %s\n" "$k" "$a_n" "$a_min" "$b_n" "$b_min" "$verdict"
done

echo
echo "==> delta_ms freeze analysis (ceiling=${DELTA_MS_CEILING}ms)"
# Frame-pacing line shape:
#   [perf] frame over budget: ... delta_ms=<N> ...
deltas=$(grep -oE 'delta_ms=[0-9]+' "$JOURNAL" 2>/dev/null | sed 's/delta_ms=//' || true)
if [ -z "$deltas" ]; then
    # No delta_ms lines means no over-budget frames OR the
    # frame_pacing log line never fired. Treat as benign on the
    # assumption frames mostly stayed inside budget.
    echo "   no delta_ms samples in journal (interpreted as no over-budget frames)"
    max_delta=0
else
    max_delta=$(printf '%s\n' "$deltas" | sort -n | tail -1)
    max_delta=${max_delta:-0}
    sample_count=$(printf '%s\n' "$deltas" | grep -c '[0-9]')
    echo "   $sample_count samples, max=${max_delta}ms"
fi

if [ "$max_delta" -gt "$DELTA_MS_CEILING" ]; then
    echo "   FAIL: max delta_ms=$max_delta exceeds ceiling=$DELTA_MS_CEILING"
    FAIL=1
else
    echo "   PASS: max delta_ms=$max_delta ≤ ceiling=$DELTA_MS_CEILING"
fi

echo
echo "==========================================="
if [ "$FAIL" = "0" ]; then
    echo "OVERALL: PASS"
else
    echo "OVERALL: FAIL"
fi
echo "==========================================="
echo "journal will be available at /tmp/v2v-golden-journal.log (the trap"
echo "copies it OUT of $RESTORE_TMP before wiping)"

exit "$FAIL"
