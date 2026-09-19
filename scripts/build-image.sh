#!/usr/bin/env bash
# scripts/build-image.sh — produce an openMarquee Pi OS SD-card image.
#
# Drives pi-gen with our images/openmarquee/ recipe. Runs the build
# inside Docker (pi-gen has fragile build-time deps and pi-gen's own
# build-docker.sh handles that for us). Output lands at
# /tmp/openmarquee-pi-image-<YYYY-MM-DD>.img.xz.
#
# Usage:
#     bash scripts/build-image.sh                   # build it
#     bash scripts/build-image.sh --dry-run         # print actions only
#     bash scripts/build-image.sh --ssh-key <path>  # substitute key into user-data
#     bash scripts/build-image.sh --workdir <dir>   # pi-gen checkout location
#
# Prerequisites:
#   - Docker (or Docker Desktop on macOS) is running.
#   - ~15-18GB free scratch space, MOST of it inside the Docker VM (pi-gen's
#     work/ holds several forwarded rootfs copies). On Docker Desktop that
#     counts against the host boot volume, so treat <20GB free as too tight.
#   - Internet access for the apt fetch during pi-gen's stages.
#
# Clean-slate contract (2026-09-16): each run PURGES the prior run's gitignored
# scratch (pi-gen work/ + deploy/ + Docker build cache + any stale pigen_work
# container) BEFORE building, so a failed/retried build can't stack its
# footprint on the last one's and balloon host disk. A genuinely in-progress
# build (running pigen_work container) is detected and NOT clobbered.

set -euo pipefail

OPENMARQUEE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_RECIPE_DIR="${OPENMARQUEE_ROOT}/images/openmarquee"

DRY_RUN=0
WORKDIR="${WORKDIR:-/tmp/pi-gen-openmarquee}"
SSH_KEY_PATH=""
PI_GEN_REF="${PI_GEN_REF:-arm64}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp}"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   DRY_RUN=1; shift ;;
        --workdir)   WORKDIR="$2"; shift 2 ;;
        --ssh-key)   SSH_KEY_PATH="$2"; shift 2 ;;
        --output)    OUTPUT_DIR="$2"; shift 2 ;;
        --help|-h)   sed -n '2,20p' "$0"; exit 0 ;;
        *)           echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

say() { printf '==> %s\n' "$*"; }
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'DRYRUN: %s\n' "$*"
    else
        "$@"
    fi
}

# Bounded `docker` call — never hang the build on a wedged/unresponsive daemon
# (a real failure mode: 2026-09-16 a disk-full event left the Docker VM wedged
# and `docker ps` hung indefinitely). Prints the command's stdout; returns
# docker's exit code, or 124 on timeout. Prefers coreutils timeout/gtimeout,
# else a portable background-kill fallback (macOS ships no GNU `timeout`).
DOCKER_TIMEOUT="${DOCKER_TIMEOUT:-20}"
docker_bounded() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "$DOCKER_TIMEOUT" docker "$@" 2>/dev/null
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$DOCKER_TIMEOUT" docker "$@" 2>/dev/null
    else
        local out rc n=0
        out="$(mktemp)"
        docker "$@" >"$out" 2>/dev/null &
        local pid=$!
        while kill -0 "$pid" 2>/dev/null; do
            n=$((n + 1))
            if [ "$n" -ge "$DOCKER_TIMEOUT" ]; then
                kill -9 "$pid" 2>/dev/null
                rm -f "$out"
                return 124
            fi
            sleep 1
        done
        wait "$pid" 2>/dev/null
        rc=$?
        cat "$out"
        rm -f "$out"
        return "$rc"
    fi
}
# Emit a clear, actionable failure when Docker is unresponsive (rc 124) rather
# than hanging or proceeding into a confusing mid-build failure.
docker_wedged_die() {
    echo "ERROR: Docker daemon is not responding ('docker $*' timed out after ${DOCKER_TIMEOUT}s)." >&2
    echo "       Cannot verify build state or clean scratch. Restart Docker Desktop and retry." >&2
    exit 1
}

