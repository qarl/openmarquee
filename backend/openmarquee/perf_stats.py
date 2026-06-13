"""r64 (2026-06-05): in-process rolling perf telemetry buffer.

Why this module exists:

qarl observed worse stalls on FYS after r62 deployed and traced
back to QA's own measurement methodology — repeated
`ssh fireplacesign 'sudo journalctl ...'` calls were generating
the load that produced the stalls being investigated. The
journal is rich with `[perf] preload`, `[perf] first_frame_paint`,
`[perf] frame over budget` events (qa/r56-video-prewarm-
recommendation.md §A), but pulling them out via journalctl is
expensive on Pi Zero 2 W (~bcm2835 disk IO + journal parse).

This module maintains a small in-memory rolling buffer of the
last N events per kind so QA can poll a single ~1KB HTTP endpoint
(`GET /api/perf-stats`) instead of shelling into the Pi. One HTTP
request, ~1KB response, no disk IO, no journal parse, single-
digit-ms serve.

The buffer is populated two ways:
  1. Backend-timed events (preload duration, begin_slide load
     duration, etc.) pushed directly from playback.py call sites
     where the timing already exists.
  2. Renderer-side `[perf]` lines parsed from the stderr forwarder
     in `rendering/rust_renderer.py::_drain_stderr` — minimal
     overhead (a regex split per stderr line).

Concurrency: the rolling buffers are protected by a single
threading.Lock. All push paths and the snapshot read hold it
briefly. The stderr drainer thread + the playback loop's
executor threads + the FastAPI request handler all hit the lock,
but each operation is O(1) (deque append + counter increment)
so contention is negligible in practice.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Rolling buffer depth per event kind. 50 covers ~30s of activity
# at the existing observed rates (preload fires once per slide;
# first_frame_paint once per slide; frame_over_budget filtered to
# > 100 ms so naturally sparse).
_MAX_SAMPLES = 50

# Filter threshold for frame_over_budget. Below this we DON'T
# record the sample (just bump the running counter) -- per
# dispatch "there are too many small ones."
_FRAME_OVER_BUDGET_MIN_MS = 100

# CMA sampling cadence + 1-minute window. 5 s × 12 = 60 s.
_CMA_SAMPLE_INTERVAL_S = 5.0
_CMA_WINDOW_SAMPLES = 12

# Path used by the CMA sampler. Override via tests by monkeypatching.
_MEMINFO_PATH = Path("/proc/meminfo")


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond resolution."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class PerfStats:
    """Singleton rolling perf telemetry store. Thread-safe via
    `_lock`. All public methods are O(1) under the lock.

    Construction is cheap; the module-level `STATS` singleton at
    the bottom of this file is what every caller uses.
    """

    _lock: threading.Lock
    _start_time: float
    # Per-kind rolling buffers. Each holds dicts with kind-specific
    # fields plus a `ts` ISO timestamp.
    preload_recent: deque[dict[str, Any]]
    first_frame_paint_recent: deque[dict[str, Any]]
    v4l2_prime_recent: deque[dict[str, Any]]
    video_load_recent: deque[dict[str, Any]]
    frame_over_budget_recent: deque[dict[str, Any]]
    begin_slide_load_recent: deque[dict[str, Any]]
    begin_transition_load_recent: deque[dict[str, Any]]
    # Counters (full counts; no truncation).
    frame_over_budget_total: int
    frames_observed_total: int
    transitions_total: int
    # CMA rolling state. Each sample is (timestamp, used_mb).
    _cma_samples: deque[tuple[float, float]]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_time = time.monotonic()
        self.preload_recent = deque(maxlen=_MAX_SAMPLES)
        self.first_frame_paint_recent = deque(maxlen=_MAX_SAMPLES)
        self.v4l2_prime_recent = deque(maxlen=_MAX_SAMPLES)
        self.video_load_recent = deque(maxlen=_MAX_SAMPLES)
        self.frame_over_budget_recent = deque(maxlen=_MAX_SAMPLES)
        self.begin_slide_load_recent = deque(maxlen=_MAX_SAMPLES)
        self.begin_transition_load_recent = deque(maxlen=_MAX_SAMPLES)
        self.frame_over_budget_total = 0
        self.frames_observed_total = 0
        self.transitions_total = 0
        self._cma_samples = deque(maxlen=_CMA_WINDOW_SAMPLES)

    # ---- record methods (push side) -----------------------------

    def record_preload(self, slide_id: str, us: int) -> None:
        with self._lock:
            self.preload_recent.append({"slide_id": slide_id, "us": int(us), "ts": _utc_now_iso()})

    def record_first_frame_paint(
        self,
        slide_id: str,
        bake_us: int,
        composite_us: int,
        present_us: int,
        scanout_us: int,
        total_us: int,
        cache_hit: bool = False,
    ) -> None:
        with self._lock:
            self.first_frame_paint_recent.append(
                {
                    "slide_id": slide_id,
                    "cache_hit": cache_hit,
                    "bake_us": int(bake_us),
                    "composite_us": int(composite_us),
                    "present_us": int(present_us),
                    "scanout_us": int(scanout_us),
                    "total_us": int(total_us),
                    "ts": _utc_now_iso(),
                }
            )

    def record_v4l2_prime(self, **kv: Any) -> None:
        with self._lock:
            entry = {k: kv[k] for k in kv}
            entry["ts"] = _utc_now_iso()
            self.v4l2_prime_recent.append(entry)

    def record_video_load(
        self,
        slide_id: str,
        mp4_open_us: int,
        prime_us: int,
        total_us: int,
    ) -> None:
        with self._lock:
            self.video_load_recent.append(
                {
                    "slide_id": slide_id,
                    "mp4_open_us": int(mp4_open_us),
                    "prime_us": int(prime_us),
                    "total_us": int(total_us),
                    "ts": _utc_now_iso(),
                }
            )

    def record_begin_slide_load(self, slide_id: str, load_us: int) -> None:
        with self._lock:
            self.begin_slide_load_recent.append(
                {"slide_id": slide_id, "load_us": int(load_us), "ts": _utc_now_iso()}
            )

    def record_begin_transition_load(self, slide_id: str, load_us: int) -> None:
        with self._lock:
            self.begin_transition_load_recent.append(
                {"slide_id": slide_id, "load_us": int(load_us), "ts": _utc_now_iso()}
            )

    def record_frame_observation(self, delta_ms: int, in_transition: bool) -> None:
        """Called for EVERY frame budget observation. Increments
        the total counter unconditionally; only appends to the
        rolling buffer when delta_ms >= _FRAME_OVER_BUDGET_MIN_MS
        (per dispatch: too many small ones).

        r64 subagent (NIT fix): doc matched against the code's >=
        comparison (was '> 100' which suggested 100 itself was
        excluded; the code includes 100)."""
        with self._lock:
            self.frames_observed_total += 1
            if delta_ms > 0:
                self.frame_over_budget_total += 1
                if delta_ms >= _FRAME_OVER_BUDGET_MIN_MS:
                    self.frame_over_budget_recent.append(
                        {
                            "delta_ms": int(delta_ms),
                            "in_transition": bool(in_transition),
                            "ts": _utc_now_iso(),
                        }
                    )

    def record_transition_observation(self) -> None:
        with self._lock:
            self.transitions_total += 1

    def record_cma_sample(self, used_mb: float) -> None:
        """Append a CMA sample to the 1-minute rolling window.
        Called by the background sampler task at _CMA_SAMPLE_INTERVAL_S
        cadence."""
        ts = time.monotonic()
        with self._lock:
            self._cma_samples.append((ts, float(used_mb)))

    # ---- snapshot (read side) -----------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the entire
        rolling state. Single lock acquisition; O(N) in the
        total sample count where N is bounded by
        _MAX_SAMPLES × number-of-kinds = ~350 entries.

        Called from the FastAPI handler. Holds the lock for the
        copy duration only -- ~tens of microseconds."""
        with self._lock:
            uptime_s = int(time.monotonic() - self._start_time)
            # Compute CMA 1-minute aggregates if we have samples.
            cma_values = [v for (_, v) in self._cma_samples]
            cma_max = max(cma_values) if cma_values else None
            cma_avg = sum(cma_values) / len(cma_values) if cma_values else None
            return {
                "timestamp": _utc_now_iso(),
                "uptime_s": uptime_s,
                "frames_observed_total": self.frames_observed_total,
                "frame_over_budget_total": self.frame_over_budget_total,
                "transitions_total": self.transitions_total,
                "cma_used_max_mb_1m": cma_max,
                "cma_used_avg_mb_1m": cma_avg,
                "cma_window_samples": len(self._cma_samples),
                "preload_recent": list(self.preload_recent),
                "first_frame_paint_recent": list(self.first_frame_paint_recent),
                "v4l2_prime_recent": list(self.v4l2_prime_recent),
                "video_load_recent": list(self.video_load_recent),
                "frame_over_budget_recent": list(self.frame_over_budget_recent),
                "begin_slide_load_recent": list(self.begin_slide_load_recent),
                "begin_transition_load_recent": list(self.begin_transition_load_recent),
            }


