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

from openmarquee.netctl_client import netctl_send


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
