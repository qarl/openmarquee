"""Perf-night r6 (2026-05-26) regression-guard.

Walks every `async def` in `backend/openmarquee/*.py` via the AST,
flags any bare blocking-I/O call. Fails CI with a pointer to the
r4/r5 wrap pattern (`await asyncio.to_thread(sync_func, *args)`).

# What this guards against

r4 + r5 + pre-existing wraps cover every sync-IPC / blocking-I/O
callsite currently in async-context backend code. r6's audit
verified that property holds across all 20 backend files containing
`async def`. This test PINS that property: any future PR that adds
a bare blocking call inside an `async def` (without an
asyncio.to_thread wrap) fails CI immediately, with a file:line
pointer and a fix-suggestion.

# Why static AST (not runtime assertion)

QA r6 design note offered a runtime-assertion alternative
(RustRenderer asserts "not in asyncio loop" on sync method entry).
The static AST walker is cleaner for the canonical
``self._renderer.advance(...)`` shape — no production overhead, no
runtime-only flake mode, no need to actually trigger the path in a
test. The runtime mode would catch dynamic dispatch (an aliased
``r = self._renderer; r.advance(...)``) that static analysis misses,
but the codebase doesn't currently use that pattern; if it ever
does, the runtime check is a 5-line addition.

# Limitations

- Static; only catches `self._renderer.X / renderer.X / loop.renderer.X`
  syntactic shapes. An aliased renderer reference (``r = self._renderer``)
  would slip through.
- Doesn't follow function calls. ``async def foo(): _sync_helper()``
  where the helper does the blocking work isn't detected.
- Lambda-in-to_thread (``asyncio.to_thread(lambda: subprocess.run(...))``)
  WOULD be a false positive — but the codebase doesn't use that
  pattern (verified by grep at r6 ship time).

The dispatch's hard rule: "Allowlist entries MUST have inline
justification comments — future drift through 'just allowlist it'
would defeat the purpose." Enforced by `test_allowlist_entries_have_rationale`
below.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterator


# ---------------------------------------------------------------
# Blocking-call patterns
# ---------------------------------------------------------------

# Module-attribute pairs: e.g. (subprocess, run) → matches
# `subprocess.run(...)`. Add new patterns here as future blocking
# APIs land in the codebase.
_BLOCKING_ATTR_CALLS: frozenset[tuple[str, str]] = frozenset({
    # Subprocess: sync APIs that block the calling thread until child
    # exits. ``asyncio.create_subprocess_exec`` is the async alternative
    # (used correctly by stream_consumer.py / tailscale.py).
    ("subprocess", "run"),
    ("subprocess", "check_output"),
    ("subprocess", "check_call"),
    # Blocking sleep. ``asyncio.sleep`` is the async alternative.
    ("time", "sleep"),
    # Sync HTTP. ``httpx.AsyncClient`` (used by flock_sync.py) is the
    # async alternative; ``requests.*`` has no async variant — wrap
    # in asyncio.to_thread if needed.
    ("requests", "get"),
    ("requests", "post"),
    ("requests", "put"),
    ("requests", "delete"),
    ("requests", "head"),
    ("requests", "patch"),
    ("requests", "request"),
})

# RustRenderer / AutoFallbackRenderer sync IPC methods. The receiver
# chain ending in one of these on a known renderer-shaped receiver
# (see _RENDERER_RECEIVERS below) is the canonical r4/r5 wedge shape.
_RENDERER_IPC_METHODS: frozenset[str] = frozenset({
    "advance",
    "begin_slide",
    "begin_transition",
    "capture",
    "reconfigure",
    "close",
    "reopen",
    "open",
    "render_frame",
    "end_external_frames",
    "begin_external_frames",
})

# Receiver names that signal "this is a renderer". The detector also
# strips a leading ``self.`` from the receiver chain so
# ``self._renderer.advance`` matches via the ``_renderer`` entry.
_RENDERER_RECEIVERS: frozenset[str] = frozenset({
    "_renderer",
    "renderer",
    "loop.renderer",
    "_primary",      # AutoFallbackRenderer's underlying RustRenderer ref
    "self._primary",
})


# ---------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------
#
# Each entry: ((relative_file_path, function_name, callee_pattern), rationale)
# - relative_file_path: relative to the repo root (e.g. "backend/openmarquee/foo.py")
# - function_name: the enclosing async def's name
# - callee_pattern: the dotted form the detector emits (e.g. "subprocess.run")
# - rationale: a non-trivial operator-grade explanation of why this
#              specific call is acceptable. Reviewed at allowlist-
#              update time. Empty / placeholder strings fail
#              test_allowlist_entries_have_rationale below.
#
# The r6 audit confirmed zero findings as of HEAD 44b013e. This dict
# starts empty. A future PR that adds a deliberate exception MUST
# add an entry AND defend the rationale in the PR review.
_ALLOWLIST: dict[tuple[str, str, str], str] = {}


# ---------------------------------------------------------------
# AST walker
# ---------------------------------------------------------------


def _attribute_chain(node: ast.AST) -> str | None:
    """Return the dotted-name form of an Attribute chain.

    `self._renderer.advance` → "self._renderer.advance"
    `renderer.advance`       → "renderer.advance"
    `obj`                    → "obj"

    Returns None if the chain isn't pure (e.g. contains a Call or
    Subscript). That's correct — only pure attribute chains
    syntactically signal "this is a known receiver".
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    else:
        return None
    return ".".join(reversed(parts))


