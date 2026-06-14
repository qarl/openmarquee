"""Journal-line analyzer for the renderer's preload + transition probes.

2026-06-13 FYS regression follow-up: the offscreen-capture golden test in
PR #2 covered the dispatch-path bug but did NOT exercise the LIVE
PaintTransition dual-decoder path that produced the BLACK outgoing video
under ``OPENMARQUEE_PRELOAD_MODE=max``. This module is the host-portable
piece of the live-regression coverage — it takes journalctl output (or
any line stream from the renderer's stderr-style ``[perf]`` probes) and
classifies the run.

The companion shell driver at
``renderer/tests/scripts/run_live_preload_contention.sh`` exercises the
full backend + renderer pipeline on a Pi-class Linux box, pipes the
journal output through this module, and asserts the contract:

  * Under ``OPENMARQUEE_PRELOAD_MODE=defer`` (production), the
    starvation signature is absent — zero ``endpoint_a_no_frame``
    skip-tick warnings and the per-handoff ``frames_drained`` is ≥ 1.
  * Under ``OPENMARQUEE_PRELOAD_MODE=max`` (experiment), the starvation
    signature appears — non-zero ``endpoint_a_no_frame`` count OR
    ``frames_drained=0 was_deferred=false`` (codec actually stalled,
    not a graceful r97 defer).

Decoupling the parser from the driver makes the contract unit-testable
in CI (host-portable) AND lets QA run the live driver on any Pi.

Probe lines this parser recognises (every one is a substring match
against a single journal line):

  ``warn: paint_transition_skip kind=<K> progress=<P> reason=endpoint_a_no_frame``
    Renderer skipped a transition tick because endpoint A's bake
    returned ``Ok(None)``. THE FROM-side starvation signature.

  ``warn: paint_transition_skip kind=<K> progress=<P> reason=endpoint_b_no_frame``
    Symmetric B-side skip. Common under cold-start (B's decoder
    hasn't reached first frame); useful as a denominator but not a
    failure on its own.

  ``[perf] preload_handoff slide_id=<UUID> frames_drained=<N> ... was_deferred=<bool>``
    Fired by ``prime_video_decoder_for_preload``'s 5 call sites.
    ``was_deferred=true`` means the r97 contention guard fired and
    preload was skipped intentionally; ``was_deferred=false`` is the
    normal handoff and ``frames_drained`` should be ≥ 1 — a 0 there
    means the codec actually stalled and is the original dual-1080p
    failure mode that motivated r97.

  ``[perf] preload_deferred_for_codec_contention``
    Counter of times the r97 defer guard fired. Under
    ``PRELOAD_MODE=max`` this MUST be zero (max bypasses defer); under
    ``PRELOAD_MODE=defer`` with an all-text-over-video playlist it
    fires once per transition.

  ``[perf] bake_b_poll_outcome ... result=deadline_exhausted``
    r94 Path B exhausted its consumer-side deadline polling for
    bake_b. Different failure mode than the FROM-side bake_a starvation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# Substring matchers — the renderer prints free-form ``eprintln!`` lines,
# not structured JSON. Keep the matches tolerant to format-string drift
# inside the kind/progress/etc fields.

_ENDPOINT_A_SKIP_RE = re.compile(r"\bpaint_transition_skip\b.*\bendpoint_a_no_frame\b")
_ENDPOINT_B_SKIP_RE = re.compile(r"\bpaint_transition_skip\b.*\bendpoint_b_no_frame\b")
_PRELOAD_HANDOFF_RE = re.compile(
    r"\[perf\]\s+preload_handoff\s+slide_id=(?P<slide>\S+)\s+"
    r"frames_drained=(?P<drained>\d+).*?was_deferred=(?P<def>true|false)"
)
_DEFERRED_FOR_CONTENTION_RE = re.compile(r"\[perf\]\s+preload_deferred_for_codec_contention\b")
_BAKE_B_DEADLINE_RE = re.compile(r"\[perf\]\s+bake_b_poll_outcome\b.*\bresult=deadline_exhausted\b")
_PRELOAD_MODE_WARN_RE = re.compile(
    r"OPENMARQUEE_PRELOAD_MODE=['\"]?(?P<mode>defer|lead|max)['\"]?"
    r"\s+is an EXPERIMENT-ONLY knob"
)


@dataclass
class PreloadJournalSummary:
    """Classification of a single journal capture window."""

    # Counters — every match increments by 1.
    endpoint_a_no_frame: int = 0
    endpoint_b_no_frame: int = 0
    preload_handoff_normal: int = 0  # was_deferred=false
    preload_handoff_deferred: int = 0  # was_deferred=true
    preload_handoff_frames_drained_zero_normal: int = 0  # ⚠ codec stalled
    deferred_for_codec_contention: int = 0
    bake_b_deadline_exhausted: int = 0

    # Did the Python-side WARN message fire? (set when MODE=lead or max)
    experiment_warning_modes: set[str] = field(default_factory=set)

    # Per-handoff drained counts so the test can compute median etc.
    handoff_drained_counts: list[int] = field(default_factory=list)

    def is_starvation_signature_present(self) -> bool:
        """True when the LIVE FROM-side starvation pattern is in the log.

        The 2026-06-13 FYS regression's smoking gun is a non-zero
        endpoint_a_no_frame count. ``frames_drained=0 was_deferred=false``
        is the dual-1080p codec-stall pattern from r97's commit body and
        also counts because it's the same starvation class.
        """
        return self.endpoint_a_no_frame > 0 or self.preload_handoff_frames_drained_zero_normal > 0

    def production_clean(self) -> bool:
        """True when the run looks like a healthy production sign.

        Inverse of ``is_starvation_signature_present`` plus a sanity
        check that we actually saw *some* preload activity — an empty
        capture window is not a pass.
        """
        if self.preload_handoff_normal + self.preload_handoff_deferred == 0:
            return False
        return not self.is_starvation_signature_present()


def classify(lines: Iterable[str]) -> PreloadJournalSummary:
    """Walk one capture window's worth of lines + tally signal counts.

    Tolerant of mixed content — non-renderer lines (other systemd
    units, kernel messages, etc.) are silently skipped. The matchers
    are substring-based so journalctl's ``timestamp host unit: msg``
    framing doesn't break them.
    """
    summary = PreloadJournalSummary()
    for line in lines:
        if _ENDPOINT_A_SKIP_RE.search(line):
            summary.endpoint_a_no_frame += 1
            continue
        if _ENDPOINT_B_SKIP_RE.search(line):
            summary.endpoint_b_no_frame += 1
            continue
        if _DEFERRED_FOR_CONTENTION_RE.search(line):
            summary.deferred_for_codec_contention += 1
            continue
        if _BAKE_B_DEADLINE_RE.search(line):
            summary.bake_b_deadline_exhausted += 1
            continue
        if handoff := _PRELOAD_HANDOFF_RE.search(line):
            drained = int(handoff.group("drained"))
            was_deferred = handoff.group("def") == "true"
            summary.handoff_drained_counts.append(drained)
            if was_deferred:
                summary.preload_handoff_deferred += 1
            else:
                summary.preload_handoff_normal += 1
                if drained == 0:
                    summary.preload_handoff_frames_drained_zero_normal += 1
            continue
        if warn := _PRELOAD_MODE_WARN_RE.search(line):
            summary.experiment_warning_modes.add(warn.group("mode"))
    return summary


def assert_production_clean(summary: PreloadJournalSummary) -> None:
    """Raise AssertionError describing the failure mode if the capture
    looks like the FYS 2026-06-13 regression.

    Used by the live runner shell script. The exact phrasing is part of
    the QA-facing contract — keep it concrete so the failure message
    immediately points at the responsible config knob.
    """
    if not summary.production_clean():
        if summary.preload_handoff_normal + summary.preload_handoff_deferred == 0:
            raise AssertionError(
                "no preload_handoff events seen in capture window — either the "
                "capture window was too short, the renderer never reached a "
                "transition, or no probe lines are flowing. Re-capture for "
                "longer and confirm OPENMARQUEE_LIVE_PREVIEW_PATH gates didn't "
                "swallow stdout."
            )
        raise AssertionError(
            "LIVE STARVATION SIGNATURE DETECTED — this looks like the FYS "
            "2026-06-13 regression. Counters:\n"
            f"  endpoint_a_no_frame                       = "
            f"{summary.endpoint_a_no_frame} (expected 0)\n"
            f"  preload_handoff frames_drained=0 normal   = "
            f"{summary.preload_handoff_frames_drained_zero_normal} (expected 0)\n"
            f"  preload_handoff normal (drained>=1)       = "
            f"{summary.preload_handoff_normal}\n"
            f"  preload_handoff deferred (graceful r97)   = "
            f"{summary.preload_handoff_deferred}\n"
            f"  bake_b_poll_outcome deadline_exhausted    = "
            f"{summary.bake_b_deadline_exhausted}\n"
            f"  preload_deferred_for_codec_contention     = "
            f"{summary.deferred_for_codec_contention}\n"
            f"  experiment_warning_modes seen             = "
            f"{sorted(summary.experiment_warning_modes)}\n"
            "Most likely cause: OPENMARQUEE_PRELOAD_MODE is set to 'max' or "
            "'lead' on the device. Inspect with `sudo systemctl show "
            "openmarquee-backend | grep PRELOAD_MODE` and clean up per "
            "docs/hardware-ceilings.md."
        )
