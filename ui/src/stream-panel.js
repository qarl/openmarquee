// Stream panel — phone-camera takeover (SYSTEM_SPEC §5.11).
//
// One-tap "Go Live" UX: open the back camera, negotiate WebRTC against
// the device's playback engine (aiortc subscriber), and the device's
// screen takes over with the live feed. Stop returns the playlist
// where it was.
//
// Flow on this side:
//   1. getUserMedia({video:{facingMode:'environment'}, audio:false})
//   2. new RTCPeerConnection(), addTrack for each video track
//   3. createOffer + setLocalDescription
//   4. wait for non-trickle ICE gathering (so the SDP has all candidates
//      baked in — matches the backend's "one round trip" signaling)
//   5. POST /api/stream/start with the offer SDP, get back the answer
//   6. setRemoteDescription(answer) → frames flow
//
// On 409 (another phone owns the screen), surface a "Take over"
// affordance instead of "Go Live". Take over hits POST /api/stream/takeover
// with the same offer.
//
// Tailscale-lifecycle warning is rendered while live: iOS / Android
// kill background VPNs, so a phone whose Tailscale drops while
// app-switched will silently lose the connection. Documented as a
// Phase 1 known limitation in §5.11.

import {
    getSettings,
    getStreamStatus,
    startStream,
    stopStream,
    takeoverStream,
} from "./api.js";

// All media constraints in one place so the §5.11 hardware-tier table
// drives them via the basic-tier numbers reported by /api/stream/status.
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

// Unified StreamHeader (eyebrow + title + variable action button) +
// viewfinder + idle-only paused-playlist row + live-only metrics grid.
// Design reference: design/stream-redesigns-2026-04-29 variants A
// (idle) + E (live). Per qarl's iteration in chat2.md (2026-04-28),
// the action button is the only thing that swaps between modes —
// everything else stays put so the eye doesn't have to chase.
//
// Divergences from the prior Phase 12.2 panel (defaulted, not
// confirmed with qarl yet):
// 1. Camera flip button dropped — the redesign defers source-
//    switching until after Stop. Matches qarl's "drop settings for
//    now" intent and keeps the live HUD uncluttered. Reversible if
//    the flow turns out to need it.
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
    <section class="stream">
        <header class="stream-header">
            <div class="stream-header-text">
                <span class="stream-header-eyebrow">Live · this device's camera</span>
                <h1 class="stream-header-title">Stream</h1>
                <p class="stream-header-blurb">
                    Push the camera on this device straight to your sign.
                    The active playlist pauses while you broadcast and
                    picks up where it left off.
                </p>
            </div>
            <div class="stream-header-action">
                <button type="button" class="om-btn primary stream-go-live">
                    <span class="stream-go-live-dot" aria-hidden="true"></span>
                    Go live
                </button>
                <button type="button" class="om-btn stream-stop" hidden>
                    <span class="stream-stop-square" aria-hidden="true"></span>
                    Stop
                </button>
                <button type="button" class="om-btn stream-take-over" hidden>
                    Take over
                </button>
                <button type="button" class="om-btn ghost stream-cancel-takeover" hidden>
                    Cancel
                </button>
            </div>
        </header>

        <div class="stream-stage">
            <div class="stream-preview-wrap">
                <video class="stream-preview" autoplay muted playsinline></video>
                <div class="stream-preview-empty">
                    Tap <strong>Go live</strong> to open your camera.
                </div>
                <div class="stream-live-pill" hidden>
                    <span class="stream-live-pill-dot" aria-hidden="true"></span>
                    LIVE
                </div>
            </div>
        </div>

        <div class="stream-paused-row" hidden>
            <span class="stream-paused-row-dot" aria-hidden="true"></span>
            <span>paused while live · resumes <b class="stream-paused-row-name">the active playlist</b> when you stop</span>
        </div>

        <div class="stream-metrics-grid" hidden>
            <div class="stream-metric-cell">
                <div class="stream-metric-label">Elapsed</div>
                <div class="stream-metric-value" data-metric="elapsed">00:00</div>
            </div>
            <div class="stream-metric-cell">
                <div class="stream-metric-label">Latency</div>
                <div class="stream-metric-value" data-metric="latency">78 ms</div>
            </div>
            <div class="stream-metric-cell">
                <div class="stream-metric-label">Bitrate</div>
                <div class="stream-metric-value" data-metric="bitrate">2.8 Mbps</div>
            </div>
            <div class="stream-metric-cell">
                <div class="stream-metric-label">Dropped</div>
                <div class="stream-metric-value" data-metric="dropped">0</div>
            </div>
        </div>

        <div class="stream-status" role="status" aria-live="polite"></div>
    </section>
