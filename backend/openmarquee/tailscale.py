"""Tailscale URL-auth wrapper (qarl 2026-05-12, arc 4 of 4).

Replaces the prior `tailscale up --auth-key=<tskey>` flow where the
operator had to obtain a key from the admin console + paste it into
settings. New flow: operator clicks Enable Tailscale → backend spawns
`tailscale up --hostname=<device_id>` with NO auth-key → captures the
auth URL Tailscale prints to stderr → surfaces it to the UI →
operator opens the URL in their browser, signs into Tailscale → the
URL becomes a no-op + tailscale up returns. We poll `tailscale
status` until the device transitions to authenticated.

Once authenticated, the hostname becomes the magic-DNS name on the
operator's tailnet. Anchored to MySignXXX (device_id), not
sign_name, so renaming the display label doesn't churn DNS.

Mockability: the binary path comes from $OPENMARQUEE_TAILSCALE_BIN
(default `/usr/bin/tailscale`). Tests point it at a python stub
that emulates the real CLI's stdout/stderr shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Literal

log = logging.getLogger("openmarquee.tailscale")

DEFAULT_BIN = "/usr/bin/tailscale"

# tailscale up prints the auth URL to stderr on a line like:
#   "To authenticate, visit:\n  https://login.tailscale.com/a/abc123def"
# or sometimes inline:
#   "Browser:\nhttps://login.tailscale.com/a/abc123def"
# Match the URL itself; tolerate either form.
_AUTH_URL_RE = re.compile(r"https?://login\.tailscale\.com/[A-Za-z0-9/_\-]+")

State = Literal["disabled", "pending", "authenticated", "error"]


def _bin() -> str:
    return os.environ.get("OPENMARQUEE_TAILSCALE_BIN", DEFAULT_BIN)


async def start_up(device_id: str | None) -> dict:
    """Spawn `tailscale up --hostname=<device_id>` (no auth-key) and
    return {state, auth_url, message}.

    Returns immediately on the auth URL — does NOT wait for the
    operator to complete sign-in. The caller polls `read_status()`
    until state == "authenticated".

    If device_id is None (off-device dev), --hostname is omitted and
    Tailscale uses whatever /etc/hostname holds.
    """
    cmd = [_bin(), "up"]
    if device_id:
        cmd.extend(["--hostname", device_id])
    log.info("tailscale up: %s", cmd)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {
            "state": "error",
            "auth_url": None,
            "message": f"tailscale binary not found at {_bin()}",
        }

    # `tailscale up` (without --auth-key) prints the URL to stderr,
    # then BLOCKS waiting for the user to sign in. We can't await the
    # process exit -- we need to read stderr until the URL appears,
    # surface it, and leave the process running (it exits on its own
    # once the operator completes sign-in; status polling will catch
    # the transition).
    stderr_chunks: list[bytes] = []
    auth_url: str | None = None
    try:
        # Read stderr line-by-line with a short overall timeout.
        # Tailscale's URL line appears within the first ~5 seconds.
        async def _read_until_url() -> str | None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return None
                stderr_chunks.append(line)
                text = line.decode("utf-8", errors="replace")
                m = _AUTH_URL_RE.search(text)
                if m:
                    return m.group(0)

        auth_url = await asyncio.wait_for(_read_until_url(), timeout=10.0)
    except TimeoutError:
        # URL didn't appear within 10s -- something's off. Kill the
        # process so we don't leak a hung tailscale invocation.
        proc.kill()
        await proc.wait()
        joined = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return {
            "state": "error",
            "auth_url": None,
            "message": f"timed out waiting for auth URL; stderr={joined!r}",
        }

    # Detach from the process. It keeps running in the background
    # waiting for the operator to complete sign-in; the kernel reaps
    # it on completion. Status polling tells us when that happens.
    if auth_url:
        return {
            "state": "pending",
            "auth_url": auth_url,
            "message": "opened tailscale up; awaiting operator sign-in",
        }
    # EOF without URL -- tailscale up exited unexpectedly.
    joined = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return {
        "state": "error",
        "auth_url": None,
        "message": f"tailscale up exited without printing an auth URL; stderr={joined!r}",
    }


async def read_status() -> dict:
    """Run `tailscale status --json` and return a thin summary:
      {state, hostname, ipv4}

    state mapping:
      - JSON .BackendState == "Running"     → "authenticated"
      - JSON .BackendState == "NeedsLogin"  → "pending"
      - JSON .BackendState == "Stopped" / "NoState" → "disabled"
      - parse failure / binary missing → "error"
    """
    cmd = [_bin(), "status", "--json"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {
            "state": "error",
            "hostname": None,
            "ipv4": None,
            "message": f"tailscale binary not found at {_bin()}",
        }
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "state": "error",
            "hostname": None,
            "ipv4": None,
            "message": "tailscale status timed out",
        }
    try:
        blob = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return {
            "state": "error",
            "hostname": None,
            "ipv4": None,
            "message": f"tailscale status JSON parse failed: {exc}",
        }
    backend = blob.get("BackendState")
    self_node = blob.get("Self") or {}
    hostname = self_node.get("HostName")
    # TailscaleIPs is a list; first entry is the IPv4 if present.
    ips = self_node.get("TailscaleIPs") or []
    ipv4 = ips[0] if ips else None
    state: State
    if backend == "Running":
        state = "authenticated"
    elif backend == "NeedsLogin":
        state = "pending"
    elif backend in ("Stopped", "NoState", None):
        state = "disabled"
    else:
        state = "pending"
    return {
        "state": state,
        "hostname": hostname,
        "ipv4": ipv4,
        "message": f"BackendState={backend}",
    }
