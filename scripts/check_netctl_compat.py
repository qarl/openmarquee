#!/usr/bin/env python3
"""check_netctl_compat.py — netctl subcommand ↔ daemon-allowlist compatibility gate.

Deploy-hygiene 2026-07-16 (Phase 2 / P2-b). The JasonsSign1 failure class
(PR #89, 1ac5811): the backend's name_actuator started calling a privileged
netctl subcommand (`avahi-write-and-restart`, added 2026-07-03) that the
DEPLOYED root daemon's allowlist — stale from an earlier deploy — did not
contain, so the daemon rejected it and its per-connection systemd unit landed
in `failed`. `systemctl --failed` cannot tell "stale daemon" from any other
failure, so the skew was silent.

This gate makes the skew LOUD and CHEAP to detect: it extracts

  1. the daemon's ALLOWLIST (the set of subcommands the root daemon will run), and
  2. every netctl subcommand the backend actually CALLS,

and asserts calls ⊆ allowlist. A backend that calls a subcommand the daemon
doesn't allow is a fail-loud error (exit 3).

Both sets are extracted with the stdlib `ast` module — no import of the backend,
no network, no live socket. Robust against the multiline call style the backend
uses (subcommand literal on the line after the `(`).

Usage:
    check_netctl_compat.py [--daemon PATH] [--backend DIR] [--quiet]

Defaults resolve relative to this script's repo (…/system/openmarquee-netctl-daemon
and …/backend). On a sign, point it at the DEPLOYED copies:
    check_netctl_compat.py --daemon /usr/local/sbin/openmarquee-netctl-daemon \\
                           --backend /opt/openmarquee/backend

Exit codes:
    0  compatible (every called subcommand is in the allowlist)
    2  usage / file-not-found / parse error (could not run the check)
    3  INCOMPATIBLE — one or more called subcommands are NOT in the allowlist
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

# The backend has THREE independent netctl transports (netctl_client's
# netctl_send/netctl_recv_data speak the socket protocol; network_supervisor_
# actuator._netctl_send and network_supervisor_takeover._run_netctl each open
# the socket directly). Each takes the subcommand as its FIRST positional arg,
# so call-site collection keys off these names.
KNOWN_SENDERS = frozenset(
    {"netctl_send", "netctl_recv_data", "_netctl_send", "_run_netctl"}
)

# Every transport references the daemon socket path. `assert_no_unknown_transport`
# uses this to catch a FUTURE transport wrapper that isn't in KNOWN_SENDERS —
# so a new sender can't silently bypass the gate (fail-loud, exit 2). Kept in
# sync with system/openmarquee-netctl.socket's ListenStream.
SOCKET_PATH_LITERAL = "/run/openmarquee/netctl.sock"


def _repo_root() -> str:
    # scripts/check_netctl_compat.py -> repo root is the parent of scripts/.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def extract_allowlist(daemon_path: str) -> set[str]:
    """Return the set of subcommand strings in the daemon's ALLOWLIST.

    Parses `ALLOWLIST = frozenset({...})` (or a bare set literal) and collects
    the string constants. Raises SystemExit(2) if the assignment or its string
    elements can't be found (better to fail the gate than silently pass on a
    daemon we couldn't understand).
    """
    with open(daemon_path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=daemon_path)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "ALLOWLIST" for t in node.targets
        ):
            continue
        value = node.value
        # frozenset({...}) -> unwrap the call to its single set/list arg.
        if isinstance(value, ast.Call) and value.args:
            value = value.args[0]
        if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            names = {
                el.value
                for el in value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            }
            if names:
                return names
        _die(2, f"{daemon_path}: found ALLOWLIST but could not extract string members")
    _die(2, f"{daemon_path}: no ALLOWLIST assignment found")


def _call_name(call: ast.Call) -> "str | None":
    """Bare callee name for a Call node: `f(...)` -> 'f', `obj.f(...)` -> 'f'."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _keyword_value(call: ast.Call, name: str) -> "ast.expr | None":
    """Value node of the `name=` keyword arg of a Call, or None."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def assert_no_unknown_transport(
    named_trees: "list[tuple[str, ast.AST]]", backend_dir: str
) -> None:
    """Fail LOUD if a backend function speaks to the netctl socket but is not a
    KNOWN_SENDER. This is the gate's self-defense: KNOWN_SENDERS is hardcoded (a
    fixpoint auto-discovery can't see the direct-socket transports), so a future
    transport wrapper would otherwise silently escape call-site collection and
    let a skewed subcommand ship unchecked. A function is treated as a transport
    if it references the netctl socket-path literal (directly or via any module
    constant assigned that literal). Exits 2 on any unknown transport.
    """
    # Module constants bound to the socket path (NETCTL_SOCKET_PATH,
    # DEFAULT_SOCKET_PATH, …) — discovered, not hardcoded, so a new module's own
    # constant name is still recognized.
    socket_consts: set[str] = set()
    for _, tree in named_trees:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and node.value.value == SOCKET_PATH_LITERAL
            ):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        socket_consts.add(tgt.id)

    unknown: dict[str, str] = {}
    for path, tree in named_trees:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in KNOWN_SENDERS:
                continue
            for sub in ast.walk(node):
                touches = (isinstance(sub, ast.Name) and sub.id in socket_consts) or (
                    isinstance(sub, ast.Constant) and sub.value == SOCKET_PATH_LITERAL
                )
                if touches:
                    unknown[node.name] = (
                        f"{os.path.relpath(path, backend_dir)}:{node.lineno}"
                    )
                    break

    if unknown:
        lines = "\n".join(f"  {n}  ({loc})" for n, loc in sorted(unknown.items()))
        _die(
            2,
            "unknown netctl transport function(s) touch the daemon socket but are "
            "not in KNOWN_SENDERS — the gate cannot guarantee it enumerates their "
            f"subcommand calls:\n{lines}\n"
            "  Fix: add each to KNOWN_SENDERS in scripts/check_netctl_compat.py "
            "(and confirm it takes the subcommand as its first positional arg).",
        )


def extract_backend_calls(backend_dir: str) -> tuple[dict[str, list[str]], int]:
    """Collect literal netctl subcommands called anywhere in the backend.

    Returns (called, non_literal_count):
      - `called` maps each subcommand string to the "file:line" sites calling it,
      - `non_literal_count` is how many sender calls had a non-literal first arg
        (e.g. a wrapper-internal `subcommand`-variable passthrough) — a
        diagnostic, not a failure.

    Runs the self-defense meta-check first (assert_no_unknown_transport), then
    collects call sites keyed off KNOWN_SENDERS. Files under any `tests/`
    directory are skipped: negative-path test fixtures intentionally pass bogus /
    rejected subcommands and would produce false "incompatible" hits.
    """
    named_trees: list[tuple[str, ast.AST]] = []
    for root, dirs, files in os.walk(backend_dir):
        # Prune test + hidden dirs in-place so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if d != "tests" and not d.startswith(".")]
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            with open(path, "r", encoding="utf-8") as fh:
                try:
                    named_trees.append((path, ast.parse(fh.read(), filename=path)))
                except SyntaxError as exc:
                    _die(2, f"{path}: parse error: {exc}")

    assert_no_unknown_transport(named_trees, backend_dir)

    called: dict[str, list[str]] = {}
    non_literal = 0
    for path, tree in named_trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in KNOWN_SENDERS:
                continue
            # The subcommand is the first POSITIONAL arg, or a `subcommand=`
            # keyword (every sender names its first param `subcommand`). Check
            # BOTH — testing only node.args[0] would let `netctl_send(
            # subcommand="new-cmd")` slip the gate as a false PASS (the exact
            # skew this exists to catch).
            arg = node.args[0] if node.args else _keyword_value(node, "subcommand")
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                site = f"{os.path.relpath(path, backend_dir)}:{node.lineno}"
                called.setdefault(arg.value, []).append(site)
            else:
                # Non-literal (wrapper-internal `subcommand` variable passthrough)
                # or no discernible subcommand — surface, don't silently drop.
                non_literal += 1
    return called, non_literal


def _die(code: int, msg: str) -> "None":
    print(f"check-netctl-compat: {msg}", file=sys.stderr)
    raise SystemExit(code)


def main(argv: list[str]) -> int:
    root = _repo_root()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--daemon",
        default=os.path.join(root, "system", "openmarquee-netctl-daemon"),
        help="path to the netctl daemon (default: repo system/openmarquee-netctl-daemon)",
    )
    ap.add_argument(
        "--backend",
        default=os.path.join(root, "backend"),
        help="path to the backend package dir (default: repo backend/)",
    )
    ap.add_argument("--quiet", action="store_true", help="only print on failure")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.daemon):
        _die(2, f"daemon not found: {args.daemon}")
    if not os.path.isdir(args.backend):
        _die(2, f"backend dir not found: {args.backend}")

    allowlist = extract_allowlist(args.daemon)
    called, non_literal = extract_backend_calls(args.backend)

    missing = {sub: sites for sub, sites in called.items() if sub not in allowlist}

    if missing:
        print(
            "check-netctl-compat: INCOMPATIBLE — backend calls subcommand(s) the "
            "daemon allowlist does NOT contain:",
            file=sys.stderr,
        )
        for sub in sorted(missing):
            print(f"  {sub!r}  (called at: {', '.join(missing[sub])})", file=sys.stderr)
        print(
            f"  daemon: {args.daemon}\n  backend: {args.backend}\n"
            "  Fix: sync system/ (the daemon allowlist) to match the backend — "
            "scripts/sync-system-to-sign.sh — or add the subcommand to the daemon "
            "ALLOWLIST if it is genuinely new.",
            file=sys.stderr,
        )
        return 3

    if not args.quiet:
        unused = sorted(allowlist - set(called))
        print(
            f"check-netctl-compat: OK — {len(called)} called subcommand(s) all in a "
            f"{len(allowlist)}-entry allowlist."
        )
        if non_literal:
            print(
                f"  note: {non_literal} netctl call(s) had a non-literal subcommand "
                "(not statically checkable; e.g. the _netctl_send→netctl_send passthrough)."
            )
        if unused:
            print(
                f"  note: {len(unused)} allowlist entr(y/ies) never called by the "
                f"backend (informational): {', '.join(unused)}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