# --- 1. Sanity-check the image recipe ---------------------------------------

say "Verify image recipe at ${IMAGE_RECIPE_DIR}"
for required in pi-gen.config stage-openmarquee/EXPORT_IMAGE \
                stage-openmarquee/prerun.sh \
                stage-openmarquee/00-install-packages/00-packages \
                cloud-init/user-data cloud-init/meta-data; do
    if [ ! -e "${IMAGE_RECIPE_DIR}/${required}" ]; then
        echo "ERROR: missing ${IMAGE_RECIPE_DIR}/${required}" >&2
        echo "       run from a clean openmarquee checkout" >&2
        exit 1
    fi
done

# --- 1b. Refuse to clobber an in-progress build -----------------------------
#
# A running pi-gen build owns a Docker container named `pigen_work`. If one is
# RUNNING, this run must NOT clean-slate + rebuild on top of it — that races
# the shared rootfs/work tree and can corrupt both builds (and the fleet rule
# is that pi-gen builds are sequenced, never concurrent). Refuse loudly. A
# STOPPED pigen_work (leftover from a failed/killed build) is fine — the
# clean-slate in 2b removes it. The check is bounded (docker_bounded) so a
# wedged daemon dies with a clear "restart Docker" message instead of hanging;
# a non-timeout docker error just warns and proceeds (build-docker.sh will
# surface the real Docker error). Skipped under --dry-run (no live state).
if [ "$DRY_RUN" -eq 1 ]; then
    say "DRYRUN: would refuse if a pi-gen build is IN PROGRESS (running 'pigen_work' container)"
elif command -v docker >/dev/null 2>&1; then
    # set +e around the capture so a non-zero docker rc doesn't trip set -e
    # before we can inspect it (124 = timed out on a wedged daemon).
    set +e
    RUNNING_PIGEN="$(docker_bounded ps --filter 'name=^/pigen_work$' --filter status=running -q)"
    dps_rc=$?
    set -e
    if [ "$dps_rc" -eq 124 ]; then
        docker_wedged_die "ps"
    elif [ "$dps_rc" -ne 0 ]; then
        say "  'docker ps' exited ${dps_rc} (daemon down?); build-docker.sh will surface a clear Docker error"
    elif [ -n "$RUNNING_PIGEN" ]; then
        echo "ERROR: a pi-gen build appears to be IN PROGRESS (container 'pigen_work' is running)." >&2
        echo "       Refusing to clean-slate + rebuild on top of it. Wait for it to finish," >&2
        echo "       or if it is stale:  docker rm -f pigen_work" >&2
        exit 1
    fi
else
    say "docker CLI not found; skipping in-progress guard (build will fail later without Docker)"
fi

# --- 2. Clone or refresh pi-gen ---------------------------------------------

say "Ensure pi-gen checkout at ${WORKDIR}"
if [ -d "${WORKDIR}/.git" ]; then
    say "  workdir present; fetch + reset to ${PI_GEN_REF}"
    run git -C "$WORKDIR" fetch --depth 1 origin "$PI_GEN_REF"
    run git -C "$WORKDIR" reset --hard "origin/${PI_GEN_REF}"
else
    say "  clone pi-gen (${PI_GEN_REF})"
    run git clone --depth 1 --branch "$PI_GEN_REF" \
        https://github.com/RPi-Distro/pi-gen.git "$WORKDIR"
fi

