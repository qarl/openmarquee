#!/usr/bin/env bash
# 2026-06-13 — Live-pipeline regression runner for the FYS PRELOAD_MODE bug.
#
# The offscreen golden runner at run_transition_golden_bg.sh exercises the
# `--capture-sb-mid` offline path with poster substitution. It did NOT
# catch the LIVE PaintTransition dual-decoder starvation that produced
# the BLACK outgoing video under `OPENMARQUEE_PRELOAD_MODE=max`. This
# runner closes that gap on a Pi-class Linux box.
#
# WHAT IT DOES
#   1. Builds a temp content-root with two text-over-video slides backed
#      by QA's golden mp4s (../../qa/fixtures/transition-golden/).
#   2. Boots the openmarquee backend systemd unit with the test content
#      bind-mounted in, ONCE for each PRELOAD_MODE under test.
#   3. Tails the renderer's [perf] lines from the unit's journal during a
#      ~30-second soak (~6 transitions).
#   4. Pipes the capture through the host-portable analyzer at
#      backend.openmarquee.rendering.preload_journal — asserts:
#        * MODE=defer ⇒ production_clean()    (must hold)
#        * MODE=max   ⇒ NOT production_clean() (the regression we lock)
#      The MODE=max arm is opt-in via RUN_BROKEN_MODE_AB=1 because it
#      requires the sign to actually display a glitched transition for
#      ~30s — fine on a bench, do not run unattended on customer FYS.
#
# REQUIREMENTS
#   * Linux box with /dev/dri/card* + bcm2835-codec (Pi 3/4/Zero 2 W).
#   * systemd-managed openmarquee-backend unit (the production shape).
#   * `journalctl` + `python3` with the backend package importable
#     (PYTHONPATH=<repo>/backend, or install -e .).
#   * The golden fixtures + ffmpeg for poster extraction.
#
# USAGE
#   FIXTURES=/path/to/qa/fixtures/transition-golden \
#     CONTENT_ROOT=/var/openmarquee/content-livetest \
#     PLAYLIST_PATH=/var/openmarquee/playlist-livetest.json \
#     RUN_BROKEN_MODE_AB=0 \
#     renderer/tests/scripts/run_live_preload_contention.sh
#
# EXIT CODES
#   0 — defer mode soak passes (and, if RUN_BROKEN_MODE_AB=1, max mode
#       soak demonstrably reproduces the regression).
#   1 — defer mode soak FAILED (regression in production config).
#   2 — environment / preflight check failed.

set -euo pipefail

# FIXTURES default: walk up to the outer repo's qa/fixtures dir.
# `${BASH_SOURCE[0]}` resolves the script's own path even when sourced
# via a relative argv; from renderer/tests/scripts/ that's ../../../qa/
# fixtures/transition-golden (under qarl's repo layout). The runner on
# a Pi will typically `FIXTURES=...` override anyway; the default is a
# best-effort developer-box value, NOT a Pi-friendly path. Document
# this loudly in the preflight if the path doesn't exist.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES="${FIXTURES:-$_SCRIPT_DIR/../../../qa/fixtures/transition-golden}"
CONTENT_ROOT="${CONTENT_ROOT:-/var/openmarquee/content-livetest}"
PLAYLIST_PATH="${PLAYLIST_PATH:-/var/openmarquee/playlist-livetest.json}"
RUN_BROKEN_MODE_AB="${RUN_BROKEN_MODE_AB:-0}"
SOAK_SECONDS="${SOAK_SECONDS:-30}"
SERVICE_NAME="${SERVICE_NAME:-openmarquee-backend}"

err() { printf '%s\n' "ERR: $*" >&2; }
log() { printf '%s\n' "$*" >&2; }

# Cleanup trap — sacred-review BLOCKER-2 fix. Any path out of this
# script (success, error, Ctrl-C, SIGTERM) MUST remove the drop-in
# file that activates the test mode and restart the unit back to
# its production posture. Without this, a failure mid-soak left the
# sign with Environment=OPENMARQUEE_PRELOAD_MODE=$mode persistent
# across reboots — which is EXACTLY the FYS 2026-06-13 bug shape
# we're locking against.
ACTIVE_DROP_IN_FILE=""
cleanup_drop_in() {
  if [ -n "$ACTIVE_DROP_IN_FILE" ] && [ -f "$ACTIVE_DROP_IN_FILE" ]; then
    log "Cleanup: removing test drop-in at $ACTIVE_DROP_IN_FILE..."
    sudo rm -f "$ACTIVE_DROP_IN_FILE" || true
    sudo systemctl daemon-reload || true
    # Restart so the running process drops the test env. Tolerate
    # restart failure (operator may have stopped the unit
    # intentionally; we don't want the trap itself to fail).
    sudo systemctl restart "$SERVICE_NAME" 2>/dev/null || true
  fi
  ACTIVE_DROP_IN_FILE=""
}
trap cleanup_drop_in EXIT INT TERM