# Module-level singleton. Every backend caller imports `STATS`
# directly -- no DI, no fixture plumbing. Tests can monkeypatch
# this attribute to substitute an isolated instance.
STATS = PerfStats()


# ----------------------------------------------------------------
# `[perf]` stderr line parser (called from rust_renderer.py)
# ----------------------------------------------------------------

# Captures: `[perf] <kind> <kvs>` where kvs is `key=value` pairs
# separated by spaces. The legacy `frame over budget` line uses a
# colon and a space-separated kv tail; handled by a sibling regex.
_PERF_LINE_RE = re.compile(r"\[perf\]\s+(\S+)\s+(.*)$")
_KV_RE = re.compile(r"(\w+)=(\S+)")
# Renderer-side `[perf] frame over budget: delta_ms=N
# in_transition=B ...` (sample line from the sidecar). The kind
# here is two words; treat specially. r64 subagent (WARN fix):
# colon is optional -- guards against future renderer log-format
# drift dropping the colon and the line silently falling into
# the generic regex (where kind would capture only "frame" and
# the recognized-but-unrouted line would be lost).
_FRAME_OVER_BUDGET_RE = re.compile(r"\[perf\]\s+frame over budget:?\s+(.*)$")


def parse_and_record_perf_line(line: str) -> bool:
    """Inspect a stderr line; if it carries `[perf] ...`, parse
    it into STATS and return True. Otherwise return False so the
    caller can fall through to its normal logging path.

    The caller (`rust_renderer._drain_stderr`) should NOT skip
    logging on a True return -- the journal still gets the line
    for narrative context; we just additionally push the parsed
    fields into STATS so the cheap endpoint can serve them.

    Defensive: any parse error is swallowed (return True if the
    line clearly carried `[perf]` but couldn't be fully parsed,
    so the caller doesn't double-handle; the running counters
    just don't get updated). One log.debug for diagnosability.
    """
    if "[perf]" not in line:
        return False

    # Renderer's `[perf] frame over budget` lines are recognized
    # (so the line counts as "handled") but DO NOT push into
    # STATS -- the backend tick path (_record_tick in playback.py)
    # is the single source of truth for frame observation
    # counters. r64 subagent (BLOCKER fix): double-ingest would
    # bump frames_observed_total + frame_over_budget_total twice
    # per actual over-budget frame (once from backend tick, once
    # from renderer stderr) and add duplicate rolling-buffer
    # entries with mismatched delta_ms values. Backend tick is
    # the cheaper + more authoritative source.
    if _FRAME_OVER_BUDGET_RE.search(line):
        return True

    m = _PERF_LINE_RE.search(line)
    if not m:
        return False
    kind = m.group(1)
    rest = m.group(2)
    try:
        kvs = dict(_KV_RE.findall(rest))
        if kind == "preload":
            slide_id = kvs.get("slide_id", "")
            us = int(kvs.get("us", "0"))
            STATS.record_preload(slide_id, us)
        elif kind == "first_frame_paint":
            STATS.record_first_frame_paint(
                slide_id=kvs.get("slide_id", ""),
                bake_us=int(kvs.get("bake_us", "0")),
                composite_us=int(kvs.get("composite_us", "0")),
                present_us=int(kvs.get("present_us", "0")),
                scanout_us=int(kvs.get("scanout_us", "0")),
                total_us=int(kvs.get("total_us", "0")),
                cache_hit=kvs.get("cache_hit", "false").lower() == "true",
            )
        elif kind == "v4l2_prime":
            # Pass-through: keep whatever sub-phase keys the
            # renderer emitted. Coerce numeric-looking values to
            # int so the JSON is cleaner; dims stays string.
            parsed: dict[str, Any] = {}
            for k, v in kvs.items():
                if v.isdigit():
                    parsed[k] = int(v)
                else:
                    parsed[k] = v
            STATS.record_v4l2_prime(**parsed)
        elif kind == "video_load":
            STATS.record_video_load(
                slide_id=kvs.get("slide_id", ""),
                mp4_open_us=int(kvs.get("mp4_open_us", "0")),
                prime_us=int(kvs.get("prime_us", "0")),
                total_us=int(kvs.get("total_us", "0")),
            )
        elif kind == "begin_slide_load":
            STATS.record_begin_slide_load(
                slide_id=kvs.get("slide_id", ""),
                load_us=int(kvs.get("load_us", "0")),
            )
        elif kind == "begin_transition_load":
            STATS.record_begin_transition_load(
                slide_id=kvs.get("slide_id", ""),
                load_us=int(kvs.get("load_us", "0")),
            )
        else:
            # Unknown perf kind: don't push to STATS but consider
            # the line handled so the caller doesn't double-log.
            # New perf line shapes added in the renderer will need
            # a sibling branch here; until then they still appear
            # in the journal via the caller's existing log.info.
            return True
    except (ValueError, TypeError) as e:
        log.debug("perf_stats: parse failed: %s (line=%r)", e, line)
    return True


