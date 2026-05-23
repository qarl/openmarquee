// Live panel — phone-camera or stream-transport takeover (SYSTEM_SPEC §5.11 +
// docs/STREAM_VLC_PROPOSAL.md).
//
// A source toggle picks between two takeover transports:
//   - Camera — one-tap "Go Live": open the back camera, negotiate
//     WebRTC against the device's playback engine (aiortc
//     subscriber). The flow below.
//   - VLC stream — paste the RTSP URL the operator's VLC is
//     publishing; the device pulls it (no camera, no WebRTC, no SDP).
// Either way the device's screen takes over with the live feed and
// Stop returns the playlist where it was.
//
// Flow on this side:
//   1. getUserMedia({video:{facingMode:'environment'}, audio:false})
//   2. new RTCPeerConnection(), addTrack for each video track
//   3. createOffer + setLocalDescription
//   4. wait for non-trickle ICE gathering (so the SDP has all candidates
//      baked in — matches the backend's "one round trip" signaling)
//   5. POST /api/live/start with the offer SDP, get back the answer
//   6. setRemoteDescription(answer) → frames flow
//
// On 409 (another phone owns the screen), surface a "Take over"
// affordance instead of "Go Live". Take over hits POST /api/live/takeover
// with the same offer.
//
// Tailscale-lifecycle warning is rendered while live: iOS / Android
// kill background VPNs, so a phone whose Tailscale drops while
// app-switched will silently lose the connection. Documented as a
// Phase 1 known limitation in §5.11.

import {
    effectiveDisplayDims,
    getSettings,
    getLiveStatus,
    startRtspLive,
    startLive,
    stopLive,
    takeoverRtspLive,
    takeoverLive,
} from "./api.js";

// All media constraints in one place so the §5.11 hardware-tier table
// drives them via the basic-tier numbers reported by /api/live/status.
// Defaults match the basic tier; if /status reports a different tier
// at mount time, we'll override.
const DEFAULT_CAPTURE_CONSTRAINTS = {
    video: {
        width: { ideal: 854, max: 1280 },
        height: { ideal: 480, max: 720 },
        frameRate: { max: 30 },
        facingMode: "environment",
    },
    audio: false,
};

// Per-session persistence for the operator's last camera choice. Reads
// at mount-init so a re-mounted panel resumes with the camera the
// operator picked last time, not the spec'd 'environment' default.
// localStorage key namespaced under openmarquee:live:* (consistent
// with the demo bundle's LS_KEY pattern). Best-effort — a private-
// browsing failure or a quota-exceeded write is silently swallowed,
// the panel just runs against the in-memory default for the session.
const FACING_MODE_LS_KEY = "openmarquee:live:facing-mode";
function readFacingModePref() {
    try {
        const v = globalThis.localStorage?.getItem(FACING_MODE_LS_KEY);
        if (v === "user" || v === "environment") return v;
    } catch {
        /* no localStorage available (private browsing, sandbox) */
    }
    return "environment";
}
function writeFacingModePref(value) {
    try {
        globalThis.localStorage?.setItem(FACING_MODE_LS_KEY, value);
    } catch {
        /* quota / disabled — skip */
    }
}

// Per-session persistence for the source mode (phone camera vs VLC
// RTSP stream) and the last-used RTSP URL — same best-effort
// localStorage pattern as the facing-mode pref. A re-mounted panel
// resumes on the operator's last source and doesn't make them re-type
// their VLC address.
const SOURCE_MODE_LS_KEY = "openmarquee:live:source-mode";
const STREAM_URL_LS_KEY = "openmarquee:live:url";
function readSourceModePref() {
    try {
        const v = globalThis.localStorage?.getItem(SOURCE_MODE_LS_KEY);
        if (v === "camera" || v === "vlc") return v;
    } catch {
        /* no localStorage available (private browsing, sandbox) */
    }
    return "camera";
}
function writeSourceModePref(value) {
    try {
        globalThis.localStorage?.setItem(SOURCE_MODE_LS_KEY, value);
    } catch {
        /* quota / disabled — skip */
    }
}
function readRtspUrlPref() {
    try {
        return globalThis.localStorage?.getItem(STREAM_URL_LS_KEY) || "";
    } catch {
        return "";
    }
}
function writeRtspUrlPref(value) {
    try {
        globalThis.localStorage?.setItem(STREAM_URL_LS_KEY, value);
    } catch {
        /* quota / disabled — skip */
    }
}

