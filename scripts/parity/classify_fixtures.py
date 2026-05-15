#!/usr/bin/env python3
"""Phase 3l-post: classify the 39 parity_tests fixtures by text-presence
+ pair each with its current parity metrics.

Finding (2026-05-15): EVERY fixture in this corpus carries at least
one text layer — bg-pattern tests use a small label ("SOLID",
"GRADIENT", ...) as a visual identifier. There are zero pure
non-text fixtures. To still answer the underlying QA question — is
Cause B the universal blocker? — we also classify by fixture
category (bg/font/animated/transition/fys) and by mean_delta tier,
which is a reasonable proxy for whether the text overlay dominates
the diff (low mean = text hairlines only) or there's a broader
mismatch (high mean = motion/transition/structural).

Output: qa/captures/parity-fixture-classification-2026-05-15.json
plus a markdown summary printed to stderr.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES_JSON = REPO / "scripts" / "parity" / "fixtures.json"
FIXTURE_DIR = REPO / "renderer" / "tests" / "fixtures"
PARITY_LOG = Path("/tmp/parity_phase3l_v2.log")
OUT_PATH = REPO / "qa" / "captures" / "parity-fixture-classification-2026-05-15.json"

# Regex matches one parity-log line:
# PASS: parity_<name>     SSIM=0.9961 (>=0.95) max_delta=231 (<=50) mean_delta=0.245 pixels_over_10=0.31% vs golden/foo.png
LINE_RE = re.compile(
    r"^(PASS|FAIL):\s+(\S+)\s+SSIM=([\d.]+)\s+\(.*?\)\s+max_delta=(\d+)\s+\(.*?\)\s+mean_delta=\s*([\d.]+)\s+pixels_over_10=\s*([\d.]+)%"
)


def _load_item(uuid: str):
    p = FIXTURE_DIR / uuid / "item.json"
    if not p.exists():
        return None
    b = json.loads(p.read_text())
    return b["item"] if "item" in b else b


def load_fixtures():
    spec = json.loads(FIXTURES_JSON.read_text())
    out = []
    for f in spec["fixtures"]:
        kind = f.get("kind", "single")
        text_layers = []
        uuid_repr = None
        if kind == "transition_mid":
            # Transitions reference two slides; check both items
            uuid_repr = f"{f.get('from_uuid','?')[:8]}->{f.get('to_uuid','?')[:8]}"
            for uuid_key in ("from_uuid", "to_uuid"):
                uid = f.get(uuid_key)
                if uid:
                    i = _load_item(uid)
                    if i is not None:
                        text_layers.extend(i.get("text_layers", []))
        else:
            uid = f.get("uuid")
            uuid_repr = uid
            item = _load_item(uid) if uid else None
            if item is not None and item.get("type") == "text_slide":
                text_layers = item.get("text_layers", [])
        has_text = any(
            isinstance(l.get("text"), str) and l["text"].strip()
            for l in text_layers
        )
        out.append({
            "name": f["name"],
            "uuid": uuid_repr,
            "kind": kind,
            "golden": f.get("golden"),
            "has_text": has_text,
            "text_samples": [l.get("text") for l in text_layers if l.get("text")],
        })
    return out


def parse_parity_log():
    """Parse /tmp/parity_phase3l_v2.log into {fixture_name: metrics}."""
    metrics = {}
    if not PARITY_LOG.exists():
        return metrics
    for line in PARITY_LOG.read_text().splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        status, name, ssim, max_delta, mean_delta, pixels_over_10 = m.groups()
        metrics[name] = {
            "status": status,
            "ssim": float(ssim),
            "max_delta": int(max_delta),
            "mean_delta": float(mean_delta),
            "pixels_over_10_pct": float(pixels_over_10),
        }
    return metrics


def _category(name: str) -> str:
    # parity_bg_*, parity_font_*, parity_animated_*, parity_transition_*,
    # parity_fys_*, otherwise "other"
    stem = name.removeprefix("parity_")
    for cat in ("bg", "font", "animated", "transition", "fys"):
        if stem.startswith(cat + "_") or stem == cat:
            return cat
    return "other"


def _mean_tier(mean_delta: float) -> str:
    # <1: text-AA hairlines dominate; 1-10: broader mismatch; >10: structural
    if mean_delta < 1.0:
        return "hairlines"
    if mean_delta < 10.0:
        return "broad"
    return "structural"


def main():
    fixtures = load_fixtures()
    metrics = parse_parity_log()

    classified = []
    for f in fixtures:
        m = metrics.get(f["name"])
        if m is None:
            continue
        row = {**f, **m}
        row["category"] = _category(f["name"])
        row["mean_tier"] = _mean_tier(m["mean_delta"])
        classified.append(row)

    text_bucket = [f for f in classified if f["has_text"]]
    non_text_bucket = [f for f in classified if not f["has_text"]]

    # Gate: SSIM >= 0.95 AND max_delta <= 50
    def passes_gate(f):
        return f["ssim"] >= 0.95 and f["max_delta"] <= 50

    text_pass = [f for f in text_bucket if passes_gate(f)]
    non_text_pass = [f for f in non_text_bucket if passes_gate(f)]

    # Sort by closest-to-PASS: lower max_delta first; tie-break by lower mean_delta
    def closeness_key(f):
        return (f["max_delta"], f["mean_delta"])

    top5_text = sorted(text_bucket, key=closeness_key)[:5]
    top5_non_text = sorted(non_text_bucket, key=closeness_key)[:5]

    # Category + tier breakdowns
    cats = {}
    for f in classified:
        cats.setdefault(f["category"], []).append(f)
    tiers = {}
    for f in classified:
        tiers.setdefault(f["mean_tier"], []).append(f)

    def cat_summary(items):
        ps = [x for x in items if passes_gate(x)]
        return {
            "count": len(items),
            "pass_count": len(ps),
            "closest_max_delta": min(x["max_delta"] for x in items) if items else None,
            "mean_of_mean_delta": (sum(x["mean_delta"] for x in items)/len(items)) if items else None,
        }

    out = {
        "total_fixtures": len(classified),
        "text_bearing_count": len(text_bucket),
        "non_text_count": len(non_text_bucket),
        "text_pass_count": len(text_pass),
        "non_text_pass_count": len(non_text_pass),
        "note": (
            "Every fixture in this corpus carries at least one text layer "
            "(bg-pattern tests use a small label as a visual identifier). "
            "Category + mean_tier give an alternative axis: mean_delta<1 "
            "implies the diff is dominated by text-AA hairlines (Cause B); "
            "mean_delta>10 implies a broader/structural mismatch."
        ),
        "by_category": {c: cat_summary(items) for c, items in cats.items()},
        "by_mean_tier": {t: cat_summary(items) for t, items in tiers.items()},
        "top5_closest_to_pass_text_bearing": top5_text,
        "all_fixtures_sorted_by_closeness": sorted(
            [{**f} for f in classified], key=closeness_key
        ),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, default=str))

    print(f"=== Phase 3l-post fixture classification ===\n", file=sys.stderr)
    print(f"Total fixtures (from parity log): {len(classified)}", file=sys.stderr)
    print(f"  Text-bearing: {len(text_bucket)}", file=sys.stderr)
    print(f"  Non-text:     {len(non_text_bucket)}  (corpus has none)", file=sys.stderr)
    print(f"\nGate-pass counts (SSIM>=0.95 AND max_delta<=50):", file=sys.stderr)
    print(f"  Text-bearing PASS:   {len(text_pass)} / {len(text_bucket)}", file=sys.stderr)
    print(f"\nBy category:", file=sys.stderr)
    for c, items in sorted(cats.items()):
        s = cat_summary(items)
        print(f"  {c:12s}  n={s['count']:2d}  pass={s['pass_count']:2d}  closest_max_delta={s['closest_max_delta']:3d}  avg_mean_delta={s['mean_of_mean_delta']:.3f}",
              file=sys.stderr)
    print(f"\nBy mean_delta tier:", file=sys.stderr)
    for t, items in sorted(tiers.items()):
        s = cat_summary(items)
        print(f"  {t:12s}  n={s['count']:2d}  pass={s['pass_count']:2d}  closest_max_delta={s['closest_max_delta']:3d}",
              file=sys.stderr)
    print(f"\nTop-5 closest-to-PASS (all text-bearing):", file=sys.stderr)
    for f in top5_text:
        print(f"  {f['name']:40s}  cat={f['category']:10s}  tier={f['mean_tier']:10s}  max_delta={f['max_delta']:3d}  SSIM={f['ssim']:.4f}  mean={f['mean_delta']:7.3f}",
              file=sys.stderr)
    print(f"\nAll fixtures (sorted by closeness):", file=sys.stderr)
    for f in sorted(classified, key=closeness_key):
        print(f"  {f['name']:40s}  cat={f['category']:10s}  tier={f['mean_tier']:10s}  max_delta={f['max_delta']:3d}  SSIM={f['ssim']:.4f}  mean={f['mean_delta']:7.3f}",
              file=sys.stderr)
    print(f"\nwrote {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
