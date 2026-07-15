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


def _run(mod, request: bytes, *, helper_stdout=b"", helper_rc=0, monkeypatch):
    monkeypatch.setattr(mod.sys, "stdin", _FakeStdin(request))
    out = _FakeStdout()
    monkeypatch.setattr(mod.sys, "stdout", out)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=helper_rc, stdout=helper_stdout),
    )
    rc = mod.main()
    return rc, out.buffer.getvalue()


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
    assert rc == 1
    assert resp.startswith(b"ERR unknown subcommand")
    assert b"leak" not in resp
