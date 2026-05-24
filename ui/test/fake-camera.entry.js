// Auth token shape mirrors ui/src/api.js — same localStorage key
// the operator UI uses. The operator logs in once via the normal
// flow; this harness picks up the stored token. Zero backend
// changes for auth.
const AUTH_TOKEN_KEY = "openmarquee_auth_token";

const $ = (id) => document.getElementById(id);
const els = {
    src: $("src"),
    preview: $("preview"),
    start: $("start"),
    takeover: $("takeover"),
    stop: $("stop"),
    status: $("status"),
};

const defaultSrc =
    new URLSearchParams(location.search).get("src") ?? "/test/fixture.mp4";
els.src.value = defaultSrc;
els.preview.src = defaultSrc;

/** @type {{ pc: RTCPeerConnection|null, sessionId: string|null,
 *   startedAt: string|null, statusTimer: number|null,
 *   conflictSessionId: string|null }} */
const session = {
    pc: null,
    sessionId: null,
    startedAt: null,
    statusTimer: null,
    conflictSessionId: null,
};

function token() {
    try { return localStorage.getItem(AUTH_TOKEN_KEY) || ""; }
    catch { return ""; }
}

function authHeaders() {
    const t = token();
    return t ? { Authorization: `Bearer ${t}` } : {};
}

function setStatus(lines) {
    if (Array.isArray(lines)) {
        els.status.textContent = lines.filter(Boolean).join("\n");
    } else {
        els.status.textContent = String(lines);
    }
}

function refreshStatusPanel() {
    if (!session.pc) return;
    const lines = [
        `phase:           live`,
        `session_id:      ${session.sessionId ?? "(none)"}`,
        `started_at:      ${session.startedAt ?? "(none)"}`,
        `elapsed:         ${
            session.startedAt
                ? `${Math.round(
                      (Date.now() - new Date(session.startedAt).getTime()) / 1000,
                  )}s`
                : "(n/a)"
        }`,
        `pc.connectionState:    ${session.pc.connectionState}`,
        `pc.iceConnectionState: ${session.pc.iceConnectionState}`,
        `pc.iceGatheringState:  ${session.pc.iceGatheringState}`,
        `video.resolution:      ${els.preview.videoWidth}×${els.preview.videoHeight}`,
    ];
    setStatus(lines);
}

async function waitForIceGatheringComplete(pc) {
    // Non-trickle: don't return until all candidates baked into the
    // local description. Same shape live-panel.js uses for its
    // production publisher.
    if (pc.iceGatheringState === "complete") return;
    await new Promise((resolve) => {
        const checkState = () => {
            if (pc.iceGatheringState === "complete") {
                pc.removeEventListener("icegatheringstatechange", checkState);
                resolve();
            }
        };
        pc.addEventListener("icegatheringstatechange", checkState);
    });
}

async function postStart(sdpOffer, takeoverMode = false) {
    const path = takeoverMode ? "/api/live/takeover" : "/api/live/start";
    const resp = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ sdp_offer: sdpOffer }),
    });
    if (resp.status === 409 && !takeoverMode) {
        const body = await resp.json().catch(() => ({}));
        const detail = body?.detail ?? {};
        const err = new Error("live_already_active");
        err.code = detail.error || "live_already_active";
        err.activeSessionId = detail.active_session_id || null;
        err.status = 409;
        throw err;
    }
    if (!resp.ok) {
        const detail = await resp.text().catch(() => "");
        throw new Error(`${path} failed (${resp.status}): ${detail}`);
    }
    return resp.json();
}

async function postStop(sessionId) {
    const resp = await fetch("/api/live/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ session_id: sessionId }),
    });
    // 204 success or 404 (session already gone) are both fine; we
    // just want to make sure the sign isn't stuck in Live mode.
    return resp.status === 204 || resp.status === 404;
}

async function captureFromVideo() {
    // Wait for the video to have actual playable frames. captureStream
    // before metadata is loaded yields a stream with no tracks.
    if (els.preview.readyState < 2) {
        await new Promise((resolve, reject) => {
            els.preview.addEventListener("loadeddata", resolve, { once: true });
            els.preview.addEventListener("error", reject, { once: true });
        });
    }
    await els.preview.play().catch(() => {
        // Some browsers block autoplay until user interaction;
        // user has clicked Start though, so play() should succeed.
    });
    const stream = els.preview.captureStream();
    const videoTracks = stream.getVideoTracks();
    if (videoTracks.length === 0) {
        throw new Error(
            "captureStream() returned no video tracks — does your browser " +
                "support HTMLMediaElement.captureStream()?",
        );
    }
    return stream;
}