`;

/**
 * Mount the Stream panel into `container`.
 *
 * @param {HTMLElement} container — slot to fill.
 * @param {object} [options] — dependency-injection seams for tests.
 * @param {() => Promise} [options.apiGetStatus]
 * @param {(sdp:string) => Promise} [options.apiStartStream]
 * @param {(sdp:string) => Promise} [options.apiTakeoverStream]
 * @param {(sessionId:string) => Promise} [options.apiStopStream]
 * @param {(constraints) => Promise<MediaStream>} [options.getUserMedia]
 * @param {() => RTCPeerConnection} [options.createPeerConnection]
 * @param {boolean} [options.simulateOnly] — when true, skip the
 *   WebRTC negotiation and the /api/stream/{start,stop,takeover}
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
export function mountStreamPanel(container, options = {}) {
    const {
        apiGetStatus = getStreamStatus,
        apiStartStream = startStream,
        apiTakeoverStream = takeoverStream,
        apiStopStream = stopStream,
        fetchSettings = getSettings,
        getUserMedia = (constraints) =>
            navigator.mediaDevices.getUserMedia(constraints),
        createPeerConnection = () => new RTCPeerConnection(),
        simulateOnly = false,
    } = options;

    container.innerHTML = SECTION_TEMPLATE;

    const previewWrapEl = container.querySelector(".stream-preview-wrap");
    const previewEl = container.querySelector(".stream-preview");
    const previewEmptyEl = container.querySelector(".stream-preview-empty");
    const statusEl = container.querySelector(".stream-status");
    const goLiveBtn = container.querySelector(".stream-go-live");
    const stopBtn = container.querySelector(".stream-stop");
    const takeOverBtn = container.querySelector(".stream-take-over");
    const cancelTakeoverBtn = container.querySelector(".stream-cancel-takeover");
    const livePillEl = container.querySelector(".stream-live-pill");
    const pausedRowEl = container.querySelector(".stream-paused-row");
    const metricsGridEl = container.querySelector(".stream-metrics-grid");
    const elapsedEl = container.querySelector('[data-metric="elapsed"]');

    // Mirror the device's display aspect ratio onto the preview wrap so
    // the operator sees actual cropping (object-fit: cover on the video
    // matches the device-side aiortc subscriber's cover-fit at playback
    // time). Updated on mount and on every openmarquee:settings-updated
    // event so a settings change while the panel is mounted reflects
    // immediately. Falls back to the CSS-default 9/16 (phone-portrait)
    // if /api/settings is unreachable on first load.
    async function refreshPreviewAspect() {
        try {
            const s = await fetchSettings();
            const w = Number(s?.display_width);
            const h = Number(s?.display_height);
            if (Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0) {
                previewWrapEl.style.setProperty(
                    "--om-stream-aspect",
                    `${w} / ${h}`,
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
        // "idle" | "requesting-camera" | "negotiating" | "live" |
        // "take-over-prompt" | "error"
        phase: "idle",
        sessionId: null,
        // Active local camera stream (preview source + the track we add
        // to the PC). Null when idle/error/take-over-prompt.
        localStream: null,
        // Active RTCPeerConnection. Null between sessions.
        pc: null,
        // User-facing message rendered into .stream-status. Reset when
        // a transition clears it.
        message: "",
        // Timestamp of the last 'live' transition (Date.now()). Cleared
        // on stop/error. The metrics-grid Elapsed cell ticks against
        // this once per second while phase === 'live'. Phase B will
        // augment with real RTCPeerConnection.getStats() polling for
        // latency/bitrate/dropped; for A.1 those stay mocked.
        startedAt: null,
    };

    // 1Hz interval handle that drives the Elapsed cell while live.
    // Lives at module scope so render() can clear/start it from any
    // phase transition without leaking.
    let elapsedTimer = null;
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

    function setMessage(text) {
        state.message = text;
        statusEl.textContent = text;
    }

    function render() {
        // Visibility matrix per phase. The render is idempotent — call
        // it after every state mutation; the DOM converges.
        const phase = state.phase;
        goLiveBtn.hidden = !(phase === "idle" || phase === "error");
        stopBtn.hidden = phase !== "live";
        takeOverBtn.hidden = phase !== "take-over-prompt";
        cancelTakeoverBtn.hidden = phase !== "take-over-prompt";

        // LIVE pill on the viewfinder + metrics grid below it: only
        // while live. Idle-only paused-playlist hint: only when idle/
        // error (i.e. not transient + not live).
        livePillEl.hidden = phase !== "live";
        metricsGridEl.hidden = phase !== "live";
        pausedRowEl.hidden = !(phase === "idle" || phase === "error");

        // Empty-state cover only when there's no local preview to show.
        previewEmptyEl.hidden = state.localStream !== null;

        // Disable Go live during transient phases so a double-tap can't
        // start two negotiations.
        goLiveBtn.disabled =
            phase === "requesting-camera" || phase === "negotiating";

        // Elapsed-timer lifecycle. Starts on the live transition,
        // stops on any non-live phase. setInterval is 1Hz to match
        // the seconds-precision the metric cell displays.
        if (phase === "live") {
            if (state.startedAt === null) state.startedAt = Date.now();
            tickElapsed();
            if (elapsedTimer === null) {
                elapsedTimer = setInterval(tickElapsed, 1000);
            }
        } else {
            if (elapsedTimer !== null) {
                clearInterval(elapsedTimer);
                elapsedTimer = null;
            }
            state.startedAt = null;
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
        // Always opens the back-facing ('environment') camera on
        // mobile. The redesign drops the in-stream camera-flip
        // affordance — source-switching is deferred until after Stop
        // (start a fresh session against a different camera). The
        // `facingMode: 'environment'` constraint is a soft hint; on
        // desktops + phones-with-only-one-camera, getUserMedia falls
        // back to whatever's available.
        const constraints = {
            ...DEFAULT_CAPTURE_CONSTRAINTS,
            video: {
                ...DEFAULT_CAPTURE_CONSTRAINTS.video,
                facingMode: "environment",
            },
        };
        const stream = await getUserMedia(constraints);
        previewEl.srcObject = stream;
        state.localStream = stream;
        return stream;
    }

    function teardownPC() {
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
        if (state.localStream) {
            for (const track of state.localStream.getTracks()) {
                try {
                    track.stop();
                } catch {
                    /* track.stop on an already-ended track is harmless */
                }
            }
            state.localStream = null;
        }
        previewEl.srcObject = null;
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

        const apiCall = takeover ? apiTakeoverStream : apiStartStream;
        const { session_id, sdp_answer } = await apiCall(offerSdp);
        await pc.setRemoteDescription({ sdp: sdp_answer, type: "answer" });
        state.sessionId = session_id;
    }

    async function simulateNegotiate() {
        // Demo-mode shortcut: skip the WebRTC negotiation entirely.
        // No PC, no /api/stream/{start,takeover} round trip — just
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
        // Pre-flight /status check: if another phone owns the screen,
        // skip the camera prompt entirely and surface the take-over UI
        // straight away. Saves the operator a permission dialog they'd
        // dismiss anyway.
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

        try {
            state.phase = "requesting-camera";
            setMessage("Requesting camera access…");
            render();
            await openLocalCamera();

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
            if (err && err.code === "stream_already_active") {
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
        teardownPC();
        state.sessionId = null;
        // simulateOnly minted the session_id locally — the backend
        // never knew about it, so /api/stream/stop has nothing to
        // tear down and would just 404.
        if (!simulateOnly) {
            try {
                if (sessionId) await apiStopStream(sessionId);
            } catch {
                // Stop API failure is non-fatal — the device times
                // out the session on PC disconnect anyway. Don't
                // block the operator.
            }
        }
        state.phase = "idle";
        setMessage("");
        render();
    }

    async function takeOver() {
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

    function cancelTakeover() {
        // Defensive: if we got here via the 409-mid-flight branch, an
        // orphan PC + camera stream may still be lingering. Belt and
        // suspenders for the goLive() catch — teardownPC is idempotent
        // when there's nothing to tear down.
        teardownPC();
        state.sessionId = null;
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

    // --- Wire up controls -------------------------------------------------

    goLiveBtn.addEventListener("click", () => {
        goLive();
    });
    stopBtn.addEventListener("click", () => {
        stopLive();
    });
    takeOverBtn.addEventListener("click", () => {
        takeOver();
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

    return {
        // Test-only window into the panel state. Exposes the phase
        // string so unit tests can assert transitions without fishing
        // through the DOM.
        getState: () => state.phase,
        destroy: () => {
            window.removeEventListener("pagehide", onPageHide);
            document.removeEventListener(
                "openmarquee:settings-updated",
                onSettingsUpdated,
            );
            if (elapsedTimer !== null) {
                clearInterval(elapsedTimer);
                elapsedTimer = null;
            }
            teardownPC();
            container.innerHTML = "";
        },
    };
}
