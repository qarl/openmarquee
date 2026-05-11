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


def parse_upload_class_fields(code_root: Path) -> dict[str, list[str]]:
    """Best-effort: scan backend api*.py for `class FooUpload(BaseModel):`
    blocks and pull out top-level field-name identifiers from them.

    Heuristic — Pydantic's model declaration syntax is regular enough
    that a regex pass catches the common shape (`name: type = ...` lines
    in the class body) without needing to actually import the modules.
    Misses inheritance + dynamically-built fields; that's fine for the
    drift-detection use case where we want false positives, not false
    negatives.

    Returns {class_name: [field_name, ...]} for each *Upload class found.
    """
    backend = code_root / "backend" / "openmarquee"
    out: dict[str, list[str]] = {}
    for path in sorted(backend.glob("api*.py")):
        text = path.read_text()
        # Match `class <Name>Upload(<Base>):` and capture name + the rest
        # of the file (we'll trim to the class body below).
        for class_match in re.finditer(
            r"^class (\w+Upload)\([^)]+\):\n((?: {4}.*\n|\n)+)",
            text,
            re.MULTILINE,
        ):
            cls_name, body = class_match.group(1), class_match.group(2)
            fields: list[str] = []
            for fm in re.finditer(
                r"^    ([a-z_][a-z_0-9]*)\s*:\s*",
                body,
                re.MULTILINE,
            ):
                fname = fm.group(1)
                # Skip Pydantic config / model-private identifiers.
                if fname.startswith("_") or fname == "model_config":
                    continue
                fields.append(fname)
            if fields:
                out[cls_name] = fields
    return out


def find_handler_blocks(mock_text: str) -> list[tuple[str, str]]:
    """Yield (handler_label, handler_text_chunk) blocks from the mock.

    The mock's route handlers are framed by `if (...method/path...)` /
    `// --- comment ---` boundaries. A robust block-extractor is more
    work than the drift signal is worth -- the heuristic just slices on
    blank lines + comment-separator lines and tags each chunk with the
    first method/path-string it contains.
    """
    blocks: list[tuple[str, str]] = []
    chunks = re.split(r"\n\s*//\s*---", mock_text)
    for chunk in chunks:
        # Tag each chunk with the first /api/... path it mentions so a
        # missing-field warning can name a route.
        path_match = re.search(r'["\'`](/api/[^"\'`]+)', chunk)
        label = path_match.group(1) if path_match else "(unlabeled)"
        blocks.append((label, chunk))
    return blocks


# Map a *Upload class to the route-path substring(s) where its fields
# should reach the mock state. Add entries here as new *Upload models
# land in the backend.
_UPLOAD_CLASS_TO_PATH = {
    "TextSlideUpload": "/api/content/text-slides",
    # ImageUpload / VideoUpload POSTs are currently forbidden in the
    # mock (the demo's bg-picker only serves pre-seeded media), so the
    # checker has no merge handler to scan -- it falls through cleanly.
    # When those routes do get implemented in mock-backend.js, the
    # class-name-driven parse_upload_class_fields() pass picks them up
    # automatically and surfaces any missing fields.
    "ImageUpload": "/api/content/images",
    "VideoUpload": "/api/content/videos",
}


def check_upload_field_drift(
    upload_fields: dict[str, list[str]],
    mock_text: str,
) -> list[str]:
    """Return human-readable warnings for fields declared in an Upload
    class but never mentioned in the mock route handler that serves
    that class's path.

    False-positive risk: a handler that uses Object.assign(item, body)
    legitimately handles every body field without naming any. We treat
    `Object.assign(item, body)` (or a `...body` spread) anywhere in a
    chunk as "handles all fields" and short-circuit the per-field check.
    """
    warnings: list[str] = []
    blocks = find_handler_blocks(mock_text)
    for cls_name, fields in upload_fields.items():
        target_path = _UPLOAD_CLASS_TO_PATH.get(cls_name)
        if not target_path:
            continue
        # Find blocks that mention this path. A path can show up in
        # multiple blocks (e.g. a "read path" block AND a "write path:
        # blocked" block both reference /api/content/text-slides). The
        # field check needs ONE block to satisfy the merge -- if any
        # block uses Object.assign(item, body) OR names every field,
        # the contract is honored.
        matching = [
            (label, body) for (label, body) in blocks if target_path in body
        ]
        if not matching:
            continue
        # Trust the handler if ANY matching block does a wildcard
        # merge (Object.assign(item, body) OR a `...body` spread).
        if any(
            re.search(r"Object\.assign\s*\(\s*\w+\s*,\s*body\b", body)
            or re.search(r"\.\.\.\s*body\b", body)
            for _, body in matching
        ):
            continue
        # If NO matching block even reads body / request.json(), the
        # path isn't a write-merge handler at all -- e.g. ImageUpload /
        # VideoUpload POSTs are forbidden in the demo (no state to
        # merge). When those routes get a real handler later, that
        # block WILL parse body and the wildcard-merge or per-field
        # check below applies.
        if not any(
            re.search(r"\bbody\.\w", body)
            or re.search(r"request\.json\s*\(", body)
            for _, body in matching
        ):
            continue
        # No wildcard merge -- need to find each field somewhere in the
        # matching blocks (union of mentions across all chunks).
        merged_body = "\n".join(body for _, body in matching)
        first_label = matching[0][0]
        for field in fields:
            # png_base64 / video_base64 are wire-only inputs; the
            # mock doesn't persist binary assets.
            if field.endswith("_base64"):
                continue
            # Word-boundary match so `name` doesn't false-match
            # `filename`.
            if not re.search(rf"\b{re.escape(field)}\b", merged_body):
                warnings.append(
                    f"{cls_name}.{field} not mentioned in mock "
                    f"handler near {first_label}"
                )
    return warnings


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

    upload_fields = parse_upload_class_fields(code_root)
    field_warnings = check_upload_field_drift(upload_fields, mock_text)

    if not missing and not field_warnings:
        print(f"  ok — {len(real)} backend routes accounted for in mock")
        return 0

    if missing:
        print(f"  drift: {len(missing)} backend route(s) not handled by demo mock:")
        for method, path in missing:
            print(f"    {method:6s} {path}")
        print("  → edit scripts/demo/static/mock-backend.js to handle them,")
        print("    or rest assured if the demo doesn't exercise them.")

    if field_warnings:
        print(f"  field-drift: {len(field_warnings)} Upload field(s) "
              "look unhandled in the mock:")
        for w in field_warnings:
            print(f"    {w}")
        print("  → merge handler should reach the field, OR use "
              "Object.assign(item, body) to mirror Pydantic round-trip.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
