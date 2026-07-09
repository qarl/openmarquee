"""Privileged device-lifecycle crossings: reboot (A4) and factory-reset (A3).

Spec §"Settings" names ``/api/system/{restart,factory-reset}`` as future
operator surfaces; §"Recovery" (qarl handover 2026-07-08) approved building
them end-to-end. The backend can't reboot the box itself (NoNewPrivileges
+ ProtectSystem=strict), so the privileged bits are issued by the root
netctl daemon via ``netctl_client.netctl_send`` — the same privilege
boundary the network take-over path uses.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from openmarquee.netctl_client import netctl_send

log = logging.getLogger(__name__)

# `systemctl reboot` enqueues the reboot transaction and returns quickly,
# but the daemon still shells out through the helper; give it comparable
# headroom to the other netctl calls.
REBOOT_TIMEOUT_S = 15.0
# Factory reset's daemon step deletes NM wifi profiles (a few nmcli
# invocations) before rebooting, so allow more headroom than a bare reboot.
FACTORY_RESET_TIMEOUT_S = 30.0


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


def _remove_file_quiet(path: Path) -> None:
    """Best-effort unlink. A missing file is success (already gone); any
    other OSError is logged but NON-fatal — a factory reset should wipe
    as much as it can, not abort on one stubborn file."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        log.warning("factory-reset: could not remove %s: %s", path, e)


def _wipe_dir_contents(root: Path) -> None:
    """Remove every child of `root` (item dirs + stray files) but keep
    `root` itself, so the storage layer can recreate items cleanly after
    the reboot. Best-effort per child."""
    if not root.exists():
        return
    for child in root.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError as e:
            log.warning("factory-reset: could not remove %s: %s", child, e)


def factory_reset_device(
    *,
    data_files: list[Path],
    content_root: Path | None,
    timeout_s: float = FACTORY_RESET_TIMEOUT_S,
) -> None:
    """Recovery A3 (DESTRUCTIVE): wipe operator data, tear down wifi, and
    reboot into a fresh setup state.

    Device IDENTITY is preserved (hostname + AP SSID/passphrase stay), so
    the physical label / QR the operator has remains valid.

    Order matters: wipe the backend-owned data FIRST (settings, playlists,
    schedule, flock, network-state, uploaded content). Only then hand off
    to the root netctl daemon's ``factory-reset`` subcommand, which deletes
    the saved NM wifi profiles + removes the take-over wpa/NM configs and
    reboots. Doing the (recoverable) data wipe before the (irreversible)
    reboot means a failure in the wipe aborts before the point of no
    return; and if the daemon step fails, the data is already gone so a
    later manual reboot still lands in the fresh setup state.

    Blocking; call from a worker thread. Raises ``SystemControlError`` if
    the daemon crossing fails (the data wipe never raises — it's
    best-effort per path).
    """
    log.warning("system-control: FACTORY RESET requested; wiping operator data")
    for path in data_files:
        _remove_file_quiet(path)
    if content_root is not None:
        _wipe_dir_contents(content_root)
    log.warning(
        "system-control: operator data wiped; handing off to netctl for wifi teardown + reboot"
    )
    netctl_send("factory-reset", b"", timeout_s=timeout_s, error_cls=SystemControlError)