// Unified StreamHeader (eyebrow + title + variable action button) +
// viewfinder + idle-only paused-playlist row + live-only metrics grid.
// Design reference: design/stream-redesigns-2026-04-29 variants A
// (idle) + E (live). Per qarl's iteration in chat2.md (2026-04-28),
// the action button is the only thing that swaps between modes —
// everything else stays put so the eye doesn't have to chase.
//
// Divergences from the prior Phase 12.2 panel (defaulted, not
// confirmed with qarl yet):
// 1. Camera flip button RESTORED 2026-05-01 per qarl. Was dropped in
//    the 2026-04-29 redesign iteration ("defer source-switching until
//    after Stop"); operator wanted the mid-stream hot-swap path back.
//    Surfaces as a circular icon button overlaid on the viewfinder's
//    top-right corner whenever a local stream is open. Idle/preview:
//    re-getUserMedia, swap srcObject. Live: track.replaceTrack on the
//    active video sender — no PC teardown, no SDP renegotiation, per
//    §5.11. Choice persists in localStorage so the next mount resumes
//    on the same camera.
// 2. Tailscale-foreground warning dropped — operator-known by now,
//    and the redesign doesn't depict it. The §5.11 known-limitation
//    still applies; if QA wants it back, it lands as an inline pill
//    under the live HUD.
// 3. Take-over button restyled to red (matches the Stop button's
//    semantics — it's a destructive-to-the-other-publisher action,
//    not a primary path).
// 4. Mocked metrics for latency/bitrate/dropped (Phase A.1 per QA's
//    handoff). Real-elapsed ticks against state.startedAt. Phase B
//    will wire RTCPeerConnection.getStats() polling for the rest.
const SECTION_TEMPLATE = `
    <section class="live">
        <header class="live-header">
            <div class="live-header-text">
                <span class="live-header-eyebrow">Live · this device's camera</span>
                <h1 class="live-header-title">Live</h1>
                <p class="live-header-blurb">
                    Push the camera on this device straight to your sign.
                    The active playlist pauses while you broadcast and
                    picks up where it left off.
                </p>
            </div>
            <div class="live-header-action">
                <button type="button" class="om-btn primary live-go-live">
                    <span class="live-go-live-dot" aria-hidden="true"></span>
                    Go live
                </button>
                <button type="button" class="om-btn primary live-start-vlc" hidden>
                    <span class="live-go-live-dot" aria-hidden="true"></span>
                    Start streaming
                </button>
                <button type="button" class="om-btn live-stop" hidden>
                    <span class="live-stop-square" aria-hidden="true"></span>
                    Stop
                </button>
                <button type="button" class="om-btn live-take-over" hidden>
                    Take over
                </button>
                <button type="button" class="om-btn ghost live-cancel-takeover" hidden>
                    Cancel
                </button>
            </div>
        </header>

        <div class="live-source-toggle" role="radiogroup" aria-label="Live source">
            <button type="button" class="live-source-opt is-selected" role="radio"
                    aria-checked="true" data-source="camera">Camera</button>
            <button type="button" class="live-source-opt" role="radio"
                    aria-checked="false" data-source="vlc">Stream</button>
        </div>

        <div class="live-stage">
            <div class="live-preview-wrap">
                <video class="live-preview" autoplay muted playsinline></video>
                <div class="live-preview-empty">
                    Tap <strong>Go live</strong> to open your camera.
                </div>
                <button type="button" class="live-flip-camera"
                        aria-label="Switch camera (front / back)"
                        title="Switch camera"
                        hidden>
                    <svg viewBox="0 0 24 24" width="20" height="20"
                         fill="none" stroke="currentColor" stroke-width="1.8"
                         stroke-linecap="round" stroke-linejoin="round"
                         aria-hidden="true">
                        <path d="M4 7h3l2-2h6l2 2h3v12H4z"/>
                        <circle cx="12" cy="13" r="3"/>
                        <path d="M9.5 13l-1.5-1.5M14.5 13l1.5 1.5"/>
                    </svg>
                </button>
                <div class="live-live-pill" hidden>
                    <span class="live-live-pill-dot" aria-hidden="true"></span>
                    LIVE
                </div>
            </div>
        </div>

        <div class="live-vlc-panel" hidden>
            <label class="live-vlc-url-label" for="live-vlc-url">Stream URL</label>
            <input type="text" id="live-vlc-url" class="live-vlc-url"
                   placeholder="rtsp://… / rtmp://… / https://….m3u8"
                   autocomplete="off" spellcheck="false" />
            <details class="live-vlc-help">
                <summary>How to publish from VLC</summary>
                <ol>
                    <li>In VLC: Media → Stream…</li>
                    <li>Add your video or playlist, then click Stream.</li>
                    <li>Destination: choose <b>RTSP</b> — port 8554, path <b>/live</b>.</li>
                    <li>Click Stream to start, then paste the rtsp:// URL above.</li>
                </ol>
                <p class="live-vlc-help-note">
                    Your VLC and this sign must be on the same network or tailnet.
                </p>
            </details>
        </div>

        <div class="live-paused-row" hidden>
            <span class="live-paused-row-dot" aria-hidden="true"></span>
            <span>paused while live · resumes <b class="live-paused-row-name">the active playlist</b> when you stop</span>
        </div>

        <div class="live-metrics-grid" hidden>
            <div class="live-metric-cell">
                <div class="live-metric-label">Elapsed</div>
                <div class="live-metric-value" data-metric="elapsed">00:00</div>
            </div>
            <div class="live-metric-cell">
                <div class="live-metric-label">Latency</div>
                <div class="live-metric-value" data-metric="latency">78 ms</div>
            </div>
            <div class="live-metric-cell">
                <div class="live-metric-label">Bitrate</div>
                <div class="live-metric-value" data-metric="bitrate">2.8 Mbps</div>
            </div>
            <div class="live-metric-cell">
                <div class="live-metric-label">Dropped</div>
                <div class="live-metric-value" data-metric="dropped">0</div>
            </div>
        </div>

        <div class="live-status" role="status" aria-live="polite"></div>
    </section>
`;

/**
 * Mount the Live panel into `container`.
 *
 * @param {HTMLElement} container — slot to fill.
 * @param {object} [options] — dependency-injection seams for tests.
 * @param {() => Promise} [options.apiGetStatus]
 * @param {(sdp:string) => Promise} [options.apiStartLive]
 * @param {(url:string) => Promise} [options.apiStartRtspLive]
 * @param {(sdp:string) => Promise} [options.apiTakeoverLive]
 * @param {(url:string) => Promise} [options.apiTakeoverRtspLive]
 * @param {(sessionId:string) => Promise} [options.apiStopLive]
 * @param {(constraints) => Promise<MediaStream>} [options.getUserMedia]
 * @param {() => RTCPeerConnection} [options.createPeerConnection]
 * @param {boolean} [options.simulateOnly] — when true, skip the
 *   WebRTC negotiation and the /api/live/{start,stop,takeover}
 *   round trips entirely. The local-camera preview, state machine,
 *   and Tailscale-foreground warning still all run as in production.
 *   Used by the openmarquee.com/demo bundle, where there's no real
 *   peer to negotiate against.
 * @param {() => Promise} [options.fetchSettings] — used to size the
 *   preview wrap to the device's display aspect ratio so the
 *   operator sees actual cropping pre-Go-Live (mirrors the device-
 *   side renderer's cover-fit at playback time).
 * @returns {{ destroy: () => void, getState: () => string }}
 */
