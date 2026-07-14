"""Protocol tests for the canonical netctl daemon client
(`openmarquee.netctl_client.netctl_send`).

Exercises the real one-round-trip protocol against a throwaway
AF_UNIX server bound in-process: subcommand line + payload framing,
SHUT_WR, and OK / ERR / empty-response parsing. Deterministic — the
server thread echoes a scripted response and closes; no timing.
"""

from __future__ import annotations

import pathlib
import shutil
import socket
import tempfile
import threading

import pytest

from openmarquee.netctl_client import netctl_recv_data, netctl_send


@pytest.fixture
def sockdir():
    """A SHORT-path temp dir. AF_UNIX socket paths are capped at
    ~104 bytes; pytest's tmp_path (under /private/var/folders/... on
    macOS) blows past that, so bind sockets under a short /tmp dir."""
    d = tempfile.mkdtemp(prefix="nc", dir="/tmp")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _serve_once(sock_path: str, response: bytes, capture: dict) -> threading.Thread:
    """Bind an AF_UNIX server that accepts ONE connection, reads the
    full client request (until EOF via the client's SHUT_WR), stores it
    in `capture["request"]`, writes `response`, and closes. Returns the
    started thread."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)

    def run() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                chunks: list[bytes] = []
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                capture["request"] = b"".join(chunks)
                conn.sendall(response)
        finally:
            server.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_ok_response_returns_and_frames_subcommand_and_payload(sockdir):
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    t = _serve_once(sock_path, b"OK\n", capture)

    netctl_send(
        "hostapd-write-and-restart",
        b"channel=6\n",
        timeout_s=5.0,
        socket_path=sock_path,
    )
    t.join(timeout=5.0)

    # The daemon protocol is: subcommand line, then payload bytes.
    assert capture["request"] == b"hostapd-write-and-restart\nchannel=6\n"


def test_no_payload_sends_bare_subcommand_line(sockdir):
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    t = _serve_once(sock_path, b"OK\n", capture)

    netctl_send("reboot", b"", timeout_s=5.0, socket_path=sock_path)
    t.join(timeout=5.0)

    assert capture["request"] == b"reboot\n"


def test_err_response_raises_error_cls_with_message(sockdir):
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    _serve_once(sock_path, b"ERR helper rc=1: boom\n", capture)

    class MyError(RuntimeError):
        pass

    with pytest.raises(MyError, match="helper rc=1: boom"):
        netctl_send("reboot", b"", timeout_s=5.0, error_cls=MyError, socket_path=sock_path)


def test_empty_response_raises(sockdir):
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    _serve_once(sock_path, b"", capture)

    with pytest.raises(RuntimeError, match="empty response"):
        netctl_send("reboot", b"", timeout_s=5.0, socket_path=sock_path)


def test_unexpected_response_raises(sockdir):
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    _serve_once(sock_path, b"MAYBE\n", capture)

    with pytest.raises(RuntimeError, match="unexpected response"):
        netctl_send("reboot", b"", timeout_s=5.0, socket_path=sock_path)


def test_missing_socket_raises_error_cls(sockdir):
    # No server bound at this path -> connect() FileNotFoundError.
    missing = str(pathlib.Path(sockdir) / "nope.sock")

    class MyError(RuntimeError):
        pass

    with pytest.raises(MyError, match="socket not found"):
        netctl_send("reboot", b"", timeout_s=5.0, error_cls=MyError, socket_path=missing)


# ---------------------------------------------------------------------------
# netctl_recv_data (Option D, 2026-07-14): the data-returning variant.
# Response is "OK\n" + opaque data bytes; tight first-line parse boundary.
# ---------------------------------------------------------------------------


def test_recv_data_ok_returns_data_and_frames_request(sockdir):
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    t = _serve_once(sock_path, b"OK\nhunter2hunter\n", capture)

    data = netctl_recv_data(
        "nm-connection-reveal-secret",
        b"qarl\n",
        timeout_s=5.0,
        socket_path=sock_path,
    )
    t.join(timeout=5.0)

    # Request framing: subcommand line then the connection-name payload.
    assert capture["request"] == b"nm-connection-reveal-secret\nqarl\n"
    # Data is everything after the first newline, verbatim (incl. nmcli's
    # trailing newline — the wrapper strips it, not the transport).
    assert data == b"hunter2hunter\n"


def test_recv_data_ok_with_no_data_returns_empty(sockdir):
    # Open / secret-less profile: daemon replies "OK\n" with no data.
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    _serve_once(sock_path, b"OK\n", capture)

    data = netctl_recv_data(
        "nm-connection-reveal-secret", b"open\n", timeout_s=5.0, socket_path=sock_path
    )
    assert data == b""


def test_recv_data_err_raises_error_cls(sockdir):
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    _serve_once(sock_path, b"ERR helper rc=10: no such connection\n", capture)

    class MyError(RuntimeError):
        pass

    with pytest.raises(MyError, match="no such connection"):
        netctl_recv_data(
            "nm-connection-reveal-secret",
            b"nope\n",
            timeout_s=5.0,
            error_cls=MyError,
            socket_path=sock_path,
        )


def test_recv_data_returns_opaque_bytes_verbatim(sockdir):
    # Tight-parse guarantee: ONLY the first newline is the status
    # boundary. Everything after it is returned verbatim — even data that
    # itself contains newlines and text that looks like a status line
    # ("ERR ..."/"OK") is never re-interpreted as a command/status.
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    weird = b"p@ss ERR not-a-status\nOK\n"
    _serve_once(sock_path, b"OK\n" + weird, capture)

    data = netctl_recv_data(
        "nm-connection-reveal-secret", b"x\n", timeout_s=5.0, socket_path=sock_path
    )
    assert data == weird


def test_recv_data_malformed_no_newline_raises(sockdir):
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    _serve_once(sock_path, b"GARBAGE-NO-NEWLINE", capture)

    with pytest.raises(RuntimeError, match="malformed response"):
        netctl_recv_data(
            "nm-connection-reveal-secret", b"x\n", timeout_s=5.0, socket_path=sock_path
        )


def test_recv_data_empty_response_raises(sockdir):
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    _serve_once(sock_path, b"", capture)

    with pytest.raises(RuntimeError, match="empty response"):
        netctl_recv_data(
            "nm-connection-reveal-secret", b"x\n", timeout_s=5.0, socket_path=sock_path
        )


def test_recv_data_caps_oversize_data(sockdir):
    # A runaway daemon can't stream unbounded bytes: the return is capped
    # at max_data_bytes.
    sock_path = str(pathlib.Path(sockdir) / "s.sock")
    capture: dict = {}
    # 300B fits the socket send buffer so the server's sendall completes
    # (no BrokenPipe when the client caps its read + closes early); still
    # well over the 100B cap under test.
    _serve_once(sock_path, b"OK\n" + b"A" * 300, capture)

    data = netctl_recv_data(
        "nm-connection-reveal-secret",
        b"x\n",
        timeout_s=5.0,
        socket_path=sock_path,
        max_data_bytes=100,
    )
    assert data == b"A" * 100
