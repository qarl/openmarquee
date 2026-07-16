"""Protocol tests for the netctl daemon's Option-D data-forwarding
extension (``system/openmarquee-netctl-daemon``).

The daemon replies ``OK\\n`` for ordinary subcommands (helper stdout
DISCARDED) and ``OK\\n`` + stdout only for the explicit
``DATA_SUBCOMMANDS`` set — so a chatty or compromised helper can never
leak stdout for a command that isn't meant to return data. The daemon is
a ``system/`` script (not an importable package), so it's loaded by path
with ``sys.stdin`` / ``sys.stdout`` / ``subprocess.run`` stubbed.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import logging
import pathlib

_DAEMON_PATH = pathlib.Path(__file__).resolve().parents[2] / "system" / "openmarquee-netctl-daemon"


def _load_daemon():
    # The daemon is an extensionless system/ script, so importlib can't
    # infer a loader from the suffix — use an explicit SourceFileLoader.
    loader = importlib.machinery.SourceFileLoader("netctl_daemon_under_test", str(_DAEMON_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _FakeStdout:
    """Captures both text writes (``_respond``) and binary ``.buffer``
    writes (forwarded data) into one ordered buffer, like a real
    stdout."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, s: str) -> int:
        return self.buffer.write(s.encode("utf-8"))

    def flush(self) -> None:
        pass


class _FakeStdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run(mod, request: bytes, *, helper_stdout=b"", helper_rc=0, helper_exc=None, monkeypatch):
    monkeypatch.setattr(mod.sys, "stdin", _FakeStdin(request))
    out = _FakeStdout()
    monkeypatch.setattr(mod.sys, "stdout", out)

    def _fake_run(*a, **k):
        # helper_exc lets a test drive the daemon-INTERNAL failure paths
        # (FileNotFoundError → helper missing; subprocess.TimeoutExpired →
        # helper timeout) — those must exit 1, unlike a handled client error.
        if helper_exc is not None:
            raise helper_exc
        return _FakeCompleted(returncode=helper_rc, stdout=helper_stdout)

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    rc = mod.main()
    return rc, out.buffer.getvalue()


def _capture_warnings(mod, monkeypatch):
    """Capture the daemon's log records. Its logger sets propagate=False
    (system-journal path), so pytest's caplog can't see it — install a spy
    via _setup_journal_logging instead. Returns the (mutable) records list."""
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    spy = logging.getLogger("netctl-daemon-test-spy")
    spy.handlers = [_Capture()]
    spy.setLevel(logging.DEBUG)
    spy.propagate = False
    monkeypatch.setattr(mod, "_setup_journal_logging", lambda: spy)
    return records


def test_reveal_registered_in_allowlist_and_data_subcommands():
    mod = _load_daemon()
    assert "nm-connection-reveal-secret" in mod.ALLOWLIST
    assert "nm-connection-reveal-secret" in mod.DATA_SUBCOMMANDS


def test_data_subcommand_forwards_helper_stdout(monkeypatch):
    mod = _load_daemon()
    rc, resp = _run(
        mod,
        b"nm-connection-reveal-secret\nqarl\n",
        helper_stdout=b"hunter2hunter\n",
        monkeypatch=monkeypatch,
    )
    assert rc == 0
    # "OK\n" status line then the helper's stdout verbatim.
    assert resp == b"OK\nhunter2hunter\n"


def test_non_data_subcommand_discards_helper_stdout(monkeypatch):
    # Security property: a subcommand NOT in DATA_SUBCOMMANDS replies
    # OK-only even when the helper is chatty on stdout — no leak.
    mod = _load_daemon()
    rc, resp = _run(
        mod,
        b"reboot\n",
        helper_stdout=b"SHOULD-NOT-LEAK\n",
        monkeypatch=monkeypatch,
    )
    assert rc == 0
    assert resp == b"OK\n"
    assert b"SHOULD-NOT-LEAK" not in resp


