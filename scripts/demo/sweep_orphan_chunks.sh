#!/usr/bin/env bash
# scripts/demo/sweep_orphan_chunks.sh -- drop stale esbuild hash-chunks
# from a built dist/ directory.
#
# Problem: `npm run build` produces content-hashed chunk filenames like
# `sortable.esm-PUWFMERW.js`. When esbuild re-runs against a changed
# source, the hash flips but the OLD hashed file isn't cleaned --
# `sortable.esm-RLL2D47F.js` (old) and `sortable.esm-PUWFMERW.js` (new)
# both end up in dist/. main.js only loads the new one, so the orphan
# is dead weight that gets deployed. Same shape as the 2026-05-03
# ffmpeg-worker tempfile leak.
#
# This sweep:
#   1. Reads all non-hashed entry files in dist/ (main.js, login.js,
#      set-password.js, welcome.js, simulator.js, spike.js,
#      ffmpeg-worker.js).
#   2. Greps each for `<name>-<8-uppercase-hex>.js` import references.
#   3. Lists every `*-<8-uppercase-hex>.js` actually present in dist/.
#   4. Removes the set difference: present-but-unreferenced.
#
# Usage:
#   bash scripts/demo/sweep_orphan_chunks.sh <dist-dir>
#   bash scripts/demo/sweep_orphan_chunks.sh --dry-run <dist-dir>

set -euo pipefail

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
    shift
fi

DIST_DIR="${1:-}"
if [ -z "$DIST_DIR" ] || [ ! -d "$DIST_DIR" ]; then
    echo "usage: $0 [--dry-run] <dist-dir>" >&2
    exit 2
fi

# Discover entry files dynamically: any .js in dist/ that's NOT shaped
# like a hashed chunk (`*-<8HASH>.js`). Hardcoding the entry list
# (main.js / login.js / etc.) would silently bite when a new entry
# lands and its hashed chunk references get missed -- the sweep would
# delete a real dependency. Dynamic discovery self-heals.
referenced=()
while read -r entry_path; do
    [ -f "$entry_path" ] || continue
    while read -r ref; do
        # Strip any leading ./ or /dist/ paths esbuild emits.
        name=$(basename "$ref")
        referenced+=("$name")
    done < <(grep -oE '[A-Za-z][A-Za-z0-9._-]*-[A-Z0-9]{8}\.js' "$entry_path" | sort -u)
done < <(find "$DIST_DIR" -maxdepth 1 -type f -name '*.js' \
         -not -regex '.*-[A-Z0-9]\{8\}\.js$')

# Deduplicate referenced set into a lookup-friendly newline-separated
# string for `grep -Fx` membership check below.
referenced_uniq=$(printf '%s\n' "${referenced[@]+"${referenced[@]}"}" | sort -u)

# Now enumerate every hashed file actually in dist/. Same regex shape.
removed=0
kept=0
while read -r path; do
    [ -f "$path" ] || continue
    name=$(basename "$path")
    if printf '%s\n' "$referenced_uniq" | grep -Fxq "$name"; then
        kept=$((kept + 1))
        continue
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRYRUN: would remove orphan: $path"
    else
        rm -f "$path"
        echo "removed orphan chunk: $path"
    fi
    removed=$((removed + 1))
done < <(find "$DIST_DIR" -maxdepth 1 -type f -regex '.*-[A-Z0-9]\{8\}\.js$')

echo "sweep: kept=$kept removed=$removed"