# --- 2b. Clean-slate scratch (prevent retry cruft accumulation) -------------
#
# pi-gen's build intermediates are gitignored, so section 2's `git reset
# --hard` does NOT remove them: work/ (per-stage rootfs copies pi-gen forwards
# stage-to-stage — several GB, mostly inside the Docker VM) and deploy/ (the
# built .img.xz) survive across runs, and Docker build cache accumulates
# layers each retry. A failed/retried build therefore stacks its footprint on
# the previous one's and balloons host scratch (2026-09-16: retry cruft +
# cache reached ~30GB and filled the boot volume, wedging the fleet). Start
# every build from a clean slate so a single build's peak can't compound.
#
# work/ + deploy/ are written by pi-gen AS ROOT (inside the build container),
# so removing them needs sudo — matching build-docker.sh's own sudo. Guarded
# to a real pi-gen checkout so a mis-set WORKDIR can't rm the wrong tree.
say "Clean-slate pi-gen scratch (work/ + deploy/ + docker build cache)"
if [ "$DRY_RUN" -eq 1 ]; then
    say "DRYRUN: sudo rm -rf ${WORKDIR}/work ${WORKDIR}/deploy"
    say "DRYRUN: would 'docker rm -f pigen_work' (if a stale container exists) + 'docker builder prune -f'"
else
    # Safety: only ever clean inside a real pi-gen git checkout, so a mis-set
    # WORKDIR can't rm the wrong tree. (Checked only for real runs — under
    # --dry-run the clone above is simulated, so .git won't exist yet.)
    if [ ! -d "${WORKDIR}/.git" ] || [ ! -f "${WORKDIR}/build-docker.sh" ]; then
        echo "ERROR: ${WORKDIR} is not a pi-gen checkout (needs .git + build-docker.sh); refusing to clean it" >&2
        exit 1
    fi
    sudo rm -rf "${WORKDIR}/work" "${WORKDIR}/deploy"
    # Remove a leftover STOPPED build container from a prior failed run so
    # pi-gen recreates it cleanly (the RUNNING case was already refused in 1b),
    # and prune accumulated build cache so retried layers don't persist. Both
    # non-fatal — a cleanup hiccup must not block the build. Bounded so a
    # wedged daemon dies clean rather than hanging (prune gets a generous
    # bound since a real prune of a large cache legitimately takes a while).
    if command -v docker >/dev/null 2>&1; then
        set +e
        STALE_PIGEN="$(docker_bounded ps -a --filter 'name=^/pigen_work$' -q)"
        dpa_rc=$?
        set -e
        [ "$dpa_rc" -eq 124 ] && docker_wedged_die "ps -a"
        if [ -n "$STALE_PIGEN" ]; then
            # Plain `docker rm` (NO -f): removes a STOPPED leftover, but
            # REFUSES a container that raced into RUNNING between 1b and here
            # (a concurrent build) rather than force-killing it — the refusal
            # is caught by `|| say` and we continue. Closes the 1b/2b TOCTOU.
            docker_bounded rm pigen_work >/dev/null 2>&1 \
                || say "  (pigen_work not removed — absent, or running from a concurrent build; continuing)"
        fi
        DOCKER_TIMEOUT=180 docker_bounded builder prune -f >/dev/null 2>&1 \
            || say "  (docker builder prune failed or timed out; continuing)"
    else
        say "  docker CLI not found; skipping stale-container + build-cache prune"
    fi
fi

# --- 3. Copy our config + custom stage into pi-gen workdir ------------------

say "Stage pi-gen.config -> ${WORKDIR}/config"
run cp "${IMAGE_RECIPE_DIR}/pi-gen.config" "${WORKDIR}/config"

say "Stage stage-openmarquee/ -> ${WORKDIR}/stage-openmarquee/"
run rm -rf "${WORKDIR}/stage-openmarquee"
run cp -r "${IMAGE_RECIPE_DIR}/stage-openmarquee" "${WORKDIR}/stage-openmarquee"

# Strip AppleDouble ._* sidecars from the copied tree. The ~/project source
# lives on the Mountain Duck SFTP mount, which regenerates a ._<name> xattr
# sidecar for every file; the `cp -r` above drags them into WORKDIR. pi-gen
# enumerates substage scripts by glob, and a stray EXECUTABLE ._NN-run.sh
# could be picked up as a substage runner (or a ._file could land in the
# image rootfs via a substage's files/). WORKDIR is on /tmp (local, not the
# mount), so a clean strip here stays clean for the rest of the build.
run find "${WORKDIR}/stage-openmarquee" -name '._*' -delete

# --- 4. Skip desktop stages (3, 4, 5) ---------------------------------------

