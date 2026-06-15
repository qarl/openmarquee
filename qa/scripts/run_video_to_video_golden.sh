#!/usr/bin/env bash
#
# Path A v2v regression-guard runner (2026-06-14, v3).
#
# v3 architecture per QA (round 2): MUTATE the live known-good playlist's
# transition fields instead of fabricating synthetic content. Two prior
# rounds of test-staging bugs (schema-version envelope missing, then
# item_ids / item-shape mismatches) proved that synthesizing playlists +
# content is whack-a-mole against the production storage contracts. The
# live playlist's items are already accepted by the backend AND already
# drive video→video transitions (QA-confirmed). The "golden" property
# is the ASSERTION (live video both sides, no freeze), not the content
# itself.
#
# This runner:
#   1. Snapshots /var/openmarquee/playlist.json + settings.json.
#   2. Verifies the live playlist has at least 2 "video-bearing" items
#      (where a video-bearing item is one whose resolved ContentItem is
#      either type="video" OR type="text_slide" with
#      background_video_slide_id set — per QA, the live "video test"
#      Aurora/Balloon/Candle/Champagne slides are text-over-video).
#   3. Mutates only the `transition` and `transition_ms` fields of each
#      item in the chosen playlist, cycling through every transition
#      kind we want to test. Other fields (id, item_ids, content dirs,
#      slide bodies) are untouched.
#   4. Restarts openmarquee-backend.service, soaks SOAK_SEC seconds.
#   5. Reads journal for transition_tex_probe + delta_ms.
#   6. Per-kind asserts side=a / side=b luma > LUMA_FLOOR. Aggregate
#      delta_ms ≤ DELTA_MS_CEILING.
#   7. EXIT/INT/TERM trap restores prior playlist + settings + bounces
#      the unit + copies journal to /tmp/v2v-golden-journal.log.
#
# Exit codes:
#   0  OVERALL: PASS (every represented kind side=a/b luma>floor +
#      delta_ms within ceiling)
#   1  OVERALL: FAIL (one or more transitions black OR delta_ms over
#      ceiling)
#   2  INFRA error: prerequisites missing, OR playlist lacks ≥2
#      video-bearing items, OR backend rejected our mutated playlist,
#      OR no probe samples landed in the window — all test-staging
#      failures, NOT transition regressions.
#   3  PASS with SUSPICIOUS: every kind that ran was clean, but at
#      least one expected kind produced 0 probe samples. Possible
#      benign config drift (kind not represented after the mutation)
#      OR a stutter-regression that silenced the probe. Human reviews
#      the per-kind N/A rows.
#
# Usage (run on a Pi with the openmarquee binary already deployed):
#   sudo bash qa/scripts/run_video_to_video_golden.sh
#
# Env overrides:
#   LUMA_FLOOR=30
#   DELTA_MS_CEILING=1000
#   SOAK_SEC=90
#   BACKEND_UNIT=openmarquee-backend.service

set -uo pipefail

LUMA_FLOOR="${LUMA_FLOOR:-30}"
DELTA_MS_CEILING="${DELTA_MS_CEILING:-1000}"
SOAK_SEC="${SOAK_SEC:-90}"
BACKEND_UNIT="${BACKEND_UNIT:-openmarquee-backend.service}"

PLAYLIST_PATH=/var/openmarquee/playlist.json
SETTINGS_PATH=/var/openmarquee/settings.json
CONTENT_ROOT=/var/openmarquee/content

# Tools required by the playlist mutation.
for tool in jq systemctl journalctl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "FAIL: required tool not in PATH: $tool" >&2
        exit 2
    fi
done

if [[ ! -f "$PLAYLIST_PATH" ]]; then
    echo "FAIL: $PLAYLIST_PATH missing (is the backend installed?)" >&2
    exit 2
fi

RESTORE_TMP="$(mktemp -d -t v2v-golden-restore.XXXXXX)"
sudo cp "$PLAYLIST_PATH" "$RESTORE_TMP/playlist.json.prior"
[[ -f "$SETTINGS_PATH" ]] && sudo cp "$SETTINGS_PATH" "$RESTORE_TMP/settings.json.prior" || true

restore() {
    # Capture exit status FIRST so the trap's cleanup doesn't clobber it.
    local rc=$?
    echo
    echo "==> restoring prior playlist (rc=$rc)"
    if [[ -f "$RESTORE_TMP/playlist.json.prior" ]]; then
        sudo cp "$RESTORE_TMP/playlist.json.prior" "$PLAYLIST_PATH" || true
    fi
    if [[ -f "$RESTORE_TMP/settings.json.prior" ]]; then
        sudo cp "$RESTORE_TMP/settings.json.prior" "$SETTINGS_PATH" || true
    fi
    sudo systemctl restart "$BACKEND_UNIT" >/dev/null 2>&1 || true
    # Copy journal OUT of RESTORE_TMP before we wipe it.
    if [[ -f "$RESTORE_TMP/journal.log" ]]; then
        sudo cp "$RESTORE_TMP/journal.log" /tmp/v2v-golden-journal.log 2>/dev/null || true
    fi
    rm -rf "$RESTORE_TMP"
    # Re-raise the exit code so PASS/FAIL propagates to caller / CI.
    return "$rc"
}
trap restore EXIT INT TERM

