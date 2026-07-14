"""Canonical client for the socket-activated root netctl daemon
(``system/openmarquee-netctl-daemon`` + ``openmarquee-netctl.socket``).

The backend runs under ``NoNewPrivileges=true`` + ``ProtectSystem=strict``
so it cannot ``sudo`` nor touch ``/etc`` / systemctl directly (QA verified
NNP blocks sudo entirely, P1.2-B.2). Privileged operations cross the
boundary by speaking a one-round-trip protocol over a Unix socket to the
daemon, which runs as root and delegates to the trusted bash helper.

Protocol (one round-trip per connection):
    Client → Server: ``<subcommand>\\n`` then optional ``<payload>`` bytes,
                      then SHUT_WR.
    Server → Client: ``OK\\n`` on success, or ``ERR <message>\\n`` on failure.

This module is the canonical, public home for that client. The recovery
reboot crossing (``system_control.reboot_device``) uses it. Three older
per-module copies predate this file
(``network_supervisor_takeover._run_netctl`` [async],
``network_supervisor_actuator._netctl_send`` [sync], and the actuator
wrappers); they should migrate here over time, but are intentionally left
untouched for now to avoid churning working, well-tested code.
"""

from __future__ import annotations

import contextlib
import socket

DEFAULT_SOCKET_PATH = "/run/openmarquee/netctl.sock"
DEFAULT_TIMEOUT_S = 15.0
# The daemon caps its own response; we cap the read too so a chatty /
# misbehaving daemon can't stream unbounded bytes into the backend.
_MAX_RESPONSE_BYTES = 8192


def netctl_send(
    subcommand: str,
    payload: bytes = b"",
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    error_cls: type[Exception] = RuntimeError,
    socket_path: str = DEFAULT_SOCKET_PATH,
) -> None:
    """Invoke one privileged ``subcommand`` via the netctl daemon.

    Blocking (synchronous). Callers on the asyncio event loop should
    wrap this in ``asyncio.to_thread(...)`` so the socket round-trip
    doesn't stall the loop.

    Raises ``error_cls`` on connect failure, timeout, or any non-``OK``
    response. ``error_cls`` is parametric so each caller can raise its
    own typed exception; it defaults to ``RuntimeError``.
    """
    # Everything that can fail funnels through `error_cls` so a caller's
    # single `except error_cls` catches ALL failure modes — including fd
    # exhaustion at socket() and a non-ASCII subcommand at encode() —
    # rather than letting a raw OSError / UnicodeError escape as a 500.
    sock: socket.socket | None = None
    try:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
        except OSError as e:
            raise error_cls(f"netctl {subcommand}: socket create failed ({e})") from e
        try:
            request_line = subcommand.encode("ascii") + b"\n"
        except UnicodeError as e:
            raise error_cls(f"netctl {subcommand!r}: subcommand not ASCII ({e})") from e
        try:
            sock.connect(socket_path)
        except FileNotFoundError as e:
            # Dev hosts (macOS / CI) have no daemon socket. Surface a
            # clear, typed error the caller can fail-soft on.
            raise error_cls(f"netctl socket not found at {socket_path}: {e}") from e
        except OSError as e:
            raise error_cls(f"netctl socket connect failed: {e}") from e
        try:
            sock.sendall(request_line)
            if payload:
                sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)
        except OSError as e:
            raise error_cls(f"netctl {subcommand}: send failed ({e})") from e
        chunks: list[bytes] = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(c) for c in chunks) > _MAX_RESPONSE_BYTES:
                    break
        except TimeoutError as e:
            raise error_cls(
                f"netctl {subcommand}: response timed out after {timeout_s:.0f}s"
            ) from e
        except OSError as e:
            raise error_cls(f"netctl {subcommand}: recv failed ({e})") from e
        response = b"".join(chunks).decode("utf-8", errors="replace").rstrip("\n")
        if response == "OK":
            return
        if response.startswith("ERR "):
            raise error_cls(f"netctl {subcommand}: {response[4:]}")
        if not response:
            raise error_cls(f"netctl {subcommand}: empty response (daemon closed before reply?)")
        raise error_cls(f"netctl {subcommand}: unexpected response {response!r}")
    finally:
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()


def netctl_recv_data(
    subcommand: str,
    payload: bytes = b"",
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    error_cls: type[Exception] = RuntimeError,
    socket_path: str = DEFAULT_SOCKET_PATH,
    max_data_bytes: int = 4096,
) -> bytes:
    """Invoke a DATA-RETURNING privileged ``subcommand`` and return its
    payload bytes (Option D, 2026-07-14).

    Unlike ``netctl_send`` (which asserts a bare ``OK`` and returns
    None), the daemon replies to a data subcommand with ``OK\\n``
    followed by opaque data bytes. Only subcommands in the daemon's
    ``DATA_SUBCOMMANDS`` set return data — the sole current member is the
    read-only ``nm-connection-reveal-secret`` PSK read.

    The parse contract is deliberately TIGHT (this is a privilege
    boundary): the STATUS is exactly the first line — we split on the
    FIRST ``\\n`` only, and everything after it is returned verbatim as
    OPAQUE bytes, NEVER re-interpreted as a command. An ``ERR <msg>``
    first line raises ``error_cls``; a missing/other first line is a
    malformed response and also raises. The read is bounded
    (``max_data_bytes`` plus the short status line) so a runaway daemon
    can't stream unbounded bytes into the backend.

    Blocking; asyncio callers should wrap in ``asyncio.to_thread(...)``.
    NEVER log the returned bytes — they may be a secret.
    """
    sock: socket.socket | None = None
    try:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
        except OSError as e:
            raise error_cls(f"netctl {subcommand}: socket create failed ({e})") from e
        try:
            request_line = subcommand.encode("ascii") + b"\n"
        except UnicodeError as e:
            raise error_cls(f"netctl {subcommand!r}: subcommand not ASCII ({e})") from e
        try:
            sock.connect(socket_path)
        except FileNotFoundError as e:
            raise error_cls(f"netctl socket not found at {socket_path}: {e}") from e
        except OSError as e:
            raise error_cls(f"netctl socket connect failed: {e}") from e
        try:
            sock.sendall(request_line)
            if payload:
                sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)
        except OSError as e:
            raise error_cls(f"netctl {subcommand}: send failed ({e})") from e
        cap = max_data_bytes + 64  # room for the "OK\n" / "ERR ..." status line
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > cap:
                    break
        except TimeoutError as e:
            raise error_cls(
                f"netctl {subcommand}: response timed out after {timeout_s:.0f}s"
            ) from e
        except OSError as e:
            raise error_cls(f"netctl {subcommand}: recv failed ({e})") from e
        raw = b"".join(chunks)
        # Tight parse: status is EXACTLY the first line; data is the rest.
        header, sep, data = raw.partition(b"\n")
        if header == b"OK" and sep:
            return data[:max_data_bytes]
        header_text = header.decode("utf-8", errors="replace")
        if header_text.startswith("ERR "):
            raise error_cls(f"netctl {subcommand}: {header_text[4:]}")
        if not raw:
            raise error_cls(f"netctl {subcommand}: empty response (daemon closed before reply?)")
        raise error_cls(f"netctl {subcommand}: malformed response {header_text!r}")
    finally:
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()
