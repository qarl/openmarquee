"""r64 (2026-06-05): `GET /api/perf-stats` — cheap perf telemetry
endpoint.

One handler, ~1 KB JSON response, no auth, no disk IO, no shelling
out, no journalctl. Just dumps the in-memory rolling state from
`openmarquee.perf_stats`. Sub-millisecond serve.

QA polls this once a minute from a small client script and computes
deltas between consecutive snapshots (frames_observed_total,
frame_over_budget_total, transitions_total) plus inspects the
recent samples for cache_hit semantics, preload tail latency, etc.

See `qa/r64-*` (if added) or the r64 dispatch in QA's record for
the full data shape contract.
"""

from __future__ import annotations

from fastapi import APIRouter

from openmarquee.perf_stats import STATS

router = APIRouter(prefix="/api", tags=["perf"])


@router.get("/perf-stats", summary="In-memory perf telemetry snapshot")
async def get_perf_stats() -> dict:
    """Return the current rolling perf state. See
    `openmarquee.perf_stats.PerfStats.snapshot()` for the schema.

    No auth -- read-only, no PII, Tailscale-gated externally."""
    return STATS.snapshot()