# ---------------------------------------------------------------- preflight

if [ "$(uname -s)" != "Linux" ]; then
  err "this runner only runs on Linux (uses systemd + /dev/dri); on $(uname -s)"
  exit 2
fi
for tool in journalctl python3 ffmpeg systemctl; do
  if ! command -v "$tool" >/dev/null; then
    err "$tool missing"; exit 2
  fi
done
if [ ! -d "$FIXTURES" ]; then err "FIXTURES dir not found: $FIXTURES"; exit 2; fi
for f in golden-red.mp4 golden-blue.mp4 spec.md; do
  if [ ! -f "$FIXTURES/$f" ]; then err "missing $FIXTURES/$f"; exit 2; fi
done
if ! systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null \
    && ! systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
  err "$SERVICE_NAME unit not registered. This runner drives the production unit."
  exit 2
fi

# ------------------------------------------------------ content-root build

# Two text-over-video slides referencing the two golden videos. Deterministic
# UUIDs so soaks are reproducible.
TEXT_A_ID="aaaaaaaa-aaaa-4000-8000-707070707071"
TEXT_B_ID="aaaaaaaa-aaaa-4000-8000-707070707072"
VID_RED_ID="f0e1d2c3-b4a5-4960-8780-707070707070"
VID_BLUE_ID="f0e1d2c3-b4a5-4960-8780-707070707071"

build_video_asset () {
  local id="$1" src_mp4="$2"
  local dir="$CONTENT_ROOT/$id"
  sudo install -d -o openmarquee -g openmarquee -m 0755 "$dir"
  sudo install -m 0644 "$src_mp4" "$dir/asset.mp4"
  sudo ffmpeg -y -loglevel error -i "$dir/asset.mp4" -vframes 1 \
    -pix_fmt yuv420p \
    -vf 'scale=1280:720:flags=lanczos:in_color_matrix=bt709:in_range=limited' \
    -color_range tv -colorspace bt709 \
    -color_primaries bt709 -color_trc bt709 \
    "$dir/poster.png"
  sudo tee "$dir/item.json" >/dev/null <<JSON
{
  "schema_version": 3,
  "item": {
    "type": "video",
    "id": "$id",
    "name": "golden-$(basename "$src_mp4" .mp4)",
    "duration_ms": 5000,
    "transition": "cut",
    "transition_ms": 500
  }
}
JSON
}

build_text_over_video () {
  local id="$1" bg_id="$2" caption="$3"
  local dir="$CONTENT_ROOT/$id"
  sudo install -d -o openmarquee -g openmarquee -m 0755 "$dir"
  sudo tee "$dir/item.json" >/dev/null <<JSON
{
  "schema_version": 3,
  "item": {
    "type": "text_slide",
    "id": "$id",
    "name": "$caption",
    "duration_ms": 5000,
    "background_color": "#000000",
    "background_video_slide_id": "$bg_id",
    "text_layers": [
      {
        "text": "$caption",
        "name": "caption",
        "font_size_pct": 50.0,
        "text_color": "#FFFFFF",
        "box": {"x": 0.1, "y": 0.4, "w": 0.8, "h": 0.2}
      }
    ],
    "transition": "fade",
    "transition_ms": 800
  }
}
JSON
}

log "Building test content-root at $CONTENT_ROOT..."
sudo install -d -o openmarquee -g openmarquee -m 0755 "$CONTENT_ROOT"
build_video_asset "$VID_RED_ID" "$FIXTURES/golden-red.mp4"
build_video_asset "$VID_BLUE_ID" "$FIXTURES/golden-blue.mp4"
build_text_over_video "$TEXT_A_ID" "$VID_RED_ID" "TEST-A"
build_text_over_video "$TEXT_B_ID" "$VID_BLUE_ID" "TEST-B"

log "Writing test playlist at $PLAYLIST_PATH..."
sudo tee "$PLAYLIST_PATH" >/dev/null <<JSON
{
  "schema_version": 4,
  "playlists": [
    {
      "id": "00000000-0000-4000-8000-000000000001",
      "name": "LiveTest",
      "items": [
        {"item_id": "$TEXT_A_ID", "transition": "fade", "transition_ms": 800},
        {"item_id": "$TEXT_B_ID", "transition": "fade", "transition_ms": 800}
      ]
    }
  ]
}
JSON