echo "==> snapshot taken: $RESTORE_TMP/playlist.json.prior"
PRIOR_PLAYLIST=$(sudo cat "$PLAYLIST_PATH")

# Identify a playlist with ≥2 video-bearing items. A "video-bearing"
# item is one whose resolved item.json is either type="video" OR
# type="text_slide" with a non-null background_video_slide_id. QA's
# round-2 dispatch: the live 4-video test playlist
# (Aurora/Balloon/Candle/Champagne) uses text_slide + bg-video.
is_video_bearing() {
    local item_id="$1"
    local item_json="$CONTENT_ROOT/$item_id/item.json"
    [[ -f "$item_json" ]] || return 1
    local type bg
    type=$(sudo jq -r '.item.type // "unknown"' "$item_json" 2>/dev/null || echo "unknown")
    if [[ "$type" == "video" ]]; then
        return 0
    fi
    if [[ "$type" == "text_slide" ]]; then
        bg=$(sudo jq -r '.item.background_video_slide_id // ""' "$item_json" 2>/dev/null || echo "")
        if [[ -n "$bg" ]] && [[ "$bg" != "null" ]]; then
            return 0
        fi
    fi
    return 1
}

# Find the first playlist (by index) with ≥2 video-bearing items.
echo "==> scanning playlists for one with ≥2 video-bearing items"
TARGET_PLAYLIST_INDEX=-1
TARGET_PLAYLIST_ITEM_COUNT=0
PLAYLIST_COUNT=$(printf '%s' "$PRIOR_PLAYLIST" | jq '.playlists | length')
if [[ -z "$PLAYLIST_COUNT" ]] || [[ "$PLAYLIST_COUNT" -eq 0 ]]; then
    echo "FAIL: $PLAYLIST_PATH has no playlists[]" >&2
    exit 2
fi

for ((pi = 0; pi < PLAYLIST_COUNT; pi++)); do
    PL_NAME=$(printf '%s' "$PRIOR_PLAYLIST" | jq -r ".playlists[$pi].name // \"(unnamed)\"")
    ITEM_IDS=$(printf '%s' "$PRIOR_PLAYLIST" | jq -r ".playlists[$pi].item_ids[]?" 2>/dev/null || true)
    if [[ -z "$ITEM_IDS" ]]; then
        # Fall back to items[].item_id if item_ids is absent.
        ITEM_IDS=$(printf '%s' "$PRIOR_PLAYLIST" | jq -r ".playlists[$pi].items[]?.item_id // empty" 2>/dev/null || true)
    fi
    if [[ -z "$ITEM_IDS" ]]; then
        echo "   skipping playlist $pi ($PL_NAME): no item_ids"
        continue
    fi
    video_count=0
    total_count=0
    while IFS= read -r iid; do
        [[ -n "$iid" ]] || continue
        total_count=$((total_count + 1))
        if is_video_bearing "$iid"; then
            video_count=$((video_count + 1))
        fi
    done <<<"$ITEM_IDS"
    echo "   playlist $pi ($PL_NAME): $video_count / $total_count video-bearing items"
    if [[ "$video_count" -ge 2 ]]; then
        TARGET_PLAYLIST_INDEX=$pi
        TARGET_PLAYLIST_ITEM_COUNT=$total_count
        break
    fi
done

if [[ "$TARGET_PLAYLIST_INDEX" -lt 0 ]]; then
    echo "FAIL: no playlist with ≥2 video-bearing items" >&2
    echo "INFRA: this runner needs a playlist already populated with video slides." >&2
    echo "       Per QA: the live 4-video test playlist (Aurora/Balloon/Candle/Champagne)" >&2
    echo "       or similar dual-video playlist is the canonical input." >&2
    exit 2
fi
echo "   chose playlist $TARGET_PLAYLIST_INDEX with $TARGET_PLAYLIST_ITEM_COUNT items"

# Transition kinds to cycle. iris + wipe are the key ones (iris was
# the original r106 verification; wipe was on QA's iter-1 bench list).
# 'cut' is included for coverage but skipped from the per-kind verdict
# (zero-duration; probe doesn't latch).
TRANSITIONS=(cut fade iris wipe slide marquee blinds push shutter glitch)

