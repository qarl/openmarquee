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
#   0   OVERALL: PASS (every represented kind side=a/b luma>floor + delta_ms within ceiling)
#   1   OVERALL: FAIL (one or more transitions black OR delta_ms>ceiling)
#   2   INFRA error (missing fixtures, systemctl unavailable, OR the
#       backend rejected the staged playlist — these are test-staging
#       failures, NOT transition regressions)
#   3   PASS with SUSPICIOUS (every kind that ran was clean, but at
#       least one expected kind produced zero probe samples — possible
#       benign config drift OR a stutter-regression silenced the probe.
#       Human reviews the per-kind N/A rows before accepting.)
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

NOW_ISO="$(date -u +'%Y-%m-%dT%H:%M:%S.000000+00:00')"
# 2026-06-14 v2 fix per QA: item.json MUST carry the full envelope the
# backend's storage/playback loader expects, not a bare `{"item": ...}`.
# The envelope shape is `{schema_version: 3, updated_at: <iso>, item: {...VideoSlide...}}`
# per backend/openmarquee/content/storage.py. Without
# `schema_version`, fetch_items() throws ValueError + the playback loop
# never starts any slides → zero transitions → the runner falsely
# reports "no samples" as a transition regression.
for id in "$RED_ID" "$BLUE_ID"; do
    cat > /tmp/item.json <<EOF
{
  "schema_version": 3,
  "updated_at": "$NOW_ISO",
  "item": {
    "type": "video",
    "id": "$id",
    "name": "v2v-golden-$id",
    "duration_ms": 3000,
    "transition": "cut",
    "transition_ms": 500,
    "created_at": "$NOW_ISO"
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

# 2026-06-14 v2 fix per QA: distinguish "test infrastructure broken"
# from "real regression." Pre-v2 the runner would falsely report
# every kind as FAIL when the playlist staging was rejected (zero
# transitions ran → zero probe samples → "no samples = FAIL"). A
# broken test masquerading as a broken fix is dangerous — operator
# reverts the fix on a phantom regression.
#
# Two pre-flight checks before per-kind analysis:
#   1. Look for backend startup errors that indicate the test
#      playlist itself failed to load (schema mismatch, JSON parse
#      error, etc.). If present → exit 2 INFRA.
#   2. Count TOTAL transition_tex_probe lines in the journal. If
#      0 across all kinds → no transitions ran → exit 2 INFRA.
# Only after both pass does the per-kind black-side / delta-ms
# analysis fire (which determines OVERALL PASS/FAIL).
echo
echo "==> pre-flight: confirming test playlist was accepted by backend"
# Sacred-review #1 fix: scope the INFRA grep to lines that reference
# OUR test slide UUIDs ($RED_ID/$BLUE_ID). The substring 'migration
# needed' appears verbatim in _storage_recovery.quarantine_corrupt_
# file's warning log when ANY item.json on disk has the wrong
# schema_version (e.g. a quarantined sibling from a PRIOR test run,
# OR an unrelated corrupt envelope). Without the UUID filter we'd
# trip INFRA on unrelated quarantines and mask the real per-kind
# signal.
INFRA_RE="playlist prune failed|fetch_items failed|envelope corrupted"
INFRA_LINES=$(grep -E "$INFRA_RE" "$JOURNAL" 2>/dev/null \
              | grep -E "$RED_ID|$BLUE_ID" || true)
# 'migration needed' is treated specially because that's the exact
# error the v1 runner produced; scope it tightly to OUR UUIDs.
MIGRATION_LINES=$(grep "migration needed" "$JOURNAL" 2>/dev/null \
                  | grep -E "$RED_ID|$BLUE_ID" || true)
INFRA_TOTAL=0
[ -n "$INFRA_LINES" ] && INFRA_TOTAL=$((INFRA_TOTAL + $(printf '%s\n' "$INFRA_LINES" | wc -l)))
[ -n "$MIGRATION_LINES" ] && INFRA_TOTAL=$((INFRA_TOTAL + $(printf '%s\n' "$MIGRATION_LINES" | wc -l)))
if [ "$INFRA_TOTAL" -gt 0 ]; then
    echo "   INFRA: backend rejected our test items ($INFRA_TOTAL error lines reference $RED_ID/$BLUE_ID):"
    printf '%s\n' "$INFRA_LINES" "$MIGRATION_LINES" | head -3 | sed 's/^/      /'
    echo
    echo "==========================================="
    echo "INFRA: test-staging error (NOT a transition regression)"
    echo "==========================================="
    echo "Inspect: $JOURNAL (copied to /tmp/v2v-golden-journal.log on exit)"
    exit 2
fi
TOTAL_PROBE_SAMPLES=$(grep -c "transition_tex_probe side=" "$JOURNAL" || true)
echo "   $TOTAL_PROBE_SAMPLES transition_tex_probe samples in window"
if [ "$TOTAL_PROBE_SAMPLES" -eq 0 ]; then
    echo
    echo "==========================================="
    echo "INFRA: no transitions ran (zero probe samples) — check playlist items accepted"
    echo "==========================================="
    echo "Hint: are the staged item.json envelopes the right schema_version?"
    echo "Inspect: $JOURNAL (copied to /tmp/v2v-golden-journal.log on exit)"
    exit 2
fi
echo "   pre-flight OK — proceeding to per-kind analysis"

# transition_tex_probe line shape (from iter-3 instrumentation):
#   [perf] transition_tex_probe side=<a|b> kind=<k> progress=<p> fbo_id=<n> tex_id=<n> luma=<L>
# Per side per transition kind, we expect ≥1 sample (latched once
# per transition at progress >= 0.4). If 0 samples for a SPECIFIC
# kind (with TOTAL_PROBE_SAMPLES > 0) → that kind didn't run
# (unknown transition name in our list, or the kind was dropped
# from the playlist) → flag as N/A, not FAIL.

FAIL=0
SUSPICIOUS=0
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
        # Pre-flight already confirmed TOTAL > 0, so per-kind 0
        # means this specific kind didn't appear in the playlist
        # (e.g. unknown transition name dropped by the loader)
        # OR — and this is the subagent #2 concern — the kind
        # ran but the renderer's probe didn't latch because the
        # framerate stuttered so badly the transition never
        # crossed progress >= 0.4 (the latch threshold). The
        # first cause is benign config drift; the second is a
        # real regression silenced. Bias toward visibility:
        # flag as SUSPICIOUS so a human reviews even if all
        # other kinds PASS. Exit code 3 distinguishes from a
        # clean PASS (0) and a real black-side FAIL (1).
        verdict="N/A (kind missing OR stutter-regression — REVIEW)"
        SUSPICIOUS=1
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
if [ "$FAIL" -gt 0 ]; then
    echo "OVERALL: FAIL"
    OVERALL_EXIT=1
elif [ "$SUSPICIOUS" -gt 0 ]; then
    echo "OVERALL: PASS (with SUSPICIOUS — review N/A rows; possible kind"
    echo "         missing from playlist OR stutter-regression that silenced"
    echo "         the probe by never crossing progress >= 0.4)"
    OVERALL_EXIT=3
else
    echo "OVERALL: PASS"
    OVERALL_EXIT=0
fi
echo "==========================================="
echo "journal will be available at /tmp/v2v-golden-journal.log (the trap"
echo "copies it OUT of $RESTORE_TMP before wiping)"

exit "$OVERALL_EXIT"