def test_helper_failure_reports_err_not_data(monkeypatch):
    mod = _load_daemon()
    rc, resp = _run(
        mod,
        b"nm-connection-reveal-secret\nnope\n",
        helper_rc=10,
        helper_stdout=b"",
        monkeypatch=monkeypatch,
    )
    assert rc == 1
    assert resp.startswith(b"ERR ")


def test_unknown_subcommand_rejected_before_helper(monkeypatch):
    mod = _load_daemon()
    rc, resp = _run(
        mod,
        b"nm-connection-reveal-secret-EVIL\nx\n",
        helper_stdout=b"leak",
        monkeypatch=monkeypatch,
    )
    # 2026-07-16 exit-semantics: a rejected unknown subcommand is a HANDLED
    # client error, not a daemon failure → exit 0 (keeps the per-connection
    # unit out of `systemctl --failed`). The ERR response + no-leak property
    # still hold.
    assert rc == 0
    assert resp.startswith(b"ERR unknown subcommand")
    assert b"leak" not in resp


def test_handled_client_errors_exit_0_and_warn(monkeypatch):
    """Exit-semantics split (2026-07-16), CLIENT-error half: a HANDLED bad
    request — empty, unknown-subcommand, or oversize — exits 0 so the
    socket-activated per-connection unit doesn't linger in
    `systemctl --failed`, while still sending ERR + logging a warning (the
    audit signal is preserved). Root cause: JasonsSign1's old deployed daemon
    rejected the (now-allowlisted) `avahi-write-and-restart` and left a
    failed unit that never cleared."""
    mod = _load_daemon()

    # empty request
    records = _capture_warnings(mod, monkeypatch)
    rc, resp = _run(mod, b"", monkeypatch=monkeypatch)
    assert rc == 0
    assert resp.startswith(b"ERR empty request")
    assert any(r.levelno == logging.WARNING for r in records), "empty: no warning logged"

    # unknown subcommand (the exact JasonsSign1 failure shape)
    records = _capture_warnings(mod, monkeypatch)
    rc, resp = _run(mod, b"avahi-write-and-restart-BOGUS\n", monkeypatch=monkeypatch)
    assert rc == 0
    assert resp.startswith(b"ERR unknown subcommand")
    assert any(r.levelno == logging.WARNING for r in records), "unknown: no warning logged"

    # oversize payload (a real allowlisted subcommand + payload over the cap)
    records = _capture_warnings(mod, monkeypatch)
    big = b"nm-write-unmanaged-wlan0\n" + b"x" * (mod.MAX_PAYLOAD_BYTES + 1)
    rc, resp = _run(mod, big, monkeypatch=monkeypatch)
    assert rc == 0
    assert resp.startswith(b"ERR payload exceeds")
    assert any(r.levelno == logging.WARNING for r in records), "oversize: no warning logged"


def test_daemon_internal_failures_exit_1(monkeypatch):
    """Exit-semantics split (2026-07-16), DAEMON-INTERNAL half: genuine
    breakage (helper missing / timeout / non-zero rc) MUST exit 1 so
    `systemctl --failed` stays a real "the daemon is broken" signal — the
    posture fix must NOT collapse these into the client-error 0-exit."""
    mod = _load_daemon()

    # helper binary missing
    rc, resp = _run(
        mod, b"nm-reload\n", helper_exc=FileNotFoundError("no helper"), monkeypatch=monkeypatch
    )
    assert rc == 1
    assert resp.startswith(b"ERR helper not found")

    # helper exits non-zero (the privileged op itself failed)
    rc, resp = _run(mod, b"nm-reload\n", helper_rc=7, monkeypatch=monkeypatch)
    assert rc == 1
    assert resp.startswith(b"ERR helper rc=")

    # helper timeout
    rc, resp = _run(
        mod,
        b"nm-reload\n",
        helper_exc=mod.subprocess.TimeoutExpired(cmd="helper", timeout=1),
        monkeypatch=monkeypatch,
    )
    assert rc == 1
    assert resp.startswith(b"ERR helper timed out")