# ------------------------------------------------------ soak under MODE

run_soak () {
  local mode="$1"
  log "----------------------------------------------------------------"
  log "Soaking under OPENMARQUEE_PRELOAD_MODE=$mode for $SOAK_SECONDS s..."
  local drop_in="/etc/systemd/system/${SERVICE_NAME}.service.d/livetest-preload-mode.conf"
  # Register the drop-in path with the trap BEFORE writing it so a
  # crash between `install` and the explicit cleanup-on-success at
  # the function tail still triggers removal.
  ACTIVE_DROP_IN_FILE="$drop_in"
  sudo install -d /etc/systemd/system/"${SERVICE_NAME}.service.d"
  sudo tee "$drop_in" >/dev/null <<UNIT
[Service]
Environment=OPENMARQUEE_PLAYLIST_PATH=$PLAYLIST_PATH
Environment=OPENMARQUEE_CONTENT_ROOT=$CONTENT_ROOT
Environment=OPENMARQUEE_PRELOAD_MODE=$mode
UNIT
  sudo systemctl daemon-reload
  sudo systemctl restart "$SERVICE_NAME"
  local since
  since="$(date '+%Y-%m-%d %H:%M:%S')"
  sleep "$SOAK_SECONDS"
  # Capture and analyze.
  local capture
  capture="$(sudo journalctl -u "$SERVICE_NAME" --since "$since" -o cat 2>&1)"
  log "Capture window: $(printf '%s\n' "$capture" | wc -l) lines."
  # Pipe through the host-portable analyzer.
  local result_json
  result_json="$(printf '%s\n' "$capture" \
    | python3 -c "
import json, sys
sys.path.insert(0, '$(realpath "$(dirname "$0")/../../../backend")')
from openmarquee.rendering.preload_journal import classify
s = classify(sys.stdin.read().splitlines())
print(json.dumps({
    'endpoint_a_no_frame': s.endpoint_a_no_frame,
    'endpoint_b_no_frame': s.endpoint_b_no_frame,
    'preload_handoff_normal': s.preload_handoff_normal,
    'preload_handoff_deferred': s.preload_handoff_deferred,
    'preload_handoff_frames_drained_zero_normal':
        s.preload_handoff_frames_drained_zero_normal,
    'deferred_for_codec_contention': s.deferred_for_codec_contention,
    'bake_b_deadline_exhausted': s.bake_b_deadline_exhausted,
    'experiment_warning_modes': sorted(s.experiment_warning_modes),
    'production_clean': s.production_clean(),
    'is_starvation_signature_present': s.is_starvation_signature_present(),
}))
")"
  printf '[mode=%s] %s\n' "$mode" "$result_json"
  # Cleanup-on-success: remove the drop-in + restart the unit back
  # to its production posture. The trap covers the failure paths.
  sudo rm -f "$drop_in"
  sudo systemctl daemon-reload
  sudo systemctl restart "$SERVICE_NAME"
  ACTIVE_DROP_IN_FILE=""
  printf '%s\n' "$result_json"
}

# ------------------------------------------------------ assertions

main () {
  local defer_json
  defer_json="$(run_soak defer | tail -1)"
  log "Defer soak result: $defer_json"
  if ! printf '%s' "$defer_json" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["production_clean"] else 1)'; then
    err "PRODUCTION soak under OPENMARQUEE_PRELOAD_MODE=defer FAILED to look clean."
    err "Inspect the journal capture; the starvation signature is present at the default mode."
    return 1
  fi
  log "PASS: defer mode soak looks production-clean."

  if [ "$RUN_BROKEN_MODE_AB" = "1" ]; then
    local max_json
    max_json="$(run_soak max | tail -1)"
    log "Max soak result: $max_json"
    if printf '%s' "$max_json" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["production_clean"] else 1)'; then
      err "PRELOAD_MODE=max DID NOT reproduce the FYS regression signature."
      err "Either the bug has been fixed in the binary (good news, investigate) or"
      err "the soak window was too short / the playlist wasn't reached."
      return 1
    fi
    log "PASS: max mode soak demonstrably reproduces the FYS 2026-06-13 starvation signature."
  else
    log "Skipping max-mode A/B comparison (RUN_BROKEN_MODE_AB=0). Pass RUN_BROKEN_MODE_AB=1 to run both."
  fi
}

main
