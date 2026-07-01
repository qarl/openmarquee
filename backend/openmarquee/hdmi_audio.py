"""HDMI audio helper — spawn ffmpeg piping a VideoSlide's audio track to
the sign's vc4hdmi ALSA card.

Design (per qa/hdmi-audio-build-brief-2026-07-01):
    - **Fire-and-forget ALSA slave at native rate, start-aligned to the
      clip, hard-cut on every boundary.** Master clock stays the Python
      playback wall clock; no PTS feedback + no resample to video clock.
    - **Fail-safe to silence.** Every spawn/kill is wrapped in
      log-only try/except; audio must NEVER block or raise into the
      playback advance path. Video is priority; audio is a bonus.
    - **Startup probe (once).** Parse ``aplay -L`` for a vc4hdmi card
      BY NAME. If absent, disable audio globally and log once (no
      per-clip spawn churn). Never a hardcoded card index — the
      device string can shift across kernel/config changes.
    - **Startup sweep.** Kill any stray openmarquee audio helpers at
      boot. Prevents R6 (an un-reaped helper from a previous run
      holding ALSA so the next clip can't open the device).

The helper process is spawned with ``start_new_session=True`` so it
lives in its own process group. Kills use ``os.killpg`` — this catches
ffmpeg + any children it spawns.

Not touched intentionally:
    - Rust renderer / mp4_demux / V4L2. Per brief: that path is
      security-hardened, CMA-tight, at the hardware ceiling. Audio
      lives entirely in Python + a spawned ffmpeg subprocess.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import ClassVar
from uuid import UUID

log = logging.getLogger(__name__)


# argv[0] sentinel used both when spawning helpers AND when sweeping
# stray helpers at boot. Popen's `executable=` arg keeps the actual
# binary as `ffmpeg` while argv[0] carries this marker; `pgrep -f`
# then matches it in /proc/PID/cmdline. Distinct enough that a
# system-wide grep can't confuse it with an operator-run ffmpeg.
_HELPER_ARGV0 = "openmarquee-hdmi-audio-helper"

# Absolute path to the ffmpeg binary. Popen's `executable=` needs the
# real path so a distinct argv[0] doesn't confuse the loader.
# Standard Raspberry Pi OS ships ffmpeg at /usr/bin/ffmpeg.
_FFMPEG_PATH = "/usr/bin/ffmpeg"


class HdmiAudioHelper:
    """Per-clip ffmpeg-to-ALSA helper manager.

    Owned by ``PlaybackLoop``. All methods are safe to call from the
    async event loop (no blocking IO except the initial probe, which
    runs once at ``initialize()`` under a 5s timeout, and the subprocess
    reap on stop, which is bounded to ~2s worst case).

    The class is intentionally a thin wrapper around subprocess.Popen —
    the important guarantees are:
        1. At most one helper alive at a time (``_current`` tracks it).
        2. ``stop_current`` is idempotent + safe on already-dead procs.
        3. Every spawn/kill path is wrapped in try/except; failures
           demote to silence, never propagate.
        4. The startup probe determines ``_device_name`` once. If None
           (no vc4hdmi card found), all subsequent ``start_for_slide``
           calls are cheap no-ops.
    """

    #: Reap timeout after SIGTERM before escalating to SIGKILL. 1s is
    #: comfortable — ffmpeg's default ALSA close is sub-100ms.
    _REAP_TIMEOUT_S: ClassVar[float] = 1.0

    #: ``aplay -L`` timeout for the startup probe.
    _PROBE_TIMEOUT_S: ClassVar[float] = 5.0

    def __init__(self, content_root: Path) -> None:
        self._content_root = Path(content_root)
        self._current: subprocess.Popen[bytes] | None = None
        # None BEFORE initialize; None AFTER means probe found no card
        # (audio globally disabled). A non-empty string means audio
        # ready to spawn to that ALSA device name.
        self._device_name: str | None = None
        self._initialized = False

    # ---- public lifecycle ---------------------------------------------

    def initialize(self) -> None:
        """Startup probe + sweep. Idempotent; safe to call again.

        Runs synchronously. The ``aplay -L`` subprocess is bounded by
        ``_PROBE_TIMEOUT_S``. Failures demote to silence + log-once.
        """
        if self._initialized:
            return
        self._initialized = True
        # Order matters slightly: sweep first (clears any stale ALSA
        # device holds from a prior openmarquee crash) THEN probe (the
        # probe just parses text — doesn't open the device). The
        # reverse order would technically also work, but "clean the
        # room before you check for a chair" reads better.
        self._startup_sweep()
        self._device_name = self._probe_alsa_card()
        if self._device_name is None:
            log.info(
                "hdmi-audio: no vc4hdmi ALSA card detected; audio "
                "globally disabled (silent playback)",
            )
        else:
            log.info(
                "hdmi-audio: initialized with ALSA device %r",
                self._device_name,
            )

    def start_for_slide(self, slide_id: UUID) -> None:
        """Spawn ffmpeg for the given slide's ``asset.mp4`` audio track.

        Kills any currently-running helper first (belt+suspenders — the
        loop normally calls ``stop_current`` on iteration boundaries,
        but a stray helper here would prevent the new one from opening
        ALSA and the operator would hear the wrong clip). Silent if
        audio is globally disabled OR the slide has no ``asset.mp4``
        on disk yet.

        The ``-stream_loop -1`` flag loops the clip so a duration-
        mismatch between video (backend wall clock) and audio (clip
        length) doesn't produce dead air INSIDE a slide hold. Boundary
        transitions cut cleanly via ``stop_current``.
        """
        # Belt+suspenders: kill any prior helper. The normal loop flow
        # calls stop_current on iteration boundaries, but defense-in-
        # depth means we can't assume it did.
        self.stop_current()
        if self._device_name is None:
            return
        asset_path = self._content_root / str(slide_id) / "asset.mp4"
        if not asset_path.exists():
            log.debug(
                "hdmi-audio: no asset.mp4 for slide %s at %s (skipping)",
                slide_id,
                asset_path,
            )
            return
        try:
            proc = subprocess.Popen(  # noqa: S603
                [
                    _HELPER_ARGV0,
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-i",
                    str(asset_path),
                    "-vn",
                    "-stream_loop",
                    "-1",
                    "-f",
                    "alsa",
                    self._device_name,
                ],
                executable=_FFMPEG_PATH,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Own process group so killpg reaches ffmpeg + any
                # children (e.g. libavformat helper threads spawned
                # for network URLs, though not applicable to local
                # asset.mp4 files).
                start_new_session=True,
                close_fds=True,
            )
            self._current = proc
            log.debug(
                "hdmi-audio: spawned helper pid=%d for slide %s",
                proc.pid,
                slide_id,
            )
        except Exception as e:  # noqa: BLE001 — fail-safe to silence
            log.warning(
                "hdmi-audio: spawn failed for slide %s (silent playback for this clip): %s",
                slide_id,
                e,
            )
            self._current = None

    def stop_current(self) -> None:
        """Kill + reap the current helper. Idempotent + safe."""
        proc = self._current
        if proc is None:
            return
        self._current = None
        pid = proc.pid
        # Kill the entire process group. ffmpeg's ALSA close is fast
        # enough that SIGTERM usually suffices; the SIGKILL fallback
        # below handles the pathological wedged case.
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as e:
            # Already dead / adopted by init — proceed to wait+reap.
            log.debug(
                "hdmi-audio: SIGTERM to pid=%d process group failed (may be already dead): %s",
                pid,
                e,
            )
        try:
            proc.wait(timeout=self._REAP_TIMEOUT_S)
            log.debug("hdmi-audio: reaped pid=%d cleanly", pid)
            return
        except subprocess.TimeoutExpired:
            log.warning(
                "hdmi-audio: pid=%d did not exit within %.1fs of SIGTERM; escalating to SIGKILL",
                pid,
                self._REAP_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001 — fail-safe
            log.warning("hdmi-audio: wait after SIGTERM failed: %s", e)
        # SIGKILL escalation.
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=self._REAP_TIMEOUT_S)
            log.debug("hdmi-audio: SIGKILL reaped pid=%d", pid)
        except (
            ProcessLookupError,
            PermissionError,
            subprocess.TimeoutExpired,
        ) as e:
            # Nothing more to do — the OS will reap eventually, or
            # the process was already gone. Log for post-mortem.
            log.warning(
                "hdmi-audio: SIGKILL/final-wait for pid=%d failed (may leak): %s",
                pid,
                e,
            )

    # ---- private helpers ----------------------------------------------

    def _probe_alsa_card(self) -> str | None:
        """Parse ``aplay -L`` for a vc4hdmi device (BY NAME) and return
        an ffmpeg-compatible device string that sets IEC958 channel
        status (required by the vc4 HDMI sink).

        Returns e.g. ``"hdmi:CARD=vc4hdmi,DEV=0"`` or None if no
        vc4hdmi card is present.

        **PR#22 review (2026-07-01)**: QA glass-tested plughw: vs
        hdmi: on the live sign. plughw:CARD=vc4hdmi,DEV=0 caused
        audible warble/glitch because plughw doesn't set IEC958
        channel-status; the vc4 HDMI sink needs it. hdmi:CARD=vc4hdmi
        (and default:CARD=vc4hdmi) do. aplay -D hdmi:CARD=vc4hdmi,
        DEV=0 drained a 20s tone cleanly.

        Ingest normalizes all audio to -ar 48000 (matches HDMI native
        rate), so hdmi: needing native rate is safe — no resample
        path is exercised.

        Probe strategy: any aplay -L definition line naming vc4hdmi
        (plughw:, hw:, sysdefault:, etc.) tells us the card exists.
        We then construct the ``hdmi:CARD=<name>,DEV=0`` string
        ourselves rather than requiring aplay -L to LIST an ``hdmi:``
        line (which depends on the ALSA config's PCM definitions
        and isn't always present).
        """
        try:
            result = subprocess.run(  # noqa: S603, S607
                ["aplay", "-L"],
                capture_output=True,
                text=True,
                timeout=self._PROBE_TIMEOUT_S,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            log.warning(
                "hdmi-audio: aplay -L probe failed (%s); audio disabled",
                e,
            )
            return None
        if result.returncode != 0:
            log.warning(
                "hdmi-audio: aplay -L exit=%d stderr=%r; audio disabled",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
            return None
        # ``aplay -L`` output shape: definitions are top-level lines
        # (no leading whitespace) followed by 1..N indented human-
        # readable description lines. Example excerpt on a Zero 2 W:
        #     plughw:CARD=vc4hdmi,DEV=0
        #         vc4-hdmi, MAI PCM vc4-hdmi-hifi-0
        #         Hardware device with all software conversions
        # We scan for ANY top-level definition line mentioning
        # vc4hdmi to confirm the card exists.
        card_present = False
        for raw in result.stdout.splitlines():
            line = raw.rstrip()
            if not line or line.startswith(" ") or line.startswith("\t"):
                continue
            if "vc4hdmi" in line.lower():
                card_present = True
                break
        if not card_present:
            log.info(
                "hdmi-audio: aplay -L emitted no vc4hdmi device; audio disabled",
            )
            return None
        # Construct the hdmi: device string ourselves — QA glass-
        # verified `hdmi:CARD=vc4hdmi,DEV=0` opens cleanly + sets
        # IEC958 channel-status. plughw: does NOT and causes audible
        # warble on the vc4 sink.
        return "hdmi:CARD=vc4hdmi,DEV=0"

    def _startup_sweep(self) -> None:
        """Kill any stray openmarquee audio helpers at boot.

        Uses ``pgrep -f`` to match on argv[0] (see ``_HELPER_ARGV0``).
        A stray helper from a prior openmarquee crash can hold the ALSA
        device open — the next clip's ffmpeg then fails to open the
        device and audio goes silent. This runs once at initialize().

        Silent no-op if pgrep isn't installed or matches nothing.
        Failures never propagate; audio initialization proceeds.
        """
        try:
            result = subprocess.run(  # noqa: S603, S607
                ["pgrep", "-f", _HELPER_ARGV0],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            log.debug("hdmi-audio: startup sweep pgrep failed: %s", e)
            return
        if result.returncode != 0:
            # pgrep returns 1 when nothing matched — the common case.
            return
        pids: list[int] = []
        for token in result.stdout.split():
            try:
                pids.append(int(token))
            except ValueError:
                continue
        if not pids:
            return
        log.info(
            "hdmi-audio: startup sweep — killing %d stray helper(s): %s",
            len(pids),
            pids,
        )
        for pid in pids:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError) as e:
                log.debug(
                    "hdmi-audio: sweep SIGTERM pid=%d failed (ok): %s",
                    pid,
                    e,
                )