say "Skip stages 3/4/5 (X11 / LXDE / Recommended)"
for stage in stage3 stage4 stage5; do
    run touch "${WORKDIR}/${stage}/SKIP" "${WORKDIR}/${stage}/SKIP_IMAGES"
done

# --- 5. Substitute SSH key into cloud-init user-data ------------------------

CLOUD_INIT_USER_DATA="${WORKDIR}/stage-openmarquee/cloud-init/user-data"
say "Stage cloud-init user-data with key substitution"
run mkdir -p "$(dirname "$CLOUD_INIT_USER_DATA")"

if [ -n "$SSH_KEY_PATH" ]; then
    if [ ! -f "$SSH_KEY_PATH" ]; then
        echo "ERROR: --ssh-key ${SSH_KEY_PATH} not found" >&2
        exit 1
    fi
    SSH_KEY_CONTENT=$(cat "$SSH_KEY_PATH")
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'DRYRUN: substitute {{SSH_AUTHORIZED_KEYS}} -> <%s>\n' "$SSH_KEY_PATH"
        printf 'DRYRUN: write %s\n' "$CLOUD_INIT_USER_DATA"
    else
        # Use python instead of sed to avoid any chance of delimiter
        # collision with the SSH key string (which contains '/', '+',
        # '=' and other sed-meta-prone chars).
        python3 - "$CLOUD_INIT_USER_DATA" "$SSH_KEY_CONTENT" \
                "${IMAGE_RECIPE_DIR}/cloud-init/user-data" <<'PY'
import sys
out_path, key, template_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(template_path) as f:
    body = f.read()
body = body.replace("{{SSH_AUTHORIZED_KEYS}}", key)
with open(out_path, "w") as f:
    f.write(body)
PY
    fi
else
    say "  no --ssh-key; copying user-data WITHOUT substitution (cloud-init will fail SSH attach)"
    say "  rerun with --ssh-key ~/.ssh/id_ed25519.pub to fix"
    run cp "${IMAGE_RECIPE_DIR}/cloud-init/user-data" "$CLOUD_INIT_USER_DATA"
fi
run cp "${IMAGE_RECIPE_DIR}/cloud-init/meta-data" \
       "${WORKDIR}/stage-openmarquee/cloud-init/meta-data"

# --- 6. Invoke pi-gen build (Docker) ----------------------------------------

say "Run pi-gen build (Docker; may take 15-30 min on first run)"
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRYRUN: cd %s && sudo ./build-docker.sh\n' "$WORKDIR"
else
    (cd "$WORKDIR" && sudo ./build-docker.sh)
fi

# --- 7. Move output to OUTPUT_DIR -------------------------------------------

say "Move .img.xz to ${OUTPUT_DIR}"
TIMESTAMP=$(date +%Y-%m-%d)
OUTPUT_NAME="openmarquee-pi-image-${TIMESTAMP}.img.xz"
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRYRUN: cp %s/deploy/*-openmarquee-*.img.xz %s/%s\n' \
           "$WORKDIR" "$OUTPUT_DIR" "$OUTPUT_NAME"
else
    # pi-gen's output filename is <date>-<IMG_NAME>-<RELEASE>-...-openmarquee.img.xz
    BUILT_IMG=$(ls -t "${WORKDIR}/deploy/"*-openmarquee-*.img.xz 2>/dev/null | head -1)
    if [ -z "$BUILT_IMG" ] || [ ! -f "$BUILT_IMG" ]; then
        echo "ERROR: pi-gen completed but no .img.xz in ${WORKDIR}/deploy/" >&2
        exit 1
    fi
    mkdir -p "$OUTPUT_DIR"
    cp "$BUILT_IMG" "${OUTPUT_DIR}/${OUTPUT_NAME}"
fi

say "Done."
say "  Image: ${OUTPUT_DIR}/${OUTPUT_NAME}"
say "  Next: bash scripts/flash-sd.sh ${OUTPUT_DIR}/${OUTPUT_NAME} <device>"