# Build a mutated playlist: every item in TARGET_PLAYLIST gets its
# transition rotated through TRANSITIONS, transition_ms set to 1500
# for kinds we want measurable. Everything else (id, name, item_ids,
# item bodies, content dirs) is preserved verbatim.
echo "==> mutating playlist $TARGET_PLAYLIST_INDEX item transitions (cycling ${#TRANSITIONS[@]} kinds)"
TRANSITIONS_JSON=$(printf '%s\n' "${TRANSITIONS[@]}" | jq -R . | jq -s . | tr -d '\n')
MUTATED_PLAYLIST=$(printf '%s' "$PRIOR_PLAYLIST" | jq --argjson kinds "$TRANSITIONS_JSON" --argjson pi "$TARGET_PLAYLIST_INDEX" '
    .playlists[$pi].items |= (
        to_entries
        | map(
            .value.transition = $kinds[(.key % ($kinds | length))]
            | .value.transition_ms = (if $kinds[(.key % ($kinds | length))] == "cut" then 0 else 1500 end)
            | .value
        )
    )
')

# Sanity: the mutated playlist must still parse + still have the same
# number of items as the original. A jq filter error would silently
# blank the playlist.
MUTATED_COUNT=$(printf '%s' "$MUTATED_PLAYLIST" | jq ".playlists[$TARGET_PLAYLIST_INDEX].items | length" 2>/dev/null || echo "0")
if [[ "$MUTATED_COUNT" != "$TARGET_PLAYLIST_ITEM_COUNT" ]]; then
    echo "FAIL: mutated playlist item count drift ($MUTATED_COUNT != $TARGET_PLAYLIST_ITEM_COUNT) — jq filter bug" >&2
    exit 2
fi

# Write the mutated playlist + ensure the chosen playlist is what
# the backend will play (the live file does NOT use
# active_playlist_id per QA; the playback engine picks by some
# other rule — leave that alone, only mutate the transitions).
echo "$MUTATED_PLAYLIST" | sudo tee "$PLAYLIST_PATH" >/dev/null

JOURNAL_SINCE_REF="$(date +'%Y-%m-%d %H:%M:%S')"
echo "==> restarting $BACKEND_UNIT (journal-since=\"$JOURNAL_SINCE_REF\")"
sudo systemctl restart "$BACKEND_UNIT"
sleep 5

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

# Pre-flight INFRA detection BEFORE per-kind analysis. v3 doesn't
# stage content so the v2-era 'migration needed' false-positive is
# gone, but a malformed playlist mutation could still trip a
# pruning error. Keep the gate generic but scoped to lines that
# reference begin_slide_load / playlist parse failures.
echo
echo "==> pre-flight: confirming mutated playlist was accepted"
INFRA_RE="playlist prune failed|fetch_items failed|envelope corrupted|playlists failed to parse"
INFRA_LINES=$(grep -E "$INFRA_RE" "$JOURNAL" 2>/dev/null || true)
if [[ -n "$INFRA_LINES" ]]; then
    echo "   INFRA: backend rejected the mutated playlist:"
    printf '%s\n' "$INFRA_LINES" | head -3 | sed 's/^/      /'
    echo
    echo "==========================================="
    echo "INFRA: playlist mutation error (NOT a transition regression)"
    echo "==========================================="
    echo "Inspect: $JOURNAL (copied to /tmp/v2v-golden-journal.log on exit)"
    exit 2
fi
SLIDE_LOADS=$(grep -c "begin_slide_load " "$JOURNAL" 2>/dev/null || true)
TOTAL_PROBE_SAMPLES=$(grep -c "transition_tex_probe side=" "$JOURNAL" 2>/dev/null || true)
echo "   $SLIDE_LOADS begin_slide_load, $TOTAL_PROBE_SAMPLES transition_tex_probe samples"
if [ "$SLIDE_LOADS" -eq 0 ]; then
    echo
    echo "==========================================="
    echo "INFRA: zero slides loaded — playlist mutation likely produced an unplayable playlist"
    echo "==========================================="
    echo "Hint: did the mutation drop item_ids or break item shape? Re-check the jq filter."
    exit 2
fi
if [ "$TOTAL_PROBE_SAMPLES" -eq 0 ]; then
    echo
    echo "==========================================="
    echo "INFRA: slides loaded but no transitions ran"
    echo "==========================================="
    echo "Hint: are the transitions configured? Check the live playlist's items[].transition."
    exit 2
fi
echo "   pre-flight OK — proceeding to per-kind analysis"

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
    if [ "$k" = "cut" ]; then
        verdict="N/A (cut is zero-duration)"
    elif [ "$a_n" = "0" ] || [ "$b_n" = "0" ]; then
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
deltas=$(grep -oE 'delta_ms=[0-9]+' "$JOURNAL" 2>/dev/null | sed 's/delta_ms=//' || true)
if [ -z "$deltas" ]; then
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
    echo "         missing from rotation OR stutter-regression that silenced"
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
