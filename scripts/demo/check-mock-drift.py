#!/usr/bin/env python3
"""Detect drift between the device backend's REST surface and the demo's
hand-rolled mock-backend.js.

What it does:
  - Walks `backend/openmarquee/api*.py`, parses every
    `@router.<method>(<path>)` decorator and the `prefix=` on the
    APIRouter to reconstruct the full URL pattern.
  - Reads `scripts/demo/static/mock-backend.js` and grep-matches the
    route patterns it explicitly handles (path strings + regexes
    against `/api/...`).
  - Reports endpoints present in the real backend but not handled by
    the mock — those are the ones likely to silently 4xx in the demo
    after a backend route lands.

Heuristic by design — the mock backend is JS, not parseable as
strictly as the FastAPI side, so misses are possible. Treat output as
a heads-up, not a verdict.

Exit code is always 0 (warning, not gate). Pipe to `grep -q` if you
want to fail a CI step on drift.

Usage:
    scripts/demo/check-mock-drift.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def parse_real_routes(code_root: Path) -> list[tuple[str, str]]:
    backend = code_root / "backend" / "openmarquee"
    routes: list[tuple[str, str]] = []
    for path in sorted(backend.glob("api*.py")):
        text = path.read_text()
        prefix_match = re.search(
            r'APIRouter\([^)]*prefix\s*=\s*["\']([^"\']+)["\']',
            text,
        )
        prefix = prefix_match.group(1) if prefix_match else ""
        # Decorators like @router.get("/foo") OR @router.get(\n   "/foo", ...).
        for m in re.finditer(
            r'@router\.(get|post|put|patch|delete)\s*\(\s*["\']([^"\']*)["\']',
            text,
        ):
            method, sub_path = m.group(1).upper(), m.group(2)
            full = (prefix + sub_path) or "/"
            routes.append((method, full))
    return routes


def mock_handles(routes: list[tuple[str, str]], mock_text: str) -> set[tuple[str, str]]:
    handled: set[tuple[str, str]] = set()
    for method, path in routes:
        # Replace FastAPI's {param} with a regex-friendly placeholder so we
        # can match against literal-or-regex strings in the mock.
        # The mock typically writes paths as `/api/flock/${peerId}` (no
        # template) OR matches with a regex like /^\/api\/flock\/[^/]+$/.
        # We probe a few common substring shapes.
        literal = re.sub(r"\{[^}]+\}", "(?:[^/]+)", path)
        lit_no_param = re.sub(r"\{[^}]+\}", "", path).rstrip("/")
        # Strict match: path string occurs in mock (with or without method).
        candidates = [path, literal, lit_no_param]
        if any(c and c in mock_text for c in candidates):
            handled.add((method, path))
            continue
        # Regex form: turn /{x} → /[^/]+ etc and search literally.
        if literal and re.search(re.escape(literal), mock_text):
            handled.add((method, path))
    return handled


def main() -> int:
    here = Path(__file__).resolve().parent
    code_root = here.parent.parent  # scripts/demo/ → scripts/ → code/
    mock_path = here / "static" / "mock-backend.js"

    if not (code_root / "backend").is_dir():
        print(f"warning: no backend at {code_root} — skipping drift check")
        return 0
    if not mock_path.is_file():
        print(f"warning: no mock at {mock_path} — skipping drift check")
        return 0

    real = parse_real_routes(code_root)
    mock_text = mock_path.read_text()
    handled = mock_handles(real, mock_text)
    missing = sorted(set(real) - handled)

    if not missing:
        print(f"  ok — {len(real)} backend routes accounted for in mock")
        return 0

    print(f"  drift: {len(missing)} backend route(s) not handled by demo mock:")
    for method, path in missing:
        print(f"    {method:6s} {path}")
    print("  → edit scripts/demo/static/mock-backend.js to handle them,")
    print("    or rest assured if the demo doesn't exercise them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
