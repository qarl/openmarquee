"""Privileged device-lifecycle crossings: reboot (A4) and, later,
factory-reset (A3).

Spec §"Settings" names ``/api/system/{restart,factory-reset}`` as future
operator surfaces; §"Recovery" (qarl handover 2026-07-08) approved building
them end-to-end. The backend can't reboot the box itself (NoNewPrivileges
+ ProtectSystem=strict), so the reboot is issued by the root netctl daemon
via ``netctl_client.netctl_send`` — the same privilege boundary the network
take-over path uses.
"""

from __future__ import annotations

import logging

from openmarquee.netctl_client import netctl_send

log = logging.getLogger(__name__)

# `systemctl reboot` enqueues the reboot transaction and returns quickly,
# but the daemon still shells out through the helper; give it comparable
# headroom to the other netctl calls.
REBOOT_TIMEOUT_S = 15.0


class SystemControlError(RuntimeError):
    """Raised when a privileged system-control crossing fails (socket
    absent on a dev host, daemon error, timeout). The API layer maps
    this to a 503 so the operator sees a clear failure rather than a
    silent no-op."""


def reboot_device(*, timeout_s: float = REBOOT_TIMEOUT_S) -> None:
    """Reboot the device via the root netctl daemon (``reboot``
    subcommand → ``systemctl reboot``).

    Blocking; call from a worker thread on the event loop. Returns once
    the daemon has ACKed that the reboot was enqueued — the actual
    teardown (which SIGTERMs this process) proceeds asynchronously
    afterward, leaving the HTTP response time to flush.

    Raises ``SystemControlError`` on any failure.
    """
    log.warning("system-control: reboot requested; issuing via netctl daemon")
    netctl_send("reboot", b"", timeout_s=timeout_s, error_cls=SystemControlError)