export function mountLivePanel(container, options = {}) {
    const {
        apiGetStatus = getLiveStatus,
        apiStartLive = startLive,
        apiStartRtspLive = startRtspLive,
        apiTakeoverLive = takeoverLive,
        apiTakeoverRtspLive = takeoverRtspLive,
        apiStopLive = stopLive,
        fetchSettings = getSettings,
        getUserMedia = (constraints) =>
            navigator.mediaDevices.getUserMedia(constraints),
        createPeerConnection = () => new RTCPeerConnection(),
        simulateOnly = false,
    } = options;

    container.innerHTML = SECTION_TEMPLATE;

    const stageEl = container.querySelector(".live-stage");
    const previewWrapEl = container.querySelector(".live-preview-wrap");
    const previewEl = container.querySelector(".live-preview");
    const previewEmptyEl = container.querySelector(".live-preview-empty");
    const statusEl = container.querySelector(".live-status");
    const goLiveBtn = container.querySelector(".live-go-live");
    const startVlcBtn = container.querySelector(".live-start-vlc");
    const sourceOptEls = container.querySelectorAll(".live-source-opt");
    const vlcPanelEl = container.querySelector(".live-vlc-panel");
    const vlcUrlEl = container.querySelector(".live-vlc-url");
    const stopBtn = container.querySelector(".live-stop");
    const takeOverBtn = container.querySelector(".live-take-over");
    const cancelTakeoverBtn = container.querySelector(".live-cancel-takeover");
    const flipCameraBtn = container.querySelector(".live-flip-camera");
    const livePillEl = container.querySelector(".live-live-pill");
    const pausedRowEl = container.querySelector(".live-paused-row");
    const metricsGridEl = container.querySelector(".live-metrics-grid");
    const elapsedEl = container.querySelector('[data-metric="elapsed"]');
    const latencyEl = container.querySelector('[data-metric="latency"]');
    const bitrateEl = container.querySelector('[data-metric="bitrate"]');
    const droppedEl = container.querySelector('[data-metric="dropped"]');

    // Pre-fill the RTSP URL field with the operator's last-used VLC
    // address so a re-mount (or a return visit) doesn't make them
    // re-type it.
    vlcUrlEl.value = readRtspUrlPref();

    // Mirror the device's display aspect ratio onto the preview wrap so
    // the operator sees actual cropping (object-fit: cover on the video
    // matches the device-side aiortc subscriber's cover-fit at playback
    // time). Updated on mount and on every openmarquee:settings-updated
    // event so a settings change while the panel is mounted reflects
    // immediately. Falls back to the CSS-default 9/16 (phone-portrait)
    // if /api/settings is unreachable on first load.
    //
    // Rotation: portrait-mounted signs (display_rotation in {90, 270})
    // output height-by-width. effectiveDisplayDims swaps so the
    // preview matches the installed orientation; without it a 1080p
    // panel rotated 90° would still preview 16:9 landscape and the
    // operator's framing wouldn't match what actually displays.
    async function refreshPreviewAspect() {
        try {
            const s = await fetchSettings();
            const dims = effectiveDisplayDims(s);
            if (dims !== null) {
                previewWrapEl.style.setProperty(
                    "--om-live-aspect",
                    `${dims.width} / ${dims.height}`,
                );
            }
        } catch {
            // Non-fatal — the CSS default 9/16 fallback stays in place.
        }
    }
    refreshPreviewAspect();
    function onSettingsUpdated() {
        refreshPreviewAspect();
    }
    document.addEventListener("openmarquee:settings-updated", onSettingsUpdated);

    // Single source of truth for the panel state. Render() reads off it
    // and toggles which controls are visible, so handlers only need to
    // mutate state + call render().
    const state = {
        // "idle" | "requesting-camera" | "preview" | "negotiating" |
        // "live" | "take-over-prompt" | "error"
        //
        // Phase 12.2 followup (qarl 2026-04-29): added 'preview' —
        // local camera is open + rendering into the viewfinder, but
        // no PC + no broadcast yet. Mount-init enters this state on
        // boot when /status is idle; Stop returns here so the next
        // Go Live skips the camera-permission round trip.
        phase: "idle",
        sessionId: null,
        // Active local camera stream (preview source + the track we add
        // to the PC). Null when idle/error/take-over-prompt.
        localStream: null,
        // 'environment' (back) or 'user' (front). Hydrated from
        // localStorage so the operator's last choice survives mount
        // cycles. Re-§5.11: flip is hot-swappable mid-stream via
        // track.replaceTrack — no PC teardown.
        facingMode: readFacingModePref(),
        // "camera" (phone WebRTC) | "vlc" (operator's VLC over RTSP).
        // Hydrated from localStorage so the operator's last source
        // choice survives mount cycles.
        sourceMode: readSourceModePref(),
        // Active RTCPeerConnection. Null between sessions, and always
        // null in VLC mode (RTSP has no peer connection).
        pc: null,
        // User-facing message rendered into .live-status. Reset when
        // a transition clears it.
        message: "",
        // Timestamp of the last 'live' transition (Date.now()). Cleared
        // on stop/error. The metrics-grid Elapsed cell ticks against
        // this once per second while phase === 'live'. Phase A.2: set
        // from the server's wall-clock UTC started_at when /start
        // returns it; falls back to Date.now() under deploy stagger
        // or simulateOnly.
        startedAt: null,
        // Last RTCPeerConnection.getStats() sample, used for bitrate
        // delta calculation (bitrate = bytesSent delta over poll
        // interval). { bytesSent, timestamp } when populated, null
        // before the first poll lands. Reset on every non-live phase.
        // Phase B.1 only polls in non-simulateOnly mode (the simulate
        // path has no real PC + no real frames; mocks stay).
        lastStats: null,
    };

    // 1Hz interval handles. Lives at module scope so render() can
    // clear/start them from any phase transition without leaking.
    // Both timers share the live↔non-live edge: elapsed paints MM:SS
    // off state.startedAt, stats polls pc.getStats() and rewrites the
    // latency/bitrate/dropped cells.
    let elapsedTimer = null;
    let statsTimer = null;
    function formatElapsed(ms) {
        const total = Math.max(0, Math.floor(ms / 1000));
        const m = Math.floor(total / 60);
        const s = total % 60;
        return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    }
    function tickElapsed() {
        if (!state.startedAt || !elapsedEl) return;
        elapsedEl.textContent = formatElapsed(Date.now() - state.startedAt);
    }

    // Phase B.1: extract publisher-side metrics from the live PC's
    // RTCStats report. The phone is the WebRTC publisher (sending
    // video to the device's aiortc subscriber), so we read send-side
    // stats: outbound-rtp for bytesSent, remote-inbound-rtp for
    // round-trip-time + packetsLost (those come back in receiver
    // reports), candidate-pair as a RTT fallback when the remote-
    // inbound-rtp report hasn't arrived yet.
    //
    // Polling cadence is 1Hz to match Elapsed and to keep the bitrate
    // delta meaningful (sub-second deltas amplify jitter). Phase B.2
    // will augment with /api/live/status subscriber-side metrics
    // (frames received, decode latency on the device).
    //
    // Single-track assumption: §5.11 v1 publishes one video track + no
    // audio + no simulcast. The loop's last-write-wins behavior on
    // multiple outbound-rtp/video entries is safe under that
    // assumption. If simulcast ever lands, this needs to aggregate
    // bytesSent across layers.
    function formatBitrateMbps(bps) {
        if (!Number.isFinite(bps) || bps < 0) return null;
        return `${(bps / 1_000_000).toFixed(1)} Mbps`;
    }
    async function pollStats() {
        // simulateOnly + non-live phases bail out before this fires
        // (statsTimer cleared in render()), so state.pc null here is
        // a remount race or a teardown mid-tick — silent return is
        // correct.
        if (!state.pc || state.phase !== "live") return;
        let report;
        try {
            report = await state.pc.getStats();
        } catch {
            // PC closed mid-poll, getStats unsupported, etc. Don't
            // touch the cells — they keep their last value, which
            // is fine for a single skipped tick.
            return;
        }
        let bytesSent;
        let timestamp;
        let packetsLost;
        let rttSec;
        let candidateRttSec;
        for (const stat of report.values()) {
            if (stat.type === "outbound-rtp" && stat.kind === "video") {
                bytesSent = stat.bytesSent;
                timestamp = stat.timestamp;
            } else if (stat.type === "remote-inbound-rtp" && stat.kind === "video") {
                packetsLost = stat.packetsLost;
                rttSec = stat.roundTripTime;
            } else if (
                stat.type === "candidate-pair" &&
                (stat.nominated === true || stat.selected === true)
            ) {
                candidateRttSec = stat.currentRoundTripTime;
            }
        }
        // Latency: prefer the remote-inbound-rtp RTT (end-to-end at
        // the receiver), fall back to candidate-pair RTT (transport
        // only, available immediately) when the receiver report
        // hasn't arrived yet.
        const rtt = Number.isFinite(rttSec) ? rttSec : candidateRttSec;
        if (Number.isFinite(rtt) && latencyEl) {
            latencyEl.textContent = `${Math.round(rtt * 1000)} ms`;
        }
        // Dropped: cumulative packetsLost from the receiver. A non-
        // monotonic value would be a stats glitch; show what we got.
        if (Number.isFinite(packetsLost) && droppedEl) {
            droppedEl.textContent = String(packetsLost);
        }
        // Bitrate: needs two samples for a delta. First poll caches;
        // second-and-onward computes (bytesSent_now - bytesSent_prev)
        // * 8 / (timestamp_now - timestamp_prev) in bps. Stats
        // timestamps are ms (DOMHighResTimeStamp / Performance.now-
        // anchored).
        if (Number.isFinite(bytesSent) && Number.isFinite(timestamp)) {
            if (state.lastStats) {
                const dBytes = bytesSent - state.lastStats.bytesSent;
                const dMs = timestamp - state.lastStats.timestamp;
                if (dMs > 0 && dBytes >= 0) {
                    const bps = (dBytes * 8 * 1000) / dMs;
                    const formatted = formatBitrateMbps(bps);
                    if (formatted && bitrateEl) bitrateEl.textContent = formatted;
                }
            }
            state.lastStats = { bytesSent, timestamp };
        }
    }

    function setMessage(text) {
        state.message = text;
        statusEl.textContent = text;
    }

    function render() {
        // Visibility matrix per phase + source mode. The render is
        // idempotent — call it after every state mutation; the DOM
        // converges.
        const phase = state.phase;
        const isVlc = state.sourceMode === "vlc";
        const isCamera = !isVlc;
        const ready = phase === "idle" || phase === "preview" || phase === "error";

        // Source toggle: reflect the selection, and lock it while a
        // session is starting or live — the source can't be swapped
        // mid-stream.
        const lockToggle =
            phase === "requesting-camera" ||
            phase === "negotiating" ||
            phase === "live";
        for (const opt of sourceOptEls) {
            const selected = opt.dataset.source === state.sourceMode;
            opt.classList.toggle("is-selected", selected);
            opt.setAttribute("aria-checked", selected ? "true" : "false");
            opt.disabled = lockToggle;
        }

        // Camera-mode viewfinder vs VLC-mode URL panel.
        stageEl.hidden = isVlc;
        vlcPanelEl.hidden = isCamera;

        // Action buttons. Camera mode shows Go live; VLC mode shows
        // Start streaming. VLC has no preview phase — its ready set is
        // just idle/error.
        const vlcReady = phase === "idle" || phase === "error";
        goLiveBtn.hidden = !(isCamera && ready);
        startVlcBtn.hidden = !(isVlc && vlcReady);
        stopBtn.hidden = phase !== "live";
        takeOverBtn.hidden = phase !== "take-over-prompt";
        cancelTakeoverBtn.hidden = phase !== "take-over-prompt";

        // Flip-camera button rides on the viewfinder whenever the
        // camera is open — preview + live phases. Hidden in idle (no
        // stream open), take-over-prompt (someone else owns the
        // screen), error (no usable stream), and during the brief
        // requesting-camera + negotiating windows.
        flipCameraBtn.hidden =
            !state.localStream || (phase !== "preview" && phase !== "live");

        // LIVE pill on the viewfinder + metrics grid below it: only
        // while live. The metrics grid is camera-only — VLC has no
        // RTCPeerConnection to read getStats() from. The paused-
        // playlist hint shows in any "ready" phase.
        livePillEl.hidden = phase !== "live";
        metricsGridEl.hidden = !(isCamera && phase === "live");
        pausedRowEl.hidden = !ready;

        // Empty-state cover only when there's no local preview to show.
        previewEmptyEl.hidden = state.localStream !== null;

        // Disable the action buttons during transient phases so a
        // double-tap can't start two sessions.
        goLiveBtn.disabled =
            phase === "requesting-camera" || phase === "negotiating";
        startVlcBtn.disabled = phase === "negotiating";

        // Elapsed + stats timer lifecycle. Both 1Hz, both started on
        // the live transition + cleared on every non-live phase.
        // Camera mode only — VLC has no PC and no metrics grid.
        // simulateOnly's stats timer skips the polling work (no real
        // PC) but keeps the mock cell values intact as the demo
        // payoff — render() doesn't reset them on phase=live so they
        // persist as long as the panel mounts.
        if (phase === "live" && isCamera) {
            if (state.startedAt === null) state.startedAt = Date.now();
            tickElapsed();
            if (elapsedTimer === null) {
                elapsedTimer = setInterval(tickElapsed, 1000);
            }
            if (statsTimer === null && !simulateOnly) {
                pollStats();
                statsTimer = setInterval(pollStats, 1000);
            }
        } else {
            if (elapsedTimer !== null) {
                clearInterval(elapsedTimer);
                elapsedTimer = null;
            }
            if (statsTimer !== null) {
                clearInterval(statsTimer);
                statsTimer = null;
            }
            state.startedAt = null;
            state.lastStats = null;
            if (elapsedEl) elapsedEl.textContent = "00:00";
        }
    }

    // --- WebRTC plumbing ---------------------------------------------------

    async function waitForIceGathering(pc) {
        // Non-trickle ICE: wait until all candidates are baked into the
        // SDP before sending it to the backend. Mirrors aiortc's
        // non-trickle expectation in §5.11.
        if (pc.iceGatheringState === "complete") return;
        await new Promise((resolve) => {
            const onChange = () => {
                if (pc.iceGatheringState === "complete") {
                    pc.removeEventListener("icegatheringstatechange", onChange);
                    resolve();
                }
            };
            pc.addEventListener("icegatheringstatechange", onChange);
        });
    }

    async function openLocalCamera() {
        // Defaults to back-facing on mobile ('environment'), but reads
        // state.facingMode so the operator's last flip choice survives
        // mount cycles. The constraint is a soft hint — on desktops +
        // phones-with-only-one-camera, getUserMedia falls back to
        // whatever's available.
        const constraints = {
            ...DEFAULT_CAPTURE_CONSTRAINTS,
            video: {
                ...DEFAULT_CAPTURE_CONSTRAINTS.video,
                facingMode: state.facingMode,
            },
        };
        const stream = await getUserMedia(constraints);
        previewEl.srcObject = stream;
        state.localStream = stream;
        return stream;
    }

    async function flipCamera() {
        // Idle/preview: re-open getUserMedia with the flipped facingMode,
        //   stop the old tracks, swap srcObject.
        // Live: same getUserMedia, but instead of teardownPC we find the
        //   active video sender and replaceTrack — the publisher stays
        //   connected, no SDP renegotiation, no PC teardown (§5.11).
        // Either way, persist the new choice to localStorage.
        const next = state.facingMode === "environment" ? "user" : "environment";
        const constraints = {
            ...DEFAULT_CAPTURE_CONSTRAINTS,
            video: {
                ...DEFAULT_CAPTURE_CONSTRAINTS.video,
                facingMode: next,
            },
        };
        const newStream = await getUserMedia(constraints);
        const oldStream = state.localStream;
        state.facingMode = next;
        writeFacingModePref(next);
        state.localStream = newStream;
        previewEl.srcObject = newStream;
        // Mid-stream hot-swap: if a PC has active senders, swap the
        // video track in place. Per-§5.11 this is the documented path
        // for camera flips during a live broadcast.
        if (state.pc) {
            const newVideoTrack = newStream.getVideoTracks()[0] ?? null;
            for (const sender of state.pc.getSenders?.() ?? []) {
                if (sender.track && sender.track.kind === "video") {
                    try {
                        await sender.replaceTrack(newVideoTrack);
                    } catch {
                        /* sender shut down between getSenders + replaceTrack */
                    }
                }
            }
        }
        // Stop the old tracks AFTER replaceTrack so the publisher never
        // sees a frame gap. Order matters: stop-then-replace blanks the
        // remote until the new track flows.
        if (oldStream) {
            for (const track of oldStream.getTracks()) {
                try {
                    track.stop();
                } catch {
                    /* track already ended — harmless */
                }
            }
        }
    }

    function teardownPC({ keepCamera = false } = {}) {
        // Capture + null state.pc BEFORE close() so the
        // connectionstatechange listener wired in negotiate() sees a
        // mismatch (state.pc !== pc) and skips its failure-path
        // handling — close() fires a 'closed' event that would
        // otherwise re-enter failTo() and double-render.
        const pc = state.pc;
        state.pc = null;
        if (pc) {
            try {
                pc.close();
            } catch {
                /* close errors aren't actionable */
            }
        }
        // Phase 12.2 followup: stop() → preview keeps the camera open
        // so the operator can re-go-live without another permission
        // round-trip. Default is full teardown (failTo, cancelTakeover,
        // pagehide, destroy) — the camera light should be off any time
        // the panel doesn't intend to show a viewfinder.
        if (!keepCamera && state.localStream) {
            for (const track of state.localStream.getTracks()) {
                try {
                    track.stop();
                } catch {
                    /* track.stop on an already-ended track is harmless */
                }
            }
            state.localStream = null;
            previewEl.srcObject = null;
        }
    }

    async function negotiate({ takeover = false } = {}) {
        // Caller already in "negotiating" phase + has a localStream open.
        const pc = createPeerConnection();
        state.pc = pc;

        // Watch the PC's connection state so the panel reacts to
        // mid-stream drops — Tailscale flap, peer crash, network blip.
        // Without this, the backend times out after ~10s (per §5.11
        // failure modes) but the panel stays in 'live' indefinitely.
        // The state.pc !== pc check makes the listener safe across
        // teardown + restart cycles: teardownPC() nulls state.pc before
        // close(), so the 'closed' event from our own teardown is
        // silently ignored here and only remote-side failures
        // (disconnected / failed) trigger the recovery path.
        pc.addEventListener("connectionstatechange", () => {
            if (state.pc !== pc) return;
            if (state.phase !== "live") return;
            const cs = pc.connectionState;
            if (cs === "failed" || cs === "disconnected") {
                state.sessionId = null;
                failTo(new Error(`Connection ${cs}.`));
            }
        });

        for (const track of state.localStream.getVideoTracks()) {
            pc.addTrack(track, state.localStream);
        }
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        await waitForIceGathering(pc);
        const offerSdp = pc.localDescription.sdp;

        const apiCall = takeover ? apiTakeoverLive : apiStartLive;
        const { session_id, sdp_answer, started_at } = await apiCall(offerSdp);
        await pc.setRemoteDescription({ sdp: sdp_answer, type: "answer" });
        state.sessionId = session_id;
        // Phase A.2: Elapsed counter ticks against the device's
        // authoritative session-start timestamp instead of a phone-
        // local Date.now() — survives a panel re-mount and is correct
        // even if the phone's clock is skewed from the device's. The
        // server's started_at lands as an ISO 8601 string; Date.parse
        // returns epoch ms. Falls through silently to render()'s
        // local-Date.now() fallback if the server is older than the
        // client (deploy-staggered).
        const startedMs = started_at ? Date.parse(started_at) : NaN;
        if (Number.isFinite(startedMs)) state.startedAt = startedMs;
    }

    async function simulateNegotiate() {
        // Demo-mode shortcut: skip the WebRTC negotiation entirely.
        // No PC, no /api/live/{start,takeover} round trip — just
        // mint a local session_id so the rest of the panel's
        // state machine has something to track.
        // The local-camera preview is already wired up by the
        // caller's openLocalCamera() call, so the operator still
        // sees real frames in the panel.
        state.sessionId =
            typeof crypto !== "undefined" && crypto.randomUUID
                ? crypto.randomUUID()
                : `demo-${Date.now()}`;
    }

    // --- Phase handlers ---------------------------------------------------

    async function goLive() {
        // If mount-init is still running, let it finish first. This
        // serializes the two paths so a synchronous Go-Live click
        // (before mount-init's /status round-trip resolves) doesn't
        // race-double-open the camera or fire two pre-flight checks.
        // Once mount-init settles we'll be in preview / take-over-
        // prompt / idle / error and can decide accordingly.
        if (mountInitPromise) {
            try {
                await mountInitPromise;
            } catch {
                /* mount-init swallows its own errors — nothing to handle here */
            }
        }
        // Pre-flight /status check: if another phone owns the screen,
        // skip the camera prompt entirely and surface the take-over UI
        // straight away. Saves the operator a permission dialog they'd
        // dismiss anyway. Skipped when starting from preview — mount-
        // init just ran the same check; the 409-mid-flight handler in
        // the catch block below covers any race that opens up between
        // mount and click.
        if (state.phase === "take-over-prompt") {
            // Mount-init resolved into take-over-prompt while we were
            // awaiting it. The button that fired this handler is now
            // hidden and the UI has already converged — nothing for
            // goLive to do.
            return;
        }
        if (state.phase !== "preview") {
            try {
                const status = await apiGetStatus();
                if (status.state === "active") {
                    state.phase = "take-over-prompt";
                    setMessage("Someone else is streaming to this screen.");
                    render();
                    return;
                }
            } catch {
                // /status failure is non-fatal — try to negotiate anyway,
                // the /start response will tell us the truth.
            }
        }

        try {
            // Open camera if mount-init didn't already (idle/error
            // entry; preview entry already has a live localStream).
            if (state.localStream === null) {
                state.phase = "requesting-camera";
                setMessage("Requesting camera access…");
                render();
                await openLocalCamera();
            }

            state.phase = "negotiating";
            setMessage("Connecting…");
            render();
            if (simulateOnly) {
                await simulateNegotiate();
            } else {
                await negotiate();
            }

            state.phase = "live";
            setMessage("Live.");
            render();
        } catch (err) {
            if (err && err.code === "live_already_active") {
                // Race: nothing was active at /status check time, but
                // another phone hit /start in the gap. Tear down the
                // PC + camera we just opened — Take Over creates fresh
                // ones, and leaking these would leave the camera light
                // on until the orphan stream is GC'd. Same UX as the
                // pre-flight branch from there.
                teardownPC();
                state.phase = "take-over-prompt";
                setMessage("Someone else is streaming to this screen.");
                render();
                return;
            }
            failTo(err);
        }
    }

    async function stopLive() {
        const sessionId = state.sessionId;
        const wasVlc = state.sourceMode === "vlc";
        // Camera mode: keep the camera open + return to preview so the
        // operator can re-go-live without another permission dialog.
        // VLC mode has no camera + no PC — it drops straight to idle.
        teardownPC({ keepCamera: !wasVlc });
        state.sessionId = null;
        // simulateOnly minted the session_id locally — the backend
        // never knew about it, so /api/live/stop has nothing to
        // tear down and would just 404.
        if (!simulateOnly) {
            try {
                if (sessionId) await apiStopLive(sessionId);
            } catch {
                // Stop API failure is non-fatal — the device times
                // out the session on disconnect anyway. Don't block
                // the operator.
            }
        }
        state.phase = wasVlc ? "idle" : "preview";
        setMessage("");
        render();
    }

    async function takeOver() {
        // VLC takeovers re-issue the RTSP start against /takeover; the
        // camera takeover path below is webrtc-only.
        if (state.sourceMode === "vlc") {
            await takeOverVlc();
            return;
        }
        try {
            state.phase = "requesting-camera";
            setMessage("Requesting camera access…");
            render();
            await openLocalCamera();

            state.phase = "negotiating";
            setMessage("Taking over…");
            render();
            if (simulateOnly) {
                await simulateNegotiate();
            } else {
                await negotiate({ takeover: true });
            }

            state.phase = "live";
            setMessage("Live.");
            render();
        } catch (err) {
            failTo(err);
        }
    }

    // --- VLC (RTSP) source handlers ---------------------------------------

    function readVlcUrl() {
        return (vlcUrlEl.value || "").trim();
    }

    async function startVlc() {
        // Let a still-running mount-init settle first, so its /status
        // pre-flight can't race this one (mirrors goLive()).
        if (mountInitPromise) {
            try {
                await mountInitPromise;
            } catch {
                /* mount-init swallows its own errors */
            }
        }
        if (state.phase === "take-over-prompt") {
            // Mount-init resolved into take-over-prompt — another
            // source owns the screen; nothing for startVlc to do.
            return;
        }
        const url = readVlcUrl();
        if (!url) {
            setMessage("Enter the stream URL to publish.");
            return;
        }
        writeRtspUrlPref(url);
        // Pre-flight /status: if another source owns the screen,
        // surface the take-over prompt instead of failing the start.
        try {
            const status = await apiGetStatus();
            if (status.state === "active") {
                state.phase = "take-over-prompt";
                setMessage("Someone else is streaming to this screen.");
                render();
                return;
            }
        } catch {
            // /status failure is non-fatal — try /start anyway; its
            // response (200 or 409) tells us the truth.
        }
        try {
            state.phase = "negotiating";
            setMessage("Connecting to the stream…");
            render();
            if (simulateOnly) {
                await simulateNegotiate();
            } else {
                const { session_id, started_at } =
                    await apiStartRtspLive(url);
                state.sessionId = session_id;
                const startedMs = started_at ? Date.parse(started_at) : NaN;
                if (Number.isFinite(startedMs)) state.startedAt = startedMs;
            }
            state.phase = "live";
            setMessage("Live from the stream.");
            render();
        } catch (err) {
            if (err && err.code === "live_already_active") {
                // Race: nothing active at the /status check, but
                // another source hit /start in the gap.
                state.phase = "take-over-prompt";
                setMessage("Someone else is streaming to this screen.");
                render();
                return;
            }
            failTo(err);
        }
    }

    async function takeOverVlc() {
        const url = readVlcUrl();
        if (!url) {
            setMessage("Enter the stream URL to publish.");
            return;
        }
        writeRtspUrlPref(url);
        try {
            state.phase = "negotiating";
            setMessage("Taking over…");
            render();
            if (simulateOnly) {
                await simulateNegotiate();
            } else {
                const { session_id, started_at } =
                    await apiTakeoverRtspLive(url);
                state.sessionId = session_id;
                const startedMs = started_at ? Date.parse(started_at) : NaN;
                if (Number.isFinite(startedMs)) state.startedAt = startedMs;
            }
            state.phase = "live";
            setMessage("Live from the stream.");
            render();
        } catch (err) {
            failTo(err);
        }
    }

    function switchSourceMode(mode) {
        if (mode !== "camera" && mode !== "vlc") return;
        if (mode === state.sourceMode) return;
        // The toggle is render()-disabled while a session is starting
        // or live; guard here too against a stale click.
        if (
            state.phase === "requesting-camera" ||
            state.phase === "negotiating" ||
            state.phase === "live"
        ) {
            return;
        }
        state.sourceMode = mode;
        writeSourceModePref(mode);
        // Leaving camera mode drops the camera so its light goes off;
        // either way the panel resets to idle (a fresh start).
        teardownPC();
        state.phase = "idle";
        setMessage("");
        render();
    }

    function cancelTakeover() {
        // Defensive: if we got here via the 409-mid-flight branch, an
        // orphan PC + camera stream may still be lingering. Belt and
        // suspenders for the goLive() catch — teardownPC is idempotent
        // when there's nothing to tear down.
        teardownPC();
        state.sessionId = null;
        // Bug 6 fix (2026-05-17): set the cancel flag BEFORE flipping
        // phase. If mountInit() is currently suspended at an await
        // and resumes between these two statements, its post-status
        // gate would otherwise see phase==="idle" (the prior
        // contract-documented state, since cancel hasn't fired yet
        // from that observer's POV) and proceed to revive the panel
        // into "preview". Setting the flag first means a racing
        // mountInit's resumption observes mountInitCancelled=true
        // and bails — regardless of whether it observes the new
        // "idle" phase or the prior "take-over-prompt" phase.
        mountInitCancelled = true;
        // Cancel from a mount-time take-over-prompt drops to plain
        // idle (no viewfinder reopen). Decision-record per QA dispatch
        // M5 closure (2026-05-23):
        //
        //   Path A (chosen) — Cancel = "I want out". Operator's
        //     explicit choice; returning the panel to a clean,
        //     request-driven state respects their intent. Next Go
        //     Live click re-runs the full request flow + reopens
        //     the camera.
        //
        //   Path B (alternative, deferred) — re-run mountInit() to
        //     recheck /api/live/status + reopen the camera if the
        //     prior publisher has since stopped. Trigger to flip:
        //     operators consistently flag the empty-panel-after-
        //     cancel UX as jarring. No such report as of M5
        //     closure. If flipped: the mountInitCancelled latch
        //     above must be reset before the re-invocation, and
        //     the Bug-6 race-fix logic at live-panel.test.js:565+
        //     needs a parallel pass.
        //
        // The no-backend-call contract (cancel never POSTs to
        // /api/live/*) is locked by `test_ui_live_cancel_*` in
        // backend/tests/test_ui_live_cancel_takeover.py. A future
        // "helpful" refactor that adds e.g. /api/live/stop here
        // would 404 noisily (no session exists at cancel time
        // when reached via the mount-time-prompt path); the static
        // lock catches that on commit.
        state.phase = "idle";
        setMessage("");
        render();
    }

    function failTo(err) {
        teardownPC();
        state.sessionId = null;
        state.phase = "error";
        setMessage(`Stream failed: ${err?.message || err}`);
        render();
    }

    // --- Mount-time pre-flight + camera open ------------------------------

    // Phase 12.2 followup (qarl 2026-04-29): open the camera at mount
    // so the operator can compose framing/lighting before clicking
    // Go live. /status pre-flight first — if another phone owns the
    // screen, surface take-over-prompt without prompting for camera
    // permission (the operator hasn't yet decided whether to take
    // over). On mount-init failure (status unreachable, getUserMedia
    // denied, no camera device) the panel falls back to plain idle;
    // clicking Go live re-runs the full request flow.
    let destroyed = false;
    // Promise tracking the mount-init pass; goLive awaits this so a
    // synchronous Go-Live click can't race-double-open the camera or
    // fire two /status pre-flights. Settled (set to null) when mount-
    // init returns either via success or via a swallowed error.
    let mountInitPromise = null;
    // Bug 6 (live-panel cancelTakeover race, 2026-05-17): once the
    // operator clicks Cancel from a 409-mid-flight take-over-prompt,
    // a still-pending mountInit() must NOT walk back through "idle"
    // → "requesting-camera" → "preview" and silently revive the
    // panel. The L817-823 contract is "operator chose Cancel = I
    // want out"; this flag latches that decision so mountInit's
    // resumption (after its awaited apiGetStatus / openLocalCamera
    // microtasks resolve) bails. Per-handle scope, never reset
    // within a handle's lifecycle — a fresh mount yields a fresh
    // flag.
    let mountInitCancelled = false;
    async function mountInit() {
        let status;
        try {
            status = await apiGetStatus();
        } catch {
            // /status unreachable: stay in idle. goLive() will retry
            // on first click and handle its own errors from there.
            return;
        }
        if (destroyed || mountInitCancelled || state.phase !== "idle") return;
        if (status.state === "active") {
            state.phase = "take-over-prompt";
            setMessage("Someone else is streaming to this screen.");
            render();
            return;
        }
        // VLC mode has no camera to pre-open — the panel just sits
        // idle with the RTSP URL field ready for the operator.
        if (state.sourceMode === "vlc") return;
        try {
            state.phase = "requesting-camera";
            setMessage("Requesting camera access…");
            render();
            await openLocalCamera();
            if (destroyed || mountInitCancelled || state.phase !== "requesting-camera") {
                // Operator clicked Go live (or destroyed the panel,
                // or cancelled the take-over-prompt) before
                // getUserMedia resolved — the click handler / cancel
                // takes over from here. If destroyed OR cancelled,
                // drop the stream we just opened so the camera light
                // turns off promptly.
                if (destroyed || mountInitCancelled) teardownPC();
                return;
            }
            state.phase = "preview";
            setMessage("");
            render();
        } catch {
            if (destroyed || mountInitCancelled || state.phase !== "requesting-camera") return;
            // Camera unavailable (permission denied, no device, OS
            // policy). Drop back to idle silently — the operator
            // can click Go live to retry; goLive's own error path
            // will surface the message if it fails again.
            state.phase = "idle";
            setMessage("");
            render();
        }
    }

    // --- Wire up controls -------------------------------------------------

    goLiveBtn.addEventListener("click", () => {
        goLive();
    });
    startVlcBtn.addEventListener("click", () => {
        startVlc();
    });
    for (const opt of sourceOptEls) {
        opt.addEventListener("click", () => {
            switchSourceMode(opt.dataset.source);
        });
    }
    // Pressing Enter in the RTSP URL field starts the VLC stream — the
    // operator just pasted a URL; Enter is the expected commit gesture.
    vlcUrlEl.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && !startVlcBtn.hidden && !startVlcBtn.disabled) {
            ev.preventDefault();
            startVlc();
        }
    });
    stopBtn.addEventListener("click", () => {
        stopLive();
    });
    takeOverBtn.addEventListener("click", () => {
        takeOver();
    });
    let flipInFlight = false;
    flipCameraBtn.addEventListener("click", async () => {
        if (flipInFlight) return;
        if (!state.localStream) return;
        flipInFlight = true;
        flipCameraBtn.disabled = true;
        try {
            await flipCamera();
        } catch (err) {
            // getUserMedia rejected the new facingMode (single-camera
            // device, OS denied, etc.). Leave the existing stream alone
            // and surface a status note; the panel stays in its current
            // phase since localStream is still healthy.
            setMessage(
                `Couldn't switch camera · ${err?.message || err}`,
            );
            render();
        } finally {
            flipInFlight = false;
            flipCameraBtn.disabled = false;
        }
    });

    cancelTakeoverBtn.addEventListener("click", () => {
        cancelTakeover();
    });

    // Tab close → release the camera. The backend will time out the
    // session itself on PC disconnect (~10s per §5.11), but freeing the
    // camera light immediately is the polite thing to do.
    function onPageHide() {
        teardownPC();
    }
    window.addEventListener("pagehide", onPageHide);

    render();

    // Defer mount-init (which probes /status + opens the camera) until
    // the panel is actually visible. The whole device UI mounts every
    // panel at boot regardless of route — eager init here would prompt
    // for camera permission before the operator ever navigates to the
    // Stream tab. We watch the closest data-section ancestor's `hidden`
    // attribute and only init when it goes (or starts) visible.
    function startMountInit() {
        if (mountInitPromise || destroyed) return;
        mountInitPromise = (async () => {
            try {
                await mountInit();
            } finally {
                mountInitPromise = null;
            }
        })();
    }

    function isHiddenChain(el) {
        for (let cur = el; cur; cur = cur.parentElement) {
            if (cur.hidden) return true;
        }
        return false;
    }

    let visibilityObserver = null;
    const section = container.closest?.("[data-section]") ?? null;

    function decideInitTiming() {
        if (destroyed) return;
        if (!section || !isHiddenChain(section)) {
            // No section ancestor (test fixture / standalone) or section
            // is currently visible (deep-link to /stream, or section.hidden
            // was never set). Init immediately.
            startMountInit();
            return;
        }
        visibilityObserver = new MutationObserver(() => {
            if (!isHiddenChain(section)) {
                visibilityObserver.disconnect();
                visibilityObserver = null;
                startMountInit();
            }
        });
        visibilityObserver.observe(section, {
            attributes: true,
            attributeFilter: ["hidden"],
        });
    }

    // Defer one animation frame before deciding. nav.js's initial show()
    // call applies `hidden` attrs to non-active sections SYNCHRONOUSLY at
    // boot, but mountLivePanel can run earlier in the boot order — so
    // a synchronous isHiddenChain() reads `undefined` and we'd eager-init
    // even when the operator is on a non-Stream route. By the time rAF
    // fires, all sync boot mounts have completed, including nav setup.
    // QA caught this regression on 2026-04-29 after 097d89c shipped: the
    // welcome-dismiss reload landed on a non-Stream route but the camera
    // permission prompt fired anyway.
    if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(decideInitTiming);
    } else {
        queueMicrotask(decideInitTiming);
    }

    return {
        // Test-only window into the panel state. Exposes the phase
        // string so unit tests can assert transitions without fishing
        // through the DOM.
        getState: () => state.phase,
        destroy: () => {
            destroyed = true;
            if (visibilityObserver !== null) {
                visibilityObserver.disconnect();
                visibilityObserver = null;
            }
            window.removeEventListener("pagehide", onPageHide);
            document.removeEventListener(
                "openmarquee:settings-updated",
                onSettingsUpdated,
            );
            if (elapsedTimer !== null) {
                clearInterval(elapsedTimer);
                elapsedTimer = null;
            }
            if (statsTimer !== null) {
                clearInterval(statsTimer);
                statsTimer = null;
            }
            teardownPC();
            container.innerHTML = "";
        },
    };
}
