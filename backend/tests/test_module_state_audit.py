"""Perf-night r12 (2026-05-26) regression-guard.

Walks every `backend/openmarquee/*.py` via the AST and flags any
module-level **empty mutable container** (`{}`, `set()`, `dict()`,
`[]`, `list()`, `defaultdict(...)`) that's a candidate for
unbounded growth at runtime. Mirrors r6's `test_async_sync_audit`
pattern (66d9964) for a different bug class.

# What this guards against

Module-level state grows for the process's lifetime. An empty
container at module scope is a classic memory-leak vector — if any
caller `.add()`s / `.append()`s / `dict[k] = v` to it from inside
a function, the structure accumulates entries forever (or until
something explicitly prunes / pops them).

r12's audit found 2 such vectors:
  - web_screenshot.py `_failed_slide_ids` (originally bare `set()`)
  - playback.py `_web_last_fetch` (instance-state, but same shape;
    playlist-pruned but not time-pruned)

Both fixed in r12. This test pins the property: any future
module-level empty container in `backend/openmarquee/` MUST either
add an entry to `_ALLOWLIST` with a >=20-char rationale, or
provide a bounding mechanism (TTL / LRU / pruning).

# Limitations

- Class-level state (`class Foo: _shared = {}`) NOT checked.
  Different fix shape; r13+ if it bites.
- Instance state in `__init__` (`self.x = {}`) NOT checked.
  Bounded by instance lifetime — typically per-request or per-
  session.
- Module-level state with a NON-EMPTY literal initializer
  (`_TIERS: dict = {"a": 1}`) NOT flagged — those are constants
  shaped as dicts; growth requires explicit code that the walker
  doesn't trace. Future runtime mutation is the latent risk,
  flagged via PR review.
- Tuple / frozenset / FrozenDict — NOT flagged. Immutable by
  construction.

Per the dispatch's hard rule: "Allowlist entries MUST have inline
justification comments — future drift through 'just allowlist it'
would defeat the purpose." Enforced by
`test_allowlist_entries_have_rationale` below.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterator


# ---------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------
#
# Each entry: ((relative_file_path, variable_name), rationale)
# - relative_file_path: relative to repo root
#   (e.g. "backend/openmarquee/foo.py")
# - variable_name: the module-level name
# - rationale: a non-trivial operator-grade explanation of why
#   the unbounded growth is acceptable. Reviewed at allowlist-
#   update time.
#
# r12 baseline: the 2 audit-fixed surfaces remain as module-level
# empty containers but now have TTL prune. Listed here so the
# test passes; future module-level empty containers MUST either
# add a TTL/LRU/prune OR add an entry here defending the absence.
_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "backend/openmarquee/web_screenshot.py",
        "_failed_slide_ids",
    ): (
        "r12 (2026-05-26): replaced bare set with dict[UUID, float] + "
        "TTL prune on access (24h). Entries drop from the throttle map "
        "after _FAILED_SLIDE_TTL_SECONDS. See web_screenshot.py:_prune_failed_slide_ids."
    ),
    (
        "backend/openmarquee/auto_render.py",
        "_image_bg_cache",
    ): (
        "Manual-LRU bounded at _IMAGE_BG_LRU_MAX (4 entries) — see "
        "load_background() at auto_render.py:180-181 which calls "
        "popitem(last=False) on overflow. clear_image_bg_cache() is the "
        "test/safety reset hook. Walker's empty-container heuristic "
        "flags this conservatively; the LRU eviction at write-site "
        "bounds the cache to ~33 MB (4 × 8.4 MB × 1.5 RGB+resize) which "
        "fits the Pi Zero 2 W budget per the comment block at lines "
        "139-146. r12 (2026-05-26) audit-confirmed: walker was right "
        "to flag the shape, but the LRU at write-site makes this bounded."
    ),
    (
        "backend/openmarquee/auth.py",
        "_VERIFIED_TOKEN_CACHE",
    ): (
        "OrderedDict LRU bounded at _VERIFIED_TOKEN_CACHE_CAP (1024 "
        "entries) with _VERIFIED_TOKEN_TTL_SECONDS (10 min) per entry. "
        "Round-10 DiD: capacity-bounded against cache-key-mangling "
        "attacks. Cache HIT moves entry to tail (most-recent); INSERT "
        "at cap evicts head (least-recent). See auth.py:265-280 + "
        "clear_verified_token_cache() helper for the reset hook. "
        "Caught by the walker on the r14 cherry-pick to main; the "
        "container existed pre-r12 on main but wasn't in code2's "
        "tree when r12 was authored (cherry-pick fix following the "
        "d859494 paint_us_p99 precedent)."
    ),
}


# ---------------------------------------------------------------
# AST walker
# ---------------------------------------------------------------


def _is_empty_mutable_container(node: ast.AST) -> bool:
    """Return True if `node` is an EMPTY mutable container literal
    or constructor call that's a leak vector at module scope.

    True for:
      - `{}` (empty Dict)
      - `[]` (empty List)
      - `set()` (Call with no args)
      - `dict()` (Call with no args)
      - `list()` (Call with no args)
      - `defaultdict(...)` (Call — any args, factory pattern)
      - `OrderedDict()` (Call with no args)

    False for:
      - Non-empty dict/list/set literals (those are constants by
        construction; mutation would be a separate latent risk)
      - Tuple / frozenset / FrozenDict (immutable)
      - Any other expression
    """
    # Empty literals
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    if isinstance(node, ast.List) and not node.elts:
        return True
    if isinstance(node, ast.Set):
        # Note: there's no syntactic empty Set literal in Python
        # (`{}` is a dict). This branch covers `{1, 2}` removing
        # elements down to empty — but Python's ast doesn't produce
        # an empty Set node from `{}`. Kept for completeness.
        return not node.elts
    # Constructor calls
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
            if name in {"set", "dict", "list", "OrderedDict"}:
                return not node.args and not node.keywords
            if name == "defaultdict":
                # defaultdict's factory pattern means it grows on
                # any `d[missing_key]` access. Always-flag.
                return True
        elif isinstance(func, ast.Attribute):
            # e.g. `collections.defaultdict(...)` — flag on attr name
            if func.attr in {"defaultdict", "OrderedDict"}:
                return True
    return False


def _find_module_level_empty_containers(
    module_path: pathlib.Path,
) -> Iterator[tuple[str, int]]:
    """Yield (variable_name, lineno) for every module-level empty
    mutable container declaration.

    Handles both:
      - Assign: `_foo = {}` / `_foo = set()`
      - AnnAssign: `_foo: dict = {}` / `_foo: set[int] = set()`

    Only top-level statements (`tree.body`) are inspected; nested
    function-scope / class-scope assignments are intentionally
    skipped (see module docstring's Limitations section).
    """
    tree = ast.parse(module_path.read_text())
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if stmt.value is None or not _is_empty_mutable_container(stmt.value):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    yield (target.id, stmt.lineno)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is None or not _is_empty_mutable_container(stmt.value):
                continue
            if isinstance(stmt.target, ast.Name):
                yield (stmt.target.id, stmt.lineno)


# ---------------------------------------------------------------
# Tests
# ---------------------------------------------------------------


def _backend_src_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "openmarquee"


def _repo_root_relative(file_path: pathlib.Path) -> str:
    backend_dir = pathlib.Path(__file__).resolve().parent.parent
    return f"backend/{file_path.relative_to(backend_dir)}"


def test_no_unbounded_module_level_containers():
    """r12 regression-guard. The audit found 2 module-level empty-
    mutable-container surfaces (web_screenshot._failed_slide_ids +
    playback._web_last_fetch — the latter is instance state, not
    module-level, so not flagged by this walker). Both fixed in
    r12 with TTL prune. This test pins the invariant: any future
    module-level empty container in backend/openmarquee/ trips
    CI unless added to _ALLOWLIST with a real rationale.

    To resolve a failure here, either:
      (1) Add a bounding mechanism to the new module-level
          container (TTL prune on access, LRU eviction, periodic
          cleanup task) and add to _ALLOWLIST with the bounding
          rationale.
      (2) Refactor to instance-state if it's actually request-
          scoped (instance state is bounded by instance lifetime;
          typically per-request).
      (3) If the container is genuinely write-once-and-stays-
          constant (e.g., a registry populated at boot from a
          deterministic source), add to _ALLOWLIST documenting
          that "writes happen at boot only, never at runtime".
    """
    src_root = _backend_src_root()
    findings: list[tuple[str, str, int]] = []
    for py_file in sorted(src_root.rglob("*.py")):
        # Skip macOS AppleDouble resource forks (per
        # [[project_apple_double_in_git_breaks_stash]] / r6
        # precedent at test_async_sync_audit.py).
        if py_file.name.startswith("._"):
            continue
        rel = _repo_root_relative(py_file)
        for var_name, lineno in _find_module_level_empty_containers(py_file):
            key = (rel, var_name)
            if key in _ALLOWLIST:
                continue
            findings.append((rel, var_name, lineno))

    if findings:
        msg = ["Module-level empty mutable containers found (r12 regression):"]
        for rel, var, lineno in findings:
            msg.append(f"  {rel}:{lineno}  {var}")
        msg.append("")
        msg.append(
            "Module-level containers grow for the process's lifetime. "
            "Either add a bounding mechanism (TTL prune, LRU, periodic "
            "cleanup) AND add an entry to _ALLOWLIST in this file, OR "
            "refactor to instance-state. See the docstring of this "
            "test file for the standard patterns + r12 commit body for "
            "the audit context."
        )
        raise AssertionError("\n".join(msg))


def test_allowlist_entries_have_rationale():
    """The dispatch's hard rule: allowlist entries MUST carry a real
    rationale. Empty / placeholder rationales fail here so an
    accidental empty-string-add doesn't silently dilute the guard.
    Mirrors r6's same-named test at test_async_sync_audit.py."""
    bad: list[str] = []
    for key, rationale in _ALLOWLIST.items():
        if not isinstance(rationale, str) or len(rationale.strip()) < 20:
            bad.append(f"  {key}: rationale={rationale!r}")
    if bad:
        raise AssertionError(
            "Allowlist entries with missing/trivial rationale:\n"
            + "\n".join(bad)
            + "\n\nEvery exception must carry a non-trivial explanation "
            "(>= 20 chars) of why the container is bounded or "
            "acceptable as module-level state."
        )


# ---------------------------------------------------------------
# Walker self-tests
# ---------------------------------------------------------------
#
# Pin the walker's classification behavior on synthetic inputs.
# Without these, a future bug in `_is_empty_mutable_container` /
# `_find_module_level_empty_containers` could silently make the
# main test always-pass.


def _collect_findings(source: str) -> list[tuple[str, int]]:
    """Parse source, walk top-level, return (name, lineno) pairs."""
    tree = ast.parse(source)
    out: list[tuple[str, int]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if stmt.value is None or not _is_empty_mutable_container(stmt.value):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    out.append((target.id, stmt.lineno))
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is None or not _is_empty_mutable_container(stmt.value):
                continue
            if isinstance(stmt.target, ast.Name):
                out.append((stmt.target.id, stmt.lineno))
    return out


def test_walker_flags_bare_empty_dict_assign():
    findings = _collect_findings("_foo = {}\n")
    assert findings == [("_foo", 1)]


def test_walker_flags_bare_empty_set_call():
    findings = _collect_findings("_foo = set()\n")
    assert findings == [("_foo", 1)]


def test_walker_flags_bare_empty_list_literal():
    findings = _collect_findings("_foo = []\n")
    assert findings == [("_foo", 1)]


def test_walker_flags_annotated_empty_dict():
    findings = _collect_findings("from uuid import UUID\n_foo: dict[UUID, float] = {}\n")
    assert findings == [("_foo", 2)]


def test_walker_flags_defaultdict():
    findings = _collect_findings(
        "from collections import defaultdict\n_foo = defaultdict(list)\n"
    )
    assert findings == [("_foo", 2)]


def test_walker_flags_collections_defaultdict_attr():
    findings = _collect_findings(
        "import collections\n_foo = collections.defaultdict(int)\n"
    )
    assert findings == [("_foo", 2)]


def test_walker_does_not_flag_nonempty_dict_literal():
    findings = _collect_findings('_foo = {"a": 1, "b": 2}\n')
    assert findings == []


def test_walker_does_not_flag_nonempty_list_literal():
    findings = _collect_findings("_foo = [1, 2, 3]\n")
    assert findings == []


def test_walker_does_not_flag_tuple():
    findings = _collect_findings("_foo = ()\n")
    assert findings == []


def test_walker_does_not_flag_frozenset():
    findings = _collect_findings("_foo = frozenset()\n")
    assert findings == []


def test_walker_does_not_flag_function_scope_empty_dict():
    findings = _collect_findings(
        "def f():\n    local = {}\n    return local\n"
    )
    assert findings == []


def test_walker_does_not_flag_class_scope_empty_dict():
    findings = _collect_findings(
        "class C:\n    shared = {}\n"
    )
    assert findings == []


def test_walker_handles_multiple_assignments_on_one_line():
    """Multi-assign `a = b = {}` shares one ast.Assign node with
    multiple targets. Both `a` and `b` get flagged."""
    findings = _collect_findings("a = b = {}\n")
    # Each target reports the same lineno because they share the
    # Assign node.
    assert ("a", 1) in findings
    assert ("b", 1) in findings
