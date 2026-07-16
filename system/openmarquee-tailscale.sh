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
read -r ENABLED HOSTNAME AUTH_KEY HTTPS_ENABLED <<<"$(python3 - <<PY
import json
with open("$SETTINGS_PATH") as f:
    s = json.load(f)
print(
    "1" if s.get("tailscale_enabled") else "0",
    s.get("tailscale_hostname") or "-",
    s.get("tailscale_auth_key") or "-",
    # Default True when the field is absent (legacy settings.json from
    # before HTTPS landed). Matches SystemSettings.tailscale_https_
    # enabled's Pydantic default — keeps boot behavior consistent
    # whether the file's been re-saved or not.
    "1" if s.get("tailscale_https_enabled", True) else "0",
)
PY
)"

if [ "$ENABLED" != "1" ]; then
    echo "tailscale: disabled in settings — leaving the node alone"
    # 2026-07-16 (qarl): this used to `tailscale logout` here, to stop a
    # node lingering on the tailnet after being disabled. That is a very
    # sharp knife pointed at the one lane we use to reach a sign we can't
    # physically touch, and it fires on the say-so of a JSON field that
    # has been wrong in three separate ways this week:
    #   * until /api/system/tailscale/up started persisting it, the ONLY
    #     writer was the Settings checkbox echoing its own value back on
    #     save — so a sign whose operator clicked Enable but never ticked
    #     and saved that box read False here and got logged out on its
    #     first reboot. (Existing signs hold True precisely because someone
    #     did tick it once, long ago.)
    #   * the Settings checkbox is disabled until the station radio is on,
    #     and that radio was itself wrong on an NM-provisioned sign, so
    #     the field could not be set to True through the UI at all;
    #   * a stale browser tab autosaving an unticked box would flip it
    #     back to False under a working node.
    # "The field said false" is exactly how a sign ends up unreachable,
    # so we no longer act destructively on it. Disabling is now the
    # operator's explicit act via `tailscale down` / the Tailscale admin
    # console — the authority that actually owns that decision — rather
    # than a boot script inferring it from settings.json.
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

# HTTPS Phase 1: tell tailscaled to serve our HTTP backend over TLS on
# port 443 (terminates on the tailscale-interface listener only, NOT on
# the AP-side or LAN-side wlan0 surfaces). Provisions a Let's Encrypt
# cert for the node's FQDN (`<hostname>.<tailnet>.ts.net`) and renews
# it automatically. Short MagicDNS names + LAN IPs continue to work on
# plain HTTP — the FastAPI middleware in
# `openmarquee.fqdn_redirect_middleware` 301-redirects non-FQDN /
# non-captive / non-LAN requests to the canonical HTTPS URL so operator
# bookmarks land on a secure-context page (Chrome's getUserMedia gate).
#
# `tailscale serve --bg` is idempotent; re-running with the same args
# is a no-op. Persisted to /var/lib/tailscale/serve.json so the listener
# survives reboots without an explicit systemd supervision step.
#
# `|| echo` (not `|| exit 1`): HTTPS provisioning failure must NOT
# block the AP-side captive-portal flow. The Let's Encrypt issuance
# step requires the admin-console "HTTPS Certificates" toggle to be
# flipped per-tailnet; until then, `tailscale serve` errors and we
# fall through. Operators see the camera-banner workaround text as
# the runtime signal that HTTPS isn't live yet.
if [ "$HTTPS_ENABLED" = "1" ]; then
    echo "tailscale: provisioning HTTPS via 'tailscale serve --https=443'"
    tailscale serve --bg --https=443 http://localhost:80 \
        || echo "tailscale serve: failed (HTTPS unavailable on tailnet)" >&2
fi

# Best-effort: once the node is authenticated, clear the auth key from
# settings.json so a later leak of the file can't re-auth. The backend
# API's PUT validator accepts an empty string → None.
#
# 19.1 / sweep #10 #2: delegate to openmarquee._atomic.atomic_write_
# text instead of the inline tmp.write_text+replace dance. The shared
# helper sets 0600 + cleans up orphan .tmp on failure (Batch 11.2's
# discipline applied here too -- settings.json carries the AP
# password, station password, and the auth key we're about to
# clear).
if [ "$AUTH_KEY" != "-" ]; then
    python3 - <<PY || true
import json, pathlib, sys
sys.path.insert(0, "/opt/openmarquee/backend")
from openmarquee._atomic import atomic_write_text
p = pathlib.Path("$SETTINGS_PATH")
s = json.loads(p.read_text())
s["tailscale_auth_key"] = None
atomic_write_text(p, json.dumps(s, indent=2))
PY
fi
