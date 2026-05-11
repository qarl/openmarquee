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
#   - ~5GB free disk space for the pi-gen workdir.
#   - Internet access for the apt fetch during pi-gen's stages.

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

# --- 3. Copy our config + custom stage into pi-gen workdir ------------------

say "Stage pi-gen.config -> ${WORKDIR}/config"
run cp "${IMAGE_RECIPE_DIR}/pi-gen.config" "${WORKDIR}/config"

say "Stage stage-openmarquee/ -> ${WORKDIR}/stage-openmarquee/"
run rm -rf "${WORKDIR}/stage-openmarquee"
run cp -r "${IMAGE_RECIPE_DIR}/stage-openmarquee" "${WORKDIR}/stage-openmarquee"

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