# ----------------------------------------------------------------
# CMA background sampler
# ----------------------------------------------------------------


def read_cma_used_mb() -> float | None:
    """Read /proc/meminfo and compute CmaUsed in MB. Returns None
    if the file isn't present (e.g. macOS dev) or if CmaTotal /
    CmaFree fields are missing.

    Cheap: one open + small read + dict parse. Linux page cache
    serves the meminfo data so the call is sub-millisecond.
    """
    try:
        text = _MEMINFO_PATH.read_text()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    cma_total = None
    cma_free = None
    for raw_line in text.splitlines():
        if ":" not in raw_line:
            continue
        key, _, val = raw_line.partition(":")
        key = key.strip()
        if key == "CmaTotal":
            cma_total = _parse_kb(val)
        elif key == "CmaFree":
            cma_free = _parse_kb(val)
    if cma_total is None or cma_free is None or cma_total == 0:
        return None
    used_kb = cma_total - cma_free
    return used_kb / 1024.0  # MB


def _parse_kb(val: str) -> int | None:
    """Parse 'N kB' style meminfo values."""
    parts = val.strip().split()
    if not parts:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


async def cma_sampler_task() -> None:
    """Long-running asyncio task that samples /proc/meminfo every
    _CMA_SAMPLE_INTERVAL_S and feeds STATS.record_cma_sample.

    Started from app.lifespan. Stops cleanly on CancelledError
    (lifespan shutdown). Errors are caught + logged so a transient
    meminfo read failure doesn't kill the sampler -- next tick
    retries.
    """
    import asyncio

    log.info(
        "perf_stats: CMA sampler starting (interval=%.1fs, window=%ds)",
        _CMA_SAMPLE_INTERVAL_S,
        int(_CMA_SAMPLE_INTERVAL_S * _CMA_WINDOW_SAMPLES),
    )
    try:
        while True:
            try:
                used = read_cma_used_mb()
                if used is not None:
                    STATS.record_cma_sample(used)
            except Exception:
                log.debug("perf_stats: CMA sample failed", exc_info=True)
            await asyncio.sleep(_CMA_SAMPLE_INTERVAL_S)
    except asyncio.CancelledError:
        log.info("perf_stats: CMA sampler cancelled (lifespan shutdown)")
        raise
