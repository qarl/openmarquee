#!/bin/bash
# /usr/local/bin/mini-play.sh — openMarquee boot-default video fallback.
#
# The smallest possible renderer: H264 HW-decode -> KMS, looping a
# sequence of clips. Stack: gstreamer (v4l2h264dec + kmssink), ZERO
# shared code with openmarquee-render. This is the Layer-3 stability
# cap rest-state AND the first-boot default, so a fresh sign is never
# a DARK screen.
#
# Runs under openmarquee-mini.service (Type=simple, Restart=on-failure).
# The stability promoter stops mini before starting the Python backend;
# on backend failure / reboot-loop cap it leaves mini running as the
# safe fallback.
#
# Video selection (generalized 2026-07-09, GAP1 — NO hardcoded UUIDs):
#   1. playlist.json — the first playlist's item assets, in order
#   2. else any staged content assets (/var/openmarquee/content/*/asset.*)
#   3. else the bundled welcome clip shipped in the image
# Only existing files are kept; the first non-empty list wins. A fresh
# Jason sign with no content still plays the welcome clip.
#
# Cold pipeline restart at each clip seam (a brief black frame) is
# expected + accepted for a fallback. No rotation: CPU videoflip
# crushed 1080p to 2-4fps on the Pi Zero 2 W, so native/landscape only
# (HW-plane rotation via kmssink is a future feature, not this).

set -eu

CONTENT_ROOT="${OPENMARQUEE_CONTENT_ROOT:-/var/openmarquee/content}"
PLAYLIST_JSON="${OPENMARQUEE_PLAYLIST_PATH:-/var/openmarquee/playlist.json}"
WELCOME_CLIP="${OPENMARQUEE_WELCOME_CLIP:-/opt/openmarquee/assets/welcome.mp4}"
MAX_CLIPS="${OPENMARQUEE_MINI_MAX_CLIPS:-20}"

log() { printf '[mini] %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }

# --- preflight: the GStreamer HW-decode stack must be present ---
command -v gst-launch-1.0 >/dev/null 2>&1 \
    || die "gst-launch-1.0 missing (need gstreamer1.0-tools)"
gst-inspect-1.0 v4l2h264dec >/dev/null 2>&1 \
    || die "v4l2h264dec missing (need gstreamer1.0-plugins-bad)"
gst-inspect-1.0 kmssink >/dev/null 2>&1 \
    || die "kmssink missing (need gstreamer1.0-plugins-bad)"

# --- single HW decoder: refuse to double-start ---
# Only matches another *gst-launch* HW-decode pipeline (a stale mini
# instance) — NOT openmarquee-render (its cmdline is the Rust binary,
# not gst-launch) and NOT this script (its cmdline is bash). The
# render<->mini handoff of /dev/video10 + the DRM master is enforced
# elsewhere (the stability promoter + backend.service ExecStartPre stop
# mini before the renderer starts); this guard just stops two mini
# pipelines fighting. At startup mini has not spawned a gst-launch
# child yet, so it's a clean cross-process check.
if pgrep -f 'gst-launch-1.0.*v4l2h264dec' >/dev/null 2>&1; then
    die "another gst-launch HW-decode pipeline is already running; refusing to double-start"
fi

# --- build the clip list ---
VIDEOS=()

# (1) playlist.json: first playlist's items, in order, mapped to their
# content assets. Schema-tolerant + fully guarded — ANY failure yields
# an empty list and we fall through to the content glob. Uses python3
# (present via python3-gi) to parse JSON without a jq dependency; this
# does not import openmarquee, so it works even when the backend is
# broken (which is exactly when mini runs).
if [ -f "$PLAYLIST_JSON" ] && command -v python3 >/dev/null 2>&1; then
    while IFS= read -r line; do
        [ -n "$line" ] && VIDEOS+=("$line")
    done < <(python3 - "$PLAYLIST_JSON" "$CONTENT_ROOT" "$MAX_CLIPS" <<'PYEOF'
import glob, json, os, sys

path, content_root, maxn = sys.argv[1], sys.argv[2], int(sys.argv[3])
VIDEO_EXTS = ("mp4", "mov", "m4v")  # qtdemux-compatible containers
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    sys.exit(0)

# Tolerate schema variants: a collection {"playlists":[...]} (each with
# "items":[{"item_id":..}] on v4 or "item_ids":[uuid] on v2/3), or a
# bare single playlist dict.
if isinstance(data, dict) and isinstance(data.get("playlists"), list):
    playlists = data["playlists"]
elif isinstance(data, dict):
    playlists = [data]
else:
    playlists = []

ids = []
for pl in playlists:
    if not isinstance(pl, dict):
        continue
    items = pl.get("items")
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("item_id"):
                ids.append(str(it["item_id"]))
            elif isinstance(it, str):
                ids.append(it)
    else:
        for i in pl.get("item_ids") or []:
            ids.append(str(i))
    if ids:
        break  # first non-empty playlist wins

seen = set()
out = []
for cid in ids:
    if cid in seen:
        continue
    seen.add(cid)
    for m in sorted(glob.glob(os.path.join(content_root, cid, "asset.*"))):
        if m.rsplit(".", 1)[-1].lower() in VIDEO_EXTS:
            out.append(m)
            break
    if len(out) >= maxn:
        break

for p in out:
    print(p)
PYEOF
    )
fi

# (2) fallback: any staged content assets, stable-sorted for determinism.
if [ "${#VIDEOS[@]}" -eq 0 ]; then
    while IFS= read -r f; do
        case "${f##*.}" in
            mp4 | mov | m4v | MP4 | MOV | M4V) VIDEOS+=("$f") ;;
        esac
    done < <(find "$CONTENT_ROOT" -maxdepth 2 -type f -name 'asset.*' 2>/dev/null \
        | sort | head -n "$MAX_CLIPS")
fi

# (3) final fallback: the bundled welcome clip.
if [ "${#VIDEOS[@]}" -eq 0 ]; then
    if [ -f "$WELCOME_CLIP" ]; then
        VIDEOS+=("$WELCOME_CLIP")
    else
        die "no playlist/content assets and welcome clip missing at $WELCOME_CLIP"
    fi
fi

# Keep only files that still exist (playlist may reference pruned assets).
EXISTING=()
for v in "${VIDEOS[@]}"; do
    [ -f "$v" ] && EXISTING+=("$v")
done
if [ "${#EXISTING[@]}" -eq 0 ]; then
    if [ -f "$WELCOME_CLIP" ]; then
        EXISTING+=("$WELCOME_CLIP")
    else
        die "no playable clips found"
    fi
fi

log "looping ${#EXISTING[@]} clip(s)"
while true; do
    for VIDEO in "${EXISTING[@]}"; do
        gst-launch-1.0 -q \
            filesrc location="$VIDEO" ! \
            qtdemux name=d d. ! queue ! \
            h264parse ! v4l2h264dec ! \
            kmssink sync=true \
            || { log "pipeline exited non-zero on $VIDEO; sleeping 1s"; sleep 1; }
    done
done
