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

const SECTION_TEMPLATE = `
    <section class="stream">
        <div class="om-page-head">
            <div>
                <span class="om-eyebrow">Live · phone camera takeover</span>
                <h1>Stream</h1>
                <p>
                    Broadcast your phone's camera live to this device's
                    screen. The active playlist pauses while you stream
                    and resumes when you stop.
                </p>
            </div>
        </div>

        <div class="stream-stage">
            <div class="stream-preview-wrap">
                <video class="stream-preview" autoplay muted playsinline></video>
                <div class="stream-preview-empty">
                    Tap <strong>Go Live</strong> to open your camera.
                </div>
            </div>
        </div>

        <div class="stream-status" role="status" aria-live="polite"></div>

        <div class="stream-warning" hidden>
            <strong>Keep openMarquee in the foreground while streaming.</strong>
            iOS and Android kill background VPNs aggressively — if
            Tailscale drops while your phone is locked or app-switched,
            the stream disconnects and won't reconnect on its own.
        </div>

        <div class="stream-controls">
            <button type="button" class="om-btn primary stream-go-live">
                Go Live
            </button>
            <button type="button" class="om-btn stream-stop" hidden>
                Stop
            </button>
            <button type="button" class="om-btn ghost stream-flip-camera" hidden>
                Flip camera
            </button>
            <button type="button" class="om-btn primary stream-take-over" hidden>
                Take over
            </button>
            <button type="button" class="om-btn ghost stream-cancel-takeover" hidden>
                Cancel
            </button>
        </div>
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
 * @returns {{ destroy: () => void, getState: () => string }}
 */
export function mountStreamPanel(container, options = {}) {
    const {
        apiGetStatus = getStreamStatus,
        apiStartStream = startStream,
        apiTakeoverStream = takeoverStream,
        apiStopStream = stopStream,
        getUserMedia = (constraints) =>
            navigator.mediaDevices.getUserMedia(constraints),
        createPeerConnection = () => new RTCPeerConnection(),
        simulateOnly = false,
    } = options;

    container.innerHTML = SECTION_TEMPLATE;

    const previewEl = container.querySelector(".stream-preview");
    const previewEmptyEl = container.querySelector(".stream-preview-empty");
    const statusEl = container.querySelector(".stream-status");
    const warningEl = container.querySelector(".stream-warning");
    const goLiveBtn = container.querySelector(".stream-go-live");
    const stopBtn = container.querySelector(".stream-stop");
    const flipBtn = container.querySelector(".stream-flip-camera");
    const takeOverBtn = container.querySelector(".stream-take-over");
    const cancelTakeoverBtn = container.querySelector(".stream-cancel-takeover");

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
        // "user" | "environment" — toggle target for the camera flip.
        facing: "environment",
        // User-facing message rendered into .stream-status. Reset when
        // a transition clears it.
        message: "",
    };

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
        flipBtn.hidden = phase !== "live";
        takeOverBtn.hidden = phase !== "take-over-prompt";
        cancelTakeoverBtn.hidden = phase !== "take-over-prompt";
        warningEl.hidden = phase !== "live";

        // Empty-state cover only when there's no local preview to show.
        previewEmptyEl.hidden = state.localStream !== null;

        // Disable Go Live during transient phases so a double-tap can't
        // start two negotiations.
        goLiveBtn.disabled =
            phase === "requesting-camera" || phase === "negotiating";
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

    async function openLocalCamera(facing) {
        const constraints = {
            ...DEFAULT_CAPTURE_CONSTRAINTS,
            video: { ...DEFAULT_CAPTURE_CONSTRAINTS.video, facingMode: facing },
        };
        const stream = await getUserMedia(constraints);
        previewEl.srcObject = stream;
        state.localStream = stream;
        state.facing = facing;
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
            await openLocalCamera("environment");

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
            await openLocalCamera("environment");

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

    async function flipCamera() {
        // Disable the flip button across the await — without this guard
        // a rapid double-tap fires two getUserMedia calls and at least
        // one of the resulting MediaStreams ends up unstopped (camera
        // light stays on). Re-enable in finally so error paths don't
        // strand the button.
        flipBtn.disabled = true;
        try {
            const next = state.facing === "environment" ? "user" : "environment";
            let newStream;
            try {
                const constraints = {
                    ...DEFAULT_CAPTURE_CONSTRAINTS,
                    video: {
                        ...DEFAULT_CAPTURE_CONSTRAINTS.video,
                        facingMode: next,
                    },
                };
                newStream = await getUserMedia(constraints);
            } catch (err) {
                // Phones with only one camera throw OverconstrainedError.
                // Surface it but keep streaming on the existing camera.
                setMessage(`Couldn't switch cameras: ${err?.message || err}`);
                return;
            }
            const newTrack = newStream.getVideoTracks()[0];
            if (!newTrack) {
                setMessage("Couldn't switch cameras: no video track.");
                return;
            }

            // replaceTrack swaps the encoded source without renegotiating
            // — no SDP exchange, no PC teardown. Per §5.11 v1 spec.
            const sender = state.pc
                ?.getSenders()
                .find((s) => s.track && s.track.kind === "video");
            if (sender) {
                try {
                    await sender.replaceTrack(newTrack);
                } catch (err) {
                    // Some Safari versions throw on replaceTrack mid-stream.
                    // Roll back.
                    for (const t of newStream.getTracks()) t.stop();
                    setMessage(`Couldn't switch cameras: ${err?.message || err}`);
                    return;
                }
            }

            // Stop the OLD tracks before we drop the reference, otherwise
            // the camera light stays on.
            for (const t of state.localStream.getTracks()) t.stop();
            previewEl.srcObject = newStream;
            state.localStream = newStream;
            state.facing = next;
        } finally {
            flipBtn.disabled = false;
        }
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
    flipBtn.addEventListener("click", () => {
        flipCamera();
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
            teardownPC();
            container.innerHTML = "";
        },
    };
}