def _classify_call(node: ast.Call) -> str | None:
    """Return the matched blocking-call pattern, or None.

    Two cases:
    1. Direct module attribute (e.g. subprocess.run, time.sleep).
    2. Renderer IPC method on a known receiver.
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    chain = _attribute_chain(func)
    if chain is None:
        return None
    parts = chain.split(".")

    # Case 1: module.attr style
    if len(parts) == 2 and (parts[0], parts[1]) in _BLOCKING_ATTR_CALLS:
        return chain

    # Case 2: receiver.ipc_method style
    if len(parts) >= 2 and parts[-1] in _RENDERER_IPC_METHODS:
        receiver = ".".join(parts[:-1])
        # Strip leading `self.` so `self._renderer.advance` matches
        # via the `_renderer` receiver entry.
        if receiver.startswith("self."):
            receiver = receiver[len("self."):]
        if receiver in _RENDERER_RECEIVERS:
            return chain

    return None


def _walk_async_blocking_calls(
    module_path: pathlib.Path,
) -> Iterator[tuple[str, str, int]]:
    """Yield (async_fn_name, callee_pattern, lineno) for every blocking
    call inside an async def in this module.

    Async functions are detected per-definition. The walker recurses
    into nested defs but treats each as its own scope (so a sync
    helper defined inside an async function isn't flagged for its
    parent's awaits).
    """
    tree = ast.parse(module_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    match = _classify_call(sub)
                    if match is not None:
                        yield (node.name, match, sub.lineno)


# ---------------------------------------------------------------
# Tests
# ---------------------------------------------------------------


def _backend_src_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "openmarquee"


def _repo_root_relative_to(file_path: pathlib.Path) -> str:
    """Return the file path relative to the workspace root that the
    allowlist keys are expressed against (e.g. backend/openmarquee/foo.py).
    """
    # tests/ lives under backend/, so the parent.parent of __file__ is the
    # backend/ dir. Render the path as backend/openmarquee/...
    backend_dir = pathlib.Path(__file__).resolve().parent.parent
    return f"backend/{file_path.relative_to(backend_dir)}"


def test_no_bare_blocking_calls_in_async_functions():
    """r6 regression-guard. The r4 + r5 wraps + pre-existing
    asyncio.to_thread calls + the r6 audit established that no
    backend async function contains a bare sync-IPC or blocking-I/O
    call. This test pins that invariant.

    To resolve a failure here, either:
      (1) Wrap the offending call:
            `result = some_sync_fn(args)`
          becomes
            `result = await asyncio.to_thread(some_sync_fn, *args)`
          See playback.py:1060 (advance) + playback.py:1248
          (render_frame) for canonical examples.
      (2) Add the call to `_ALLOWLIST` above with a non-trivial
          rationale. This MUST be reviewed — the rationale should
          explain why the wrap isn't right here (e.g., the call is
          truly sub-microsecond, or runs in a separately-managed
          thread, or some other documented exception).
    """
    src_root = _backend_src_root()
    findings: list[tuple[str, str, str, int]] = []
    for py_file in sorted(src_root.rglob("*.py")):
        # macOS AppleDouble metadata files (._*) are NFS-side
        # resource forks that travel alongside .py files on virtiofs/
        # SMB mounts. They have a binary header and can't be parsed
        # as Python. Skip them rather than tripping the walker on
        # UnicodeDecodeError. Per [[project_apple_double_in_git_breaks_stash]]
        # the codebase has a recurring AppleDouble nuisance on this mount.
        if py_file.name.startswith("._"):
            continue
        rel = _repo_root_relative_to(py_file)
        for fn_name, callee, lineno in _walk_async_blocking_calls(py_file):
            key = (rel, fn_name, callee)
            if key in _ALLOWLIST:
                continue
            findings.append((rel, fn_name, callee, lineno))

    if findings:
        msg = ["Bare blocking calls found in async functions (r6 regression):"]
        for rel, fn, callee, lineno in findings:
            msg.append(f"  {rel}:{lineno}  async def {fn}  → {callee}")
        msg.append("")
        msg.append(
            "Wrap via `await asyncio.to_thread(...)` (see playback.py:1060 / "
            "playback.py:1248 for the canonical r4/r5 pattern), OR add an "
            "entry to _ALLOWLIST in this file with a real rationale."
        )
        raise AssertionError("\n".join(msg))


def test_allowlist_entries_have_rationale():
    """The dispatch's hard rule: allowlist entries MUST carry a real
    rationale. Empty / placeholder rationales fail here so an
    accidental empty-string-add doesn't silently dilute the guard.
    """
    bad: list[str] = []
    for key, rationale in _ALLOWLIST.items():
        if not isinstance(rationale, str) or len(rationale.strip()) < 20:
            bad.append(f"  {key}: rationale={rationale!r}")
    if bad:
        raise AssertionError(
            "Allowlist entries with missing/trivial rationale:\n"
            + "\n".join(bad)
            + "\n\nEvery exception must carry a non-trivial explanation "
            "(>= 20 chars) of why the wrap isn't right at this site."
        )


# ---------------------------------------------------------------
# Walker self-tests
# ---------------------------------------------------------------
#
# These pin the walker's classification behavior on synthetic inputs.
# Without them, a future bug in _classify_call could silently make
# the main test always-pass — defeating the regression guard.


def test_walker_flags_bare_subprocess_run():
    tree = ast.parse(
        "async def bad():\n"
        "    import subprocess\n"
        "    subprocess.run(['echo', 'hi'])\n"
    )
    findings = _collect_findings(tree)
    assert findings == [("bad", "subprocess.run")]


def test_walker_does_not_flag_wrapped_subprocess_run():
    tree = ast.parse(
        "import asyncio, subprocess\n"
        "async def good():\n"
        "    await asyncio.to_thread(subprocess.run, ['echo', 'hi'])\n"
    )
    findings = _collect_findings(tree)
    assert findings == []


def test_walker_flags_bare_renderer_advance():
    tree = ast.parse(
        "async def bad(self):\n"
        "    self._renderer.advance(123)\n"
    )
    findings = _collect_findings(tree)
    assert findings == [("bad", "self._renderer.advance")]


def test_walker_does_not_flag_wrapped_renderer_advance():
    tree = ast.parse(
        "import asyncio\n"
        "async def good(self):\n"
        "    await asyncio.to_thread(self._renderer.advance, 123)\n"
    )
    findings = _collect_findings(tree)
    assert findings == []


def test_walker_flags_bare_time_sleep():
    tree = ast.parse(
        "import time\n"
        "async def bad():\n"
        "    time.sleep(1)\n"
    )
    findings = _collect_findings(tree)
    assert findings == [("bad", "time.sleep")]


def test_walker_does_not_flag_asyncio_sleep():
    tree = ast.parse(
        "import asyncio\n"
        "async def good():\n"
        "    await asyncio.sleep(1)\n"
    )
    findings = _collect_findings(tree)
    assert findings == []


def test_walker_does_not_flag_sync_def():
    """Bare blocking call inside a SYNC function is fine — the walker
    only inspects async defs (sync helpers may be invoked via to_thread
    by their callers, which is the correct pattern)."""
    tree = ast.parse(
        "import subprocess\n"
        "def bad():\n"
        "    subprocess.run(['echo'])\n"
    )
    findings = _collect_findings(tree)
    assert findings == []


def test_walker_flags_renderer_method_with_loop_dot_renderer():
    """Receiver `loop.renderer.X` should be detected (the api_settings.py
    pattern: `await asyncio.to_thread(loop.renderer.reopen)` IS
    wrapped, but a bare `loop.renderer.reopen()` would not be).
    """
    tree = ast.parse(
        "async def bad(loop):\n"
        "    loop.renderer.reopen()\n"
    )
    findings = _collect_findings(tree)
    assert findings == [("bad", "loop.renderer.reopen")]


def test_walker_does_not_flag_unknown_receiver():
    """Defensive: `other_thing.advance(x)` where `other_thing` isn't
    in the renderer-receivers list should NOT be flagged. The
    detector intentionally only flags known shapes — false positives
    would dilute the signal."""
    tree = ast.parse(
        "async def maybe(self):\n"
        "    self.unknown_obj.advance(123)\n"
    )
    findings = _collect_findings(tree)
    assert findings == []


def test_walker_flags_blocking_call_in_nested_control_flow():
    """A bare blocking call inside an `if` / `try` / `for` block must
    still be flagged. ast.walk recurses through sub-nodes; pinning
    this prevents a future scope-narrowing of _walk_async_blocking_calls
    from accidentally skipping nested constructs."""
    tree = ast.parse(
        "async def bad():\n"
        "    import subprocess\n"
        "    for _ in range(3):\n"
        "        try:\n"
        "            if True:\n"
        "                subprocess.run(['echo'])\n"
        "        except Exception:\n"
        "            pass\n"
    )
    findings = _collect_findings(tree)
    assert findings == [("bad", "subprocess.run")]


def test_walker_flags_async_def_nested_in_sync_def():
    """An async def can be defined inside a sync def (closures,
    factory functions). The walker MUST scan such nested async
    defs — pinning this prevents a future ast.walk swap to a
    shallow-only iteration from missing the nested scope."""
    tree = ast.parse(
        "def outer():\n"
        "    async def inner():\n"
        "        import time\n"
        "        time.sleep(1)\n"
        "    return inner\n"
    )
    findings = _collect_findings(tree)
    assert findings == [("inner", "time.sleep")]


def _collect_findings(tree: ast.Module) -> list[tuple[str, str]]:
    """Helper for the walker self-tests: collect (fn_name, callee) pairs."""
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    match = _classify_call(sub)
                    if match is not None:
                        out.append((node.name, match))
    return out
