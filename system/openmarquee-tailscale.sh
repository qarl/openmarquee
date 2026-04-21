#!/usr/bin/env bash
# Bring up the Tailscale daemon on the operator's tailnet, reading values
# from openMarquee's settings.json. Runs on boot via
# `openmarquee-tailscale.service` after the backend is up (needed so the
# settings file exists and is parseable).
#
# Designed to be *idempotent*: if the node is already registered, this
# is a no-op. If the auth key was consumed on a prior boot, subsequent
# boots just `tailscale up` without re-auth.
#
# Install:
#     /opt/openmarquee/system/openmarquee-tailscale.sh

set -euo pipefail

SETTINGS_PATH="${OPENMARQUEE_SETTINGS_PATH:-/var/openmarquee/settings.json}"

if [ ! -f "$SETTINGS_PATH" ]; then
    echo "tailscale: settings.json not found at $SETTINGS_PATH — skipping"
    exit 0
fi

# jq would be cleaner but we don't want to require a new package on the
# SD image. Python is already present for the backend.
read -r ENABLED HOSTNAME AUTH_KEY <<<"$(python3 - <<PY
import json
with open("$SETTINGS_PATH") as f:
    s = json.load(f)
print(
    "1" if s.get("tailscale_enabled") else "0",
    s.get("tailscale_hostname") or "-",
    s.get("tailscale_auth_key") or "-",
)
PY
)"

if [ "$ENABLED" != "1" ]; then
    echo "tailscale: disabled in settings — leaving alone"
    # If we're disabled but tailscaled was running from a previous boot,
    # bring it down so the node doesn't linger on the tailnet.
    tailscale logout || true
    exit 0
fi

if ! command -v tailscale >/dev/null 2>&1; then
    echo "tailscale: binary not installed — enable with apt install tailscale" >&2
    exit 1
fi

ARGS=(--accept-routes)
if [ "$HOSTNAME" != "-" ]; then
    ARGS+=(--hostname="$HOSTNAME")
fi
if [ "$AUTH_KEY" != "-" ]; then
    ARGS+=(--authkey="$AUTH_KEY")
fi

echo "tailscale: bringing up (${ARGS[*]/--authkey=*/--authkey=***})"
tailscale up "${ARGS[@]}"

# Best-effort: once the node is authenticated, clear the auth key from
# settings.json so a later leak of the file can't re-auth. The backend
# API's PUT validator accepts an empty string → None.
if [ "$AUTH_KEY" != "-" ]; then
    python3 - <<PY || true
import json, pathlib
p = pathlib.Path("$SETTINGS_PATH")
s = json.loads(p.read_text())
s["tailscale_auth_key"] = None
tmp = p.with_name(p.name + ".tmp")
tmp.write_text(json.dumps(s, indent=2))
tmp.replace(p)
PY
fi
