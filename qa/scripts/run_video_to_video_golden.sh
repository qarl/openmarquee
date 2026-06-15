#!/usr/bin/env bash
#
# Path A v2v regression-guard runner (2026-06-14, v4).
#
# v4 architecture per QA (round 3): replace jq with python3. jq is NOT
# installed on fireplacesign (or fresh-gift devices); python3 IS (the
# backend runs on it). The playlist mutation logic stays the same as
# v3 — mutate ONLY items[].transition + transition_ms on the chosen
# playlist; leave item_ids, item bodies, content dirs UNTOUCHED.
#
# Per QA dispatches: this runner mutates the LIVE known-good playlist's
# transition fields instead of fabricating synthetic content. Two prior
# rounds of test-staging bugs (schema-version envelope missing, then
# item_ids / item-shape mismatches) proved that synthesizing playlists +
# content is whack-a-mole against the production storage contracts. The
# live playlist's items are already accepted by the backend AND already
# drive video→video transitions (QA-confirmed). The "golden" property
# is the ASSERTION (live video both sides, no freeze), not the content.
#
# Exit codes:
#   0  OVERALL: PASS
#   1  OVERALL: FAIL (real regression — black side OR delta_ms > ceiling)
#   2  INFRA error (test-staging failure; not a transition regression)
#   3  PASS with SUSPICIOUS (every kind that ran was clean, but >=1
#      expected kind had 0 probe samples — review N/A rows)
#
# Usage: sudo bash qa/scripts/run_video_to_video_golden.sh
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

# Tools required. v4 dropped jq in favor of python3 (per QA round 3:
# jq is not on fireplacesign or fresh-gift devices; python3 IS — the
# backend runs on it).
for tool in python3 systemctl journalctl; do
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
    if [[ -f "$RESTORE_TMP/journal.log" ]]; then
        sudo cp "$RESTORE_TMP/journal.log" /tmp/v2v-golden-journal.log 2>/dev/null || true
    fi
    rm -rf "$RESTORE_TMP"
    return "$rc"
}
trap restore EXIT INT TERM

echo "==> snapshot taken: $RESTORE_TMP/playlist.json.prior"

# Transition kinds to cycle. iris + wipe are the key ones (iris was
# the original r106 verification; wipe was on QA's iter-1 bench list).
# 'cut' is included for coverage but skipped from the per-kind verdict
# (zero-duration; probe doesn't latch).
TRANSITIONS=(cut fade iris wipe slide marquee blinds push shutter glitch)
KINDS_PY=$(printf "'%s'," "${TRANSITIONS[@]}")
KINDS_PY="[${KINDS_PY%,}]"

echo "==> scanning playlists + mutating transitions via python3"
SCAN_OUT=$(sudo python3 - "$PLAYLIST_PATH" "$CONTENT_ROOT" "$KINDS_PY" <<'PY' 2>&1
import json, os, sys, pathlib

playlist_path = pathlib.Path(sys.argv[1])
content_root  = pathlib.Path(sys.argv[2])
kinds         = eval(sys.argv[3])  # noqa: S307 — controlled input from runner

def is_video_bearing(item_id):
    """Video-bearing iff on-disk item.json envelope holds either a
    VideoSlide (type='video') OR a TextSlide with non-null
    background_video_slide_id. Per QA: the live 4-video test playlist
    (Aurora/Balloon/Candle/Champagne) uses text_slide + bg-video."""
    item_json = content_root / item_id / "item.json"
    if not item_json.is_file():
        return False
    try:
        env = json.loads(item_json.read_text())
    except Exception:
        return False
    item = env.get("item") or {}
    if item.get("type") == "video":
        return True
    if item.get("type") == "text_slide":
        bg = item.get("background_video_slide_id")
        if bg is not None and bg != "":
            return True
    return False

try:
    data = json.loads(playlist_path.read_text())
except Exception as e:
    print(f"FAIL playlist_unreadable: {e!r}")
    sys.exit(0)

playlists = data.get("playlists") or []
if not playlists:
    print("FAIL no_playlists_in_envelope")
    sys.exit(0)

target_index = -1
target_count = 0
for pi, pl in enumerate(playlists):
    name = pl.get("name", "(unnamed)")
    items = pl.get("items") or []
    ids = pl.get("item_ids") or [it.get("item_id") for it in items if isinstance(it, dict)]
    ids = [i for i in ids if i]
    video_n = sum(1 for iid in ids if is_video_bearing(iid))
    total_n = len(ids)
    print(f"SCAN playlist={pi} name={name!r} video_bearing={video_n}/{total_n}",
          file=sys.stderr)
    if video_n >= 2 and target_index < 0:
        target_index = pi
        target_count = total_n

if target_index < 0:
    print("FAIL no_qualifying_playlist (need >= 2 video-bearing items in one playlist)")
    sys.exit(0)

# Mutate ONLY transition + transition_ms on each item of the target
# playlist, cycling through KINDS. Everything else (id, item_ids,
# item bodies, content dirs) is untouched.
target = playlists[target_index]
items = target.get("items") or []
if not items:
    print(f"FAIL chosen_playlist_has_no_items index={target_index}")
    sys.exit(0)

original_item_count = len(items)
n_kinds = len(kinds)
for i, item in enumerate(items):
    if not isinstance(item, dict):
        continue
    k = kinds[i % n_kinds]
    item["transition"] = k
    # 'cut' is zero-duration; everything else gets 1500ms for
    # measurable probe samples + a comfortable delta_ms window.
    item["transition_ms"] = 0 if k == "cut" else 1500

if len(items) != original_item_count:
    print(f"FAIL item_count_drift original={original_item_count} mutated={len(items)}")
    sys.exit(0)

# Safer write: tmp + rename so a SIGINT mid-write can't corrupt.
playlist_tmp = playlist_path.with_suffix(".json.runner-mutate")
playlist_tmp.write_text(json.dumps(data, indent=2))
os.replace(playlist_tmp, playlist_path)
print(f"OK {target_index} {original_item_count}")
PY
)
SCAN_RC=$?
if [ "$SCAN_RC" -ne 0 ]; then
    echo "FAIL: python3 mutation script crashed (rc=$SCAN_RC)" >&2
    printf '%s\n' "$SCAN_OUT" | head -10
    exit 2
fi

# Display SCAN lines (sent to stderr from within python3) + result line.
printf '%s\n' "$SCAN_OUT" | grep -E '^SCAN ' || true
RESULT_LINE=$(printf '%s\n' "$SCAN_OUT" | grep -E '^(OK|FAIL) ' | tail -1)
echo "   $RESULT_LINE"
if [[ "$RESULT_LINE" =~ ^FAIL ]]; then
    echo "INFRA: playlist mutation refused — see SCAN lines above." >&2
    exit 2
fi
TARGET_PLAYLIST_INDEX=$(echo "$RESULT_LINE" | awk '{print $2}')
TARGET_PLAYLIST_ITEM_COUNT=$(echo "$RESULT_LINE" | awk '{print $3}')
echo "   chose playlist $TARGET_PLAYLIST_INDEX with $TARGET_PLAYLIST_ITEM_COUNT items"

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
    echo "Hint: did the mutation drop item_ids or break item shape? Re-check the python3 mutation."
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
