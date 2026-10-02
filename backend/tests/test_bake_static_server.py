"""bake.py's localhost static server must survive the parity harness's
burst of parallel fetches (~58 resources at once).

Regression (QA root-cause 2026-10-02, qa/reports/2026-10-02/
test-bake-socket-not-connected-root-cause.md): it was a single-threaded
socketserver.TCPServer with the default listen backlog of 5, so the
overflow connections were reset (net::ERR_SOCKET_NOT_CONNECTED). On a
slow filesystem a dropped JS module aborted the module graph and the
bake timed out.
"""

from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
N_PARALLEL = 60  # > the harness's ~58 parallel fetches


def _load_bake():
    spec = importlib.util.spec_from_file_location("bake_under_test", REPO / "scripts" / "bake.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # playwright is imported lazily, not here
    return mod


def test_static_server_serves_a_burst_of_parallel_connections(tmp_path: Path) -> None:
    """Open N connections BEFORE sending any request, the way a browser
    opens a burst of sockets. A single-threaded server blocks reading
    the first one, so everything past the listen backlog is reset or
    stalls. Every connection must be accepted and served."""
    (tmp_path / "f.txt").write_text("ok")
    bake = _load_bake()
    server, port = bake._start_static_server(tmp_path)
    socks: list[socket.socket] = []
    failures = 0
    try:
        for _ in range(N_PARALLEL):
            s = socket.socket()
            s.settimeout(3.0)
            try:
                s.connect(("127.0.0.1", port))
                socks.append(s)
            except OSError:
                failures += 1
                s.close()
        for s in socks:
            try:
                s.sendall(b"GET /f.txt HTTP/1.0\r\nHost: x\r\n\r\n")
                resp = s.recv(64)
                if not resp.startswith(b"HTTP/1.0 200"):
                    failures += 1
            except OSError:
                failures += 1
    finally:
        for s in socks:
            s.close()
        server.shutdown()
        server.server_close()
    assert failures == 0, f"{failures}/{N_PARALLEL} parallel requests failed"