async function start(takeoverMode = false) {
    if (!token()) {
        setStatus([
            "ERROR: no auth token in localStorage.",
            "Open / in this same browser, log in, then reload this page.",
        ]);
        return;
    }
    els.start.disabled = true;
    els.takeover.hidden = true;
    els.stop.disabled = true;
    setStatus(["phase: capturing video stream …"]);
    try {
        const stream = await captureFromVideo();
        const pc = new RTCPeerConnection();
        session.pc = pc;
        for (const track of stream.getVideoTracks()) {
            pc.addTrack(track, stream);
        }
        setStatus(["phase: negotiating SDP …"]);
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        await waitForIceGatheringComplete(pc);
        // ICE-gathered SDP carries the candidates non-trickle.
        const offerSdp = pc.localDescription.sdp;
        const body = await postStart(offerSdp, takeoverMode);
        session.sessionId = body.session_id;
        session.startedAt = body.started_at;
        session.conflictSessionId = null;
        await pc.setRemoteDescription({ type: "answer", sdp: body.sdp_answer });
        els.stop.disabled = false;
        session.statusTimer = window.setInterval(refreshStatusPanel, 500);
        refreshStatusPanel();
    } catch (err) {
        if (err.code === "live_already_active") {
            session.conflictSessionId = err.activeSessionId;
            setStatus([
                "phase: blocked — another session owns the sign.",
                `active_session_id: ${err.activeSessionId}`,
                "click 'Take over' to force-stop it + start this one.",
            ]);
            els.takeover.hidden = false;
            els.start.disabled = false;
        } else {
            setStatus([
                "phase: error",
                String(err?.message ?? err),
            ]);
            els.start.disabled = false;
        }
        // Clean up the in-progress PC if start() failed AFTER it
        // was created — otherwise we leak a connecting RTCPeer.
        if (session.pc) {
            try { session.pc.close(); } catch { /* best-effort */ }
            session.pc = null;
        }
    }
}

async function stop() {
    els.stop.disabled = true;
    if (session.statusTimer) {
        clearInterval(session.statusTimer);
        session.statusTimer = null;
    }
    const sid = session.sessionId;
    if (session.pc) {
        try { session.pc.close(); } catch { /* best-effort */ }
        session.pc = null;
    }
    session.sessionId = null;
    session.startedAt = null;
    if (sid) {
        try {
            const ok = await postStop(sid);
            setStatus([ok ? "phase: stopped." : "phase: stop sent but server returned an unexpected status."]);
        } catch (err) {
            setStatus([
                "phase: stop request errored (sign may still be in Live mode)",
                String(err?.message ?? err),
            ]);
        }
    } else {
        setStatus(["phase: idle."]);
    }
    els.start.disabled = false;
}

els.start.addEventListener("click", () => start(false));
els.takeover.addEventListener("click", () => start(true));
els.stop.addEventListener("click", stop);
els.src.addEventListener("change", () => {
    els.preview.src = els.src.value || defaultSrc;
    els.preview.load();
});

// Best-effort stop on tab close so the sign doesn't get stuck.
// sendBeacon is synchronous-ish + survives unload (unlike fetch).
// We can't set custom headers (Authorization) on sendBeacon, but
// a query-param token would leak; instead, fire a Beacon with a
// best-effort empty body — the server will 401, which is fine for
// this purpose (the operator can also stop manually). For now: try
// fetch with keepalive first; if it doesn't go through, the watchdog
// on LiveSession (10s phantom-track timeout for WebRTC, or stream
// first-frame watchdog for other transports) will reap the session.
window.addEventListener("beforeunload", () => {
    if (!session.sessionId) return;
    const sid = session.sessionId;
    try {
        fetch("/api/live/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json", ...authHeaders() },
            body: JSON.stringify({ session_id: sid }),
            keepalive: true,
        });
    } catch { /* best-effort */ }
});

// Enable Start once the video has metadata; until then captureStream
// returns a track-less stream.
els.preview.addEventListener("loadedmetadata", () => {
    els.start.disabled = false;
    if (els.status.textContent === "idle.") {
        setStatus([
            "idle — video loaded, ready to publish.",
            `source: ${els.preview.currentSrc || els.src.value}`,
            `dims:   ${els.preview.videoWidth}×${els.preview.videoHeight}`,
        ]);
    }
});
els.preview.addEventListener("error", () => {
    setStatus([
        `ERROR: failed to load source: ${els.src.value}`,
        "Try a different `?src=` URL or the bundled fixture at /test/fixture.mp4.",
    ]);
});

// Initial token check so we surface the "log in first" message
// before the operator clicks Start.
if (!token()) {
    setStatus([
        "no auth token in localStorage.",
        "Open / in this same browser, log in (first-run + bearer token),",
        "then reload this page. The harness reuses the operator's existing token.",
    ]);
}
