// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mountLivePanel } from "./live-panel.js";

beforeEach(() => {
    vi.stubGlobal("RTCPeerConnection", undefined);
    // The panel persists source-mode + facing-mode + live-url to
    // localStorage; clear it between tests so one test's writes don't
    // leak into the next mount's hydration.
    globalThis.localStorage?.clear();
});

afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

// Pump the microtask + macrotask queue until `predicate()` is truthy or
// `maxIters` ticks have elapsed. Used to wait for the panel's awaited
// API/getUserMedia promises to resolve into state transitions.
async function waitFor(predicate, maxIters = 20) {
    for (let i = 0; i < maxIters; i++) {
        if (predicate()) return;
        await tick();
    }
    throw new Error(`waitFor: predicate never became true (${maxIters} ticks)`);
}

// --- Fakes -------------------------------------------------------------

function makeFakeTrack({ kind = "video" } = {}) {
    return {
        kind,
        stopped: false,
        stop() {
            this.stopped = true;
        },
    };
}

function makeFakeStream({ tracks } = {}) {
    const t = tracks || [makeFakeTrack()];
    return {
        getTracks: () => t,
        getVideoTracks: () => t.filter((tr) => tr.kind === "video"),
    };
}

function makeFakePc({ answerSdp = "v=0\r\nfake-answer\r\n" } = {}) {
    const handlers = {};
    const senders = [];
    const pc = {
        iceGatheringState: "complete",
        connectionState: "new",
        addTrack(track) {
            const sender = {
                track,
                async replaceTrack(newTrack) {
                    sender.track = newTrack;
                },
            };
            senders.push(sender);
            return sender;
        },
        getSenders: () => senders,
        async createOffer() {
            return { sdp: "v=0\r\nfake-offer\r\n", type: "offer" };
        },
        async setLocalDescription(offer) {
            pc.localDescription = offer;
        },
        async setRemoteDescription(answer) {
            pc.remoteDescription = answer;
        },
        addEventListener(event, fn) {
            handlers[event] = fn;
        },
        removeEventListener(event) {
            delete handlers[event];
        },
        // Test-only knob: simulate the PC's connection state changing
        // (Tailscale flap, peer crash, network blip). Sets the new
        // state and fires whatever connectionstatechange listener the
        // panel registered.
        _setConnectionState(next) {
            pc.connectionState = next;
            if (handlers.connectionstatechange) {
                handlers.connectionstatechange();
            }
        },
        closed: false,
        close() {
            pc.closed = true;
        },
        // Phase B.1: getStats() returns a Map<string, RTCStats> per
        // the WebRTC spec. Default fakePc returns a minimal report
        // shape (one outbound-rtp/video, one remote-inbound-rtp/video,
        // one nominated candidate-pair) that the panel's pollStats
        // happy-path consumes; tests that need to observe a delta
        // override _fakeStatsReport between polls.
        _fakeStatsReport: new Map([
            [
                "outbound-rtp-video",
                {
                    type: "outbound-rtp",
                    kind: "video",
                    bytesSent: 100_000,
                    timestamp: 1_000_000,
                },
            ],
            [
                "remote-inbound-rtp-video",
                {
                    type: "remote-inbound-rtp",
                    kind: "video",
                    roundTripTime: 0.078, // 78 ms
                    packetsLost: 3,
                },
            ],
            [
                "candidate-pair",
                {
                    type: "candidate-pair",
                    nominated: true,
                    currentRoundTripTime: 0.080,
                },
            ],
        ]),
        async getStats() {
            return pc._fakeStatsReport;
        },
    };
    pc.localDescription = { sdp: "v=0\r\nfake-offer\r\n", type: "offer" };
    pc._answerSdp = answerSdp;
    return pc;
}

function defaultMounts(overrides = {}) {
    const fakePc = makeFakePc();
    return {
        apiGetStatus: vi.fn(async () => ({
            state: "idle",
            session_id: null,
            tier: { name: "basic", max_width: 854, max_height: 480, max_fps: 30 },
        })),
        apiStartLive: vi.fn(async () => ({
            session_id: "11111111-1111-1111-1111-111111111111",
            sdp_answer: "v=0\r\nfake-answer\r\n",
            // Phase A.2: backend stamps wall-clock UTC start time so
            // the phone's Elapsed counter ticks against the device's
            // authoritative reference instead of phone-local Date.now.
            started_at: "2026-04-29T00:00:00+00:00",
        })),
        apiTakeoverLive: vi.fn(async () => ({
            session_id: "22222222-2222-2222-2222-222222222222",
            sdp_answer: "v=0\r\nfake-answer\r\n",
            started_at: "2026-04-29T00:00:00+00:00",
        })),
        apiStartRtspLive: vi.fn(async () => ({
            session_id: "33333333-3333-3333-3333-333333333333",
            sdp_answer: null,
            started_at: "2026-05-20T00:00:00+00:00",
        })),
        apiTakeoverRtspLive: vi.fn(async () => ({
            session_id: "44444444-4444-4444-4444-444444444444",
            sdp_answer: null,
            started_at: "2026-05-20T00:00:00+00:00",
        })),
        apiStopLive: vi.fn(async () => undefined),
        fetchSettings: vi.fn(async () => ({
            display_width: 1920,
            display_height: 1080,
        })),
        getUserMedia: vi.fn(async () => makeFakeStream()),
        createPeerConnection: vi.fn(() => fakePc),
        _fakePc: fakePc,
        ...overrides,
    };
}

// --- Tests --------------------------------------------------------------

describe("mountLivePanel", () => {
    it("renders Go live in idle: action button visible, LIVE pill + metrics + Stop hidden", () => {
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        expect(container.querySelector(".live-go-live").hidden).toBe(false);
        expect(container.querySelector(".live-stop").hidden).toBe(true);
        // 2026-04-29 redesign dropped the tailscale-foreground warning;
        // still gone. Camera flip was dropped in that pass too but
        // restored 2026-05-01 per qarl — it's hidden in idle (no
        // localStream), surfaces in preview/live.
        expect(container.querySelector(".live-warning")).toBeNull();
        expect(container.querySelector(".live-flip-camera").hidden).toBe(true);
        // LIVE pill + metrics grid only show in the live phase.
        expect(container.querySelector(".live-live-pill").hidden).toBe(true);
        expect(container.querySelector(".live-metrics-grid").hidden).toBe(true);
        // Paused-playlist hint is visible in any "ready" phase.
        expect(container.querySelector(".live-paused-row").hidden).toBe(false);
        // Phase machine starts in idle synchronously; mount-init's
        // pre-flight + camera-open are both async and resolve later
        // (covered by separate tests below).
        expect(handle.getState()).toBe("idle");
    });

    it("opens the camera at mount + lands in preview when /status is idle", async () => {
        // Phase 12.2 followup, qarl 2026-04-29: operator should see
        // their camera framed in the viewfinder before clicking Go
        // live. Mount-init runs the same /status pre-flight goLive
        // used to do, then opens the camera if nothing else owns the
        // screen, leaving the panel in the new "preview" phase.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        await waitFor(() => handle.getState() === "preview");

        expect(opts.apiGetStatus).toHaveBeenCalledTimes(1);
        expect(opts.getUserMedia).toHaveBeenCalledTimes(1);
        // Go live still visible (preview is a "ready" phase); no LIVE
        // pill or metrics grid yet (those are live-only).
        expect(container.querySelector(".live-go-live").hidden).toBe(false);
        expect(container.querySelector(".live-live-pill").hidden).toBe(true);
        expect(container.querySelector(".live-metrics-grid").hidden).toBe(true);
    });

    it("mount-init falls back to idle silently when getUserMedia rejects", async () => {
        // Permission denied / no camera at mount should not blast an
        // error — the operator may not intend to go live. Click Go
        // live then surfaces a real error if the retry also fails.
        const container = document.createElement("div");
        const opts = defaultMounts({
            getUserMedia: vi.fn(async () => {
                throw new Error("Permission denied");
            }),
        });
        const handle = mountLivePanel(container, opts);

        // Wait for mount-init's getUserMedia attempt to land first
        // (the panel starts in idle synchronously, so a naive
        // waitFor(state==="idle") could resolve before mount-init
        // has run anything at all).
        await waitFor(() => opts.getUserMedia.mock.calls.length === 1);
        await waitFor(() => handle.getState() === "idle");
        expect(opts.apiGetStatus).toHaveBeenCalledTimes(1);
        // No noise in the status line — the panel reads as a clean
        // idle state.
        expect(container.querySelector(".live-status").textContent).toBe("");
    });

    it("defers camera + /status until the section becomes visible", async () => {
        // Bug B4 (qarl batch 2026-04-29): mount-init was firing at app
        // boot regardless of route, so the camera permission prompt
        // appeared before the operator ever clicked the Live tab.
        // The panel should now wait for its closest [data-section]
        // ancestor's `hidden` attribute to clear before probing /status
        // or opening the camera.
        const section = document.createElement("section");
        section.setAttribute("data-section", "live");
        section.hidden = true;
        const container = document.createElement("div");
        section.appendChild(container);
        document.body.appendChild(section);
        try {
            const opts = defaultMounts();
            const handle = mountLivePanel(container, opts);

            // Flush microtasks so any (errant) eager init has a chance
            // to land — asserting on a negative needs determinism, not
            // a wall-clock sleep.
            await Promise.resolve();
            await Promise.resolve();
            expect(opts.apiGetStatus).not.toHaveBeenCalled();
            expect(opts.getUserMedia).not.toHaveBeenCalled();
            expect(handle.getState()).toBe("idle");

            // Operator navigates to Live → nav clears the hidden attr.
            section.hidden = false;

            await waitFor(() => handle.getState() === "preview");
            expect(opts.apiGetStatus).toHaveBeenCalledTimes(1);
            expect(opts.getUserMedia).toHaveBeenCalledTimes(1);

            handle.destroy();
        } finally {
            section.remove();
        }
    });

    it("doesn't race nav setup: section.hidden=undefined at mount still defers init when nav applies hidden synchronously after", async () => {
        // QA caught this regression on 2026-04-29: my first B4 fix
        // (097d89c) checked section.hidden synchronously at mount, but
        // mountLivePanel runs BEFORE nav.js's initial show() call in
        // main.js's boot order. So `hidden` was still undefined at
        // mount time and the panel eager-inited anyway, even when the
        // operator was on a non-Live route after the welcome-dismiss
        // reload.
        //
        // The fix defers the visibility decision to the next animation
        // frame, by which time nav has applied initial hidden states.
        const section = document.createElement("section");
        section.setAttribute("data-section", "live");
        // hidden NOT set — simulating mount-before-nav at boot.
        const container = document.createElement("div");
        section.appendChild(container);
        document.body.appendChild(section);
        try {
            const opts = defaultMounts();
            const handle = mountLivePanel(container, opts);

            // Boot sequence: nav's show() lands synchronously after mount.
            // Simulate that by setting hidden synchronously here.
            section.hidden = true;

            // Wait two animation frames so the deferred init has fired
            // and (correctly) decided to install the observer rather
            // than init.
            await new Promise((r) => requestAnimationFrame(() => r()));
            await new Promise((r) => requestAnimationFrame(() => r()));

            expect(opts.apiGetStatus).not.toHaveBeenCalled();
            expect(opts.getUserMedia).not.toHaveBeenCalled();
            expect(handle.getState()).toBe("idle");

            // Operator clicks the Live tab → nav clears the hidden attr.
            section.hidden = false;
            await waitFor(() => handle.getState() === "preview");
            expect(opts.apiGetStatus).toHaveBeenCalledTimes(1);
            expect(opts.getUserMedia).toHaveBeenCalledTimes(1);

            handle.destroy();
        } finally {
            section.remove();
        }
    });

    it("mount-init enters take-over-prompt without opening the camera when /status is active", async () => {
        // Some other phone owns the screen at mount time. Pre-flight
        // surfaces take-over-prompt directly, skipping the camera-
        // permission dialog the operator hasn't earned yet.
        const container = document.createElement("div");
        const opts = defaultMounts({
            apiGetStatus: vi.fn(async () => ({
                state: "active",
                session_id: "33",
                tier: { name: "basic", max_width: 854, max_height: 480, max_fps: 30 },
            })),
        });
        const handle = mountLivePanel(container, opts);

        await waitFor(() => handle.getState() === "take-over-prompt");
        expect(opts.getUserMedia).not.toHaveBeenCalled();
        expect(container.querySelector(".live-take-over").hidden).toBe(false);
    });

    it("Go live from preview skips the second pre-flight + camera-open round trip", async () => {
        // Mount-init already ran /status + opened the camera; clicking
        // Go live should reuse them and go straight to negotiating.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        await waitFor(() => handle.getState() === "preview");

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        // Single /status (mount-init) + single getUserMedia (mount-
        // init): no double round-trip on the click.
        expect(opts.apiGetStatus).toHaveBeenCalledTimes(1);
        expect(opts.getUserMedia).toHaveBeenCalledTimes(1);
    });

    it("Go Live → live: opens camera, negotiates, flips to live phase", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        expect(opts.apiGetStatus).toHaveBeenCalledTimes(1);
        expect(opts.getUserMedia).toHaveBeenCalledTimes(1);
        const constraints = opts.getUserMedia.mock.calls[0][0];
        // Default tier = back camera, no audio (per §5.11 NO AUDIO posture).
        expect(constraints.video.facingMode).toBe("environment");
        expect(constraints.audio).toBe(false);
        expect(opts.apiStartLive).toHaveBeenCalledTimes(1);
        // Live state: Stop visible, Go live hidden, LIVE pill +
        // metrics grid visible, idle-only paused-playlist row hidden.
        expect(container.querySelector(".live-stop").hidden).toBe(false);
        expect(container.querySelector(".live-go-live").hidden).toBe(true);
        expect(container.querySelector(".live-live-pill").hidden).toBe(false);
        expect(container.querySelector(".live-metrics-grid").hidden).toBe(false);
        expect(container.querySelector(".live-paused-row").hidden).toBe(true);
    });

    it("Stop → preview hides LIVE pill + metrics grid (QA regression — inconsistent state where Go live and LIVE pill were simultaneously visible)", async () => {
        // QA verifier 02649a6 caught: post-Stop, Go live re-visible
        // (preview phase) but LIVE pill still rendered. JS-level fix:
        // render() correctly sets .hidden=true on each live-only
        // element. The visual fix lived in styles.css — see
        // .live-live-pill[hidden] etc. block — because the
        // .om-app [hidden] !important global rule wasn't winning the
        // cascade against display: inline-flex / flex / grid in QA's
        // verifier-build environment.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        await waitFor(() => handle.getState() === "preview");
        // Preview-empty cover hides as soon as the camera stream
        // is wired to the <video> — defends the .live-preview-
        // empty[hidden] rule too, since it's the same shape.
        expect(container.querySelector(".live-preview-empty").hidden).toBe(true);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");
        // Live phase: LIVE pill + metrics grid visible, paused row hidden.
        expect(container.querySelector(".live-live-pill").hidden).toBe(false);
        expect(container.querySelector(".live-metrics-grid").hidden).toBe(false);
        expect(container.querySelector(".live-paused-row").hidden).toBe(true);

        container.querySelector(".live-stop").click();
        await waitFor(() => handle.getState() === "preview");
        // Preview phase: LIVE pill + metrics grid hidden, paused row visible,
        // Go live re-visible (none of those should be in a "live"-only state).
        expect(container.querySelector(".live-live-pill").hidden).toBe(true);
        expect(container.querySelector(".live-metrics-grid").hidden).toBe(true);
        expect(container.querySelector(".live-paused-row").hidden).toBe(false);
        expect(container.querySelector(".live-go-live").hidden).toBe(false);
        expect(container.querySelector(".live-stop").hidden).toBe(true);
        // Camera kept open across Stop → the empty-cover stays hidden.
        expect(container.querySelector(".live-preview-empty").hidden).toBe(true);
    });

    it("Stop → preview: posts session_id, keeps the camera open for fast re-go-live", async () => {
        // Phase 12.2 followup, qarl 2026-04-29: Stop returns to the
        // preview phase (camera stays open + viewfinder still rendering)
        // so the operator can re-go-live without another permission
        // round-trip. The PC is torn down (the session is over) but the
        // local camera stream is preserved.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        const stream = opts.getUserMedia.mock.results[0].value;
        const track = (await stream).getVideoTracks()[0];

        container.querySelector(".live-stop").click();
        await waitFor(() => handle.getState() === "preview");

        expect(opts.apiStopLive).toHaveBeenCalledTimes(1);
        expect(opts.apiStopLive.mock.calls[0][0]).toBe(
            "11111111-1111-1111-1111-111111111111",
        );
        // The PC is closed (session ended) but the camera stream
        // and its tracks stay alive — they're the preview source.
        expect(opts._fakePc.closed).toBe(true);
        expect(track.stopped).toBe(false);
    });

    it("pre-flight /status returns active → take-over-prompt without opening camera", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts({
            apiGetStatus: vi.fn(async () => ({
                state: "active",
                session_id: "33",
                tier: { name: "basic", max_width: 854, max_height: 480, max_fps: 30 },
            })),
        });
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        expect(opts.getUserMedia).not.toHaveBeenCalled();
        expect(opts.apiStartLive).not.toHaveBeenCalled();
        expect(container.querySelector(".live-take-over").hidden).toBe(false);
        expect(container.querySelector(".live-cancel-takeover").hidden).toBe(false);
        expect(container.querySelector(".live-go-live").hidden).toBe(true);
    });

    it("/start returning 409 mid-flight transitions to take-over-prompt and tears down the half-open PC + camera", async () => {
        // Regression for the leak the pre-commit subagent caught: prior
        // version flipped phase to take-over-prompt without closing the
        // PC or stopping the camera tracks, so a subsequent Take Over
        // overwrote the references and orphaned them — camera light
        // stayed on, PC kept ICE'ing.
        const container = document.createElement("div");
        const conflict = Object.assign(new Error("live_already_active"), {
            code: "live_already_active",
            activeSessionId: "44",
            status: 409,
        });
        const stream = makeFakeStream();
        const fakePc = makeFakePc();
        const opts = defaultMounts({
            apiStartLive: vi.fn(async () => {
                throw conflict;
            }),
            getUserMedia: vi.fn(async () => stream),
            createPeerConnection: vi.fn(() => fakePc),
        });
        // _fakePc on opts is the default fixture; override here so we
        // can assert against the one actually used.
        opts._fakePc = fakePc;
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        expect(opts.getUserMedia).toHaveBeenCalled();
        expect(container.querySelector(".live-take-over").hidden).toBe(false);
        // The PC created during the failed negotiation MUST be closed,
        // and the camera stream's tracks MUST be stopped — otherwise
        // tapping Take Over (which creates fresh ones) leaks the
        // originals.
        expect(fakePc.closed).toBe(true);
        expect(stream.getVideoTracks()[0].stopped).toBe(true);
    });

    it("Cancel from a 409-mid-flight take-over-prompt also tears down (defense in depth)", async () => {
        // Even with the goLive() path's teardown, the user might have
        // gotten to take-over-prompt some other way and we want
        // cancelTakeover to be safe to call regardless.
        const container = document.createElement("div");
        const conflict = Object.assign(new Error("live_already_active"), {
            code: "live_already_active",
            activeSessionId: "44",
            status: 409,
        });
        const stream = makeFakeStream();
        const fakePc = makeFakePc();
        const opts = defaultMounts({
            apiStartLive: vi.fn(async () => {
                throw conflict;
            }),
            getUserMedia: vi.fn(async () => stream),
            createPeerConnection: vi.fn(() => fakePc),
        });
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        container.querySelector(".live-cancel-takeover").click();
        await tick();
        expect(handle.getState()).toBe("idle");
        // Already torn down by goLive's catch; cancelTakeover's defensive
        // teardown is a no-op against an already-closed PC, which is the
        // important contract — calling teardown twice doesn't blow up.
        expect(fakePc.closed).toBe(true);
    });

    it("Bug 6: cancelTakeover stays idle even when mountInit resolves AFTER cancel", async () => {
        // Regression for the live-panel cancelTakeover race (2026-05-17):
        // a deferred mountInit() rAF could resume after cancelTakeover()
        // restored state.phase = "idle", walk through "requesting-camera"
        // and silently land us in "preview" — violating the L817-823
        // "operator chose Cancel = I want out" contract.
        //
        // Determinism: by holding mountInit's awaited promises with
        // manual `resolve` controllers and releasing them ONLY after
        // the cancel click, we force the race window open every run.
        // If a fix regresses, this test fails 100%, not 20%.
        const container = document.createElement("div");
        const conflict = Object.assign(new Error("live_already_active"), {
            code: "live_already_active",
            activeSessionId: "44",
            status: 409,
        });
        // Pending controllers for mountInit's two awaits: apiGetStatus
        // and getUserMedia. mountInit awaits these; until we resolve
        // them, mountInit is suspended at its first/second `await`.
        let resolveMountStatus;
        let resolveMountGUM;
        const mountStatusPromise = new Promise((r) => {
            resolveMountStatus = r;
        });
        const mountGUMPromise = new Promise((r) => {
            resolveMountGUM = r;
        });
        let statusCallCount = 0;
        let getUserMediaCallCount = 0;
        const fakePc = makeFakePc();
        const fakeStream = makeFakeStream();
        const opts = defaultMounts({
            apiGetStatus: vi.fn(async () => {
                statusCallCount++;
                // Call #1 is goLive's own pre-flight (mountInit's rAF
                // hasn't fired yet — goLive skips its mountInitPromise
                // await because the promise is null at click time).
                // Call #2 is mountInit's, fired after rAF; that's the
                // one we hold so its resumption races cancelTakeover.
                if (statusCallCount === 2) {
                    await mountStatusPromise;
                }
                return {
                    state: "idle",
                    session_id: null,
                    tier: { name: "basic", max_width: 854, max_height: 480, max_fps: 30 },
                };
            }),
            apiStartLive: vi.fn(async () => {
                throw conflict;
            }),
            getUserMedia: vi.fn(async () => {
                getUserMediaCallCount++;
                // Call #1 is goLive's (opens camera before negotiate);
                // call #2 is mountInit's. Hold #2 so mountInit is
                // suspended at openLocalCamera when cancel fires.
                if (getUserMediaCallCount === 2) {
                    await mountGUMPromise;
                }
                return fakeStream;
            }),
            createPeerConnection: vi.fn(() => fakePc),
        });
        const handle = mountLivePanel(container, opts);

        // Reach take-over-prompt via goLive (which does its own
        // pre-flight + 409). mountInit is suspended at its first await.
        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        // Cancel — synchronously sets phase = "idle" AND latches the
        // mountInitCancelled flag (the Option A fix).
        container.querySelector(".live-cancel-takeover").click();
        expect(handle.getState()).toBe("idle");

        // NOW release mountInit's two awaits. Pre-fix this would walk:
        //   apiGetStatus (idle) → L862 gate sees phase==="idle", continues
        //   → state.phase = "requesting-camera" → openLocalCamera
        //   → state.phase = "preview"
        // Post-fix: the flag-set in cancelTakeover blocks L862, and
        // even if mountInit had already passed L862, the flag also
        // blocks L874 + the camera-fail catch at L910.
        resolveMountStatus();
        resolveMountGUM();
        // Pump enough ticks for both awaits + any downstream microtasks
        // to complete. 5 ticks is overkill but cheap.
        for (let i = 0; i < 5; i++) await tick();

        expect(handle.getState()).toBe("idle");
    });

    it("Take over → live: hits the takeover endpoint", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts({
            apiGetStatus: vi.fn(async () => ({
                state: "active",
                session_id: "33",
                tier: { name: "basic", max_width: 854, max_height: 480, max_fps: 30 },
            })),
        });
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        container.querySelector(".live-take-over").click();
        await waitFor(() => handle.getState() === "live");

        expect(opts.apiTakeoverLive).toHaveBeenCalledTimes(1);
        expect(opts.apiStartLive).not.toHaveBeenCalled();
    });

    it("Cancel from take-over-prompt returns to idle without negotiating", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts({
            apiGetStatus: vi.fn(async () => ({
                state: "active",
                session_id: "33",
                tier: { name: "basic", max_width: 854, max_height: 480, max_fps: 30 },
            })),
        });
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        container.querySelector(".live-cancel-takeover").click();
        await tick();
        expect(handle.getState()).toBe("idle");
        expect(opts.apiTakeoverLive).not.toHaveBeenCalled();
    });

    it("flip-camera button surfaces in preview, swaps facingMode, and persists choice", async () => {
        // Re-§5.11 (qarl 2026-05-01): flip is back. Idle has no stream
        // open so the button is hidden; preview lights it up; click
        // re-getUserMedias with the opposite facingMode and persists
        // to localStorage so the next mount resumes there.
        //
        // jsdom in this vitest setup ships a stripped Storage (the
        // `--localstorage-file` warning at run time + spyOn-on-getItem
        // failing because the methods don't exist) — so swap in a real
        // backing Map via Object.defineProperty for the duration of
        // the test, then restore.
        const backing = new Map();
        const stub = {
            getItem: (k) => (backing.has(k) ? backing.get(k) : null),
            setItem: (k, v) => backing.set(k, String(v)),
            removeItem: (k) => backing.delete(k),
        };
        const orig = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
        Object.defineProperty(globalThis, "localStorage", {
            configurable: true,
            value: stub,
        });
        try {
            const container = document.createElement("div");
            const opts = defaultMounts();
            const handle = mountLivePanel(container, opts);
            await waitFor(() => handle.getState() === "preview");

            const flipBtn = container.querySelector(".live-flip-camera");
            expect(flipBtn).not.toBeNull();
            expect(flipBtn.hidden).toBe(false);

            // Initial open used the default 'environment' facingMode.
            expect(
                opts.getUserMedia.mock.calls[0][0].video.facingMode,
            ).toBe("environment");

            flipBtn.click();
            // Wait on the LAST side-effect of flipCamera (the write)
            // rather than getUserMedia's call count — the latter
            // increments synchronously when the mock fn is invoked but
            // before its returned promise resolves, which races the
            // post-await code (state mutation + writeFacingModePref).
            await waitFor(
                () => backing.get("openmarquee:live:facing-mode") === "user",
            );
            expect(
                opts.getUserMedia.mock.calls[1][0].video.facingMode,
            ).toBe("user");
        } finally {
            if (orig) {
                Object.defineProperty(globalThis, "localStorage", orig);
            } else {
                delete globalThis.localStorage;
            }
        }
    });

    it("flip-camera while live calls replaceTrack on the active video sender (no PC teardown)", async () => {
        // §5.11: mid-stream flip is hot-swappable via track.replaceTrack.
        // The publisher stays connected, no SDP renegotiation, no
        // closeup of the existing PC.
        const container = document.createElement("div");
        const fakePc = makeFakePc();
        const initialVideoTrack = { kind: "video", stop: vi.fn() };
        // Mock localStream for the FIRST getUserMedia → tracks include
        // the initial video track; subsequent calls return a fresh
        // stream with a fresh track.
        let mediaCallCount = 0;
        const opts = defaultMounts({
            createPeerConnection: vi.fn(() => fakePc),
            _fakePc: fakePc,
            getUserMedia: vi.fn(async () => {
                mediaCallCount += 1;
                const track = {
                    kind: "video",
                    stop: vi.fn(),
                    _id: `track-${mediaCallCount}`,
                };
                return {
                    getTracks: () => [track],
                    getVideoTracks: () => [track],
                };
            }),
        });
        // Wire a sender on the fake PC that the panel can replaceTrack on.
        const replaceTrack = vi.fn(async () => {});
        fakePc.getSenders = () => [
            { track: { kind: "video", _id: "track-1" }, replaceTrack },
        ];
        const handle = mountLivePanel(container, opts);
        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        const flipBtn = container.querySelector(".live-flip-camera");
        flipBtn.click();
        await waitFor(() => replaceTrack.mock.calls.length === 1);

        // Sender's replaceTrack received the freshly-opened video track.
        const passed = replaceTrack.mock.calls[0][0];
        expect(passed?.kind).toBe("video");
        // PC was NOT torn down — fakePc.closed flag stays false (the
        // helper sets it to true on close()).
        expect(fakePc.closed).toBe(false);
        // Phase still 'live'.
        expect(handle.getState()).toBe("live");
    });

    it("mount-init reads localStorage and opens the operator's last facingMode", async () => {
        const backing = new Map([["openmarquee:live:facing-mode", "user"]]);
        const stub = {
            getItem: (k) => (backing.has(k) ? backing.get(k) : null),
            setItem: (k, v) => backing.set(k, String(v)),
            removeItem: (k) => backing.delete(k),
        };
        const orig = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
        Object.defineProperty(globalThis, "localStorage", {
            configurable: true,
            value: stub,
        });
        try {
            const container = document.createElement("div");
            const opts = defaultMounts();
            const handle = mountLivePanel(container, opts);
            await waitFor(() => handle.getState() === "preview");
            expect(
                opts.getUserMedia.mock.calls[0][0].video.facingMode,
            ).toBe("user");
        } finally {
            if (orig) {
                Object.defineProperty(globalThis, "localStorage", orig);
            } else {
                delete globalThis.localStorage;
            }
        }
    });

    it("getUserMedia rejection lands in error phase with a message", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts({
            getUserMedia: vi.fn(async () => {
                throw new Error("Permission denied");
            }),
        });
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "error");

        expect(container.querySelector(".live-status").textContent).toMatch(
            /Permission denied/,
        );
        // Go Live becomes available again on error so the operator can
        // retry after granting permission.
        expect(container.querySelector(".live-go-live").hidden).toBe(false);
    });

    it("preview wrap aspect ratio mirrors the device's display dims on mount", async () => {
        // Phase 12.2 followup, qarl 2026-04-28 ask: operator sees the
        // actual cropping pre-Go-Live. Wrap aspect ratio = device's
        // display_width / display_height; CSS object-fit:cover on the
        // <video> mirrors the device-side aiortc subscriber's
        // cover-fit at playback time.
        const container = document.createElement("div");
        const opts = defaultMounts({
            fetchSettings: vi.fn(async () => ({
                display_width: 64,
                display_height: 32,
            })),
        });
        mountLivePanel(container, opts);
        await waitFor(
            () =>
                container
                    .querySelector(".live-preview-wrap")
                    .style.getPropertyValue("--om-live-aspect")
                    .replace(/\s+/g, "") === "64/32",
        );
        const wrap = container.querySelector(".live-preview-wrap");
        expect(
            wrap.style.getPropertyValue("--om-live-aspect").replace(/\s+/g, ""),
        ).toBe("64/32");
    });

    it("aspect ratio refreshes on openmarquee:settings-updated", async () => {
        // Operator changes display dims in Settings while the Stream
        // panel is mounted: the preview crop should reflect the new
        // ratio without needing a panel re-mount.
        const container = document.createElement("div");
        let dims = { display_width: 1920, display_height: 1080 };
        const opts = defaultMounts({
            fetchSettings: vi.fn(async () => dims),
        });
        const handle = mountLivePanel(container, opts);
        await waitFor(
            () =>
                container
                    .querySelector(".live-preview-wrap")
                    .style.getPropertyValue("--om-live-aspect")
                    .replace(/\s+/g, "") === "1920/1080",
        );

        // Simulate a Settings save: operator switched to a HUB75 panel.
        dims = { display_width: 64, display_height: 32 };
        document.dispatchEvent(new CustomEvent("openmarquee:settings-updated"));
        await waitFor(
            () =>
                container
                    .querySelector(".live-preview-wrap")
                    .style.getPropertyValue("--om-live-aspect")
                    .replace(/\s+/g, "") === "64/32",
        );
        expect(opts.fetchSettings).toHaveBeenCalledTimes(2);
        handle.destroy();
    });

    it("preview aspect swaps width/height when display_rotation is 90 or 270", async () => {
        // Phase 12.2 followup, qarl 2026-04-28: portrait-mounted signs
        // (rotation in {90, 270}) actually output height-by-width, so
        // the preview must swap dims or the operator's framing won't
        // match the installed orientation.
        const container = document.createElement("div");
        const opts = defaultMounts({
            fetchSettings: vi.fn(async () => ({
                display_width: 1920,
                display_height: 1080,
                display_rotation: 90,
            })),
        });
        mountLivePanel(container, opts);
        await waitFor(
            () =>
                container
                    .querySelector(".live-preview-wrap")
                    .style.getPropertyValue("--om-live-aspect")
                    .replace(/\s+/g, "") === "1080/1920",
        );
    });

    it("preview aspect passes through unchanged at display_rotation 0 / 180", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts({
            fetchSettings: vi.fn(async () => ({
                display_width: 1920,
                display_height: 1080,
                display_rotation: 180,
            })),
        });
        mountLivePanel(container, opts);
        await waitFor(
            () =>
                container
                    .querySelector(".live-preview-wrap")
                    .style.getPropertyValue("--om-live-aspect")
                    .replace(/\s+/g, "") === "1920/1080",
        );
    });

    it("Elapsed cell ticks against the server's started_at, not local Date.now", async () => {
        // Phase A.2: the backend stamps the session-start timestamp
        // and returns it in /start (and /status). The phone's Elapsed
        // counter subtracts that from wall-clock-now, so it's correct
        // even if the phone's clock is skewed and survives a panel
        // re-mount mid-stream. Local Date.now() is the deploy-stagger
        // fallback (server older than client); not exercised here.
        const container = document.createElement("div");
        // Server says the session started 65 seconds ago.
        const serverStartedAt = new Date(Date.now() - 65000).toISOString();
        const opts = defaultMounts({
            apiStartLive: vi.fn(async () => ({
                session_id: "11111111-1111-1111-1111-111111111111",
                sdp_answer: "v=0\r\nfake-answer\r\n",
                started_at: serverStartedAt,
            })),
        });
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        // First render reads state.startedAt and writes the cell.
        // 65s ago -> the Elapsed cell should read MM:SS where MM>=1
        // (1:05 +/- a tick of jitter from clock skew between the
        // mock-time fixture and Date.now). MM:SS=00:00 would mean
        // the server's started_at was ignored and the local Date.now
        // path fired — which is exactly the regression this test
        // pins against.
        const elapsedEl = container.querySelector('[data-metric="elapsed"]');
        const text = elapsedEl.textContent;
        expect(text).toMatch(/^\d\d:\d\d$/);
        const [mm, ss] = text.split(":").map(Number);
        const totalSec = mm * 60 + ss;
        // Tolerance for jitter: 60-75 inclusive. Lower bound catches
        // the regression (Date.now-fallback path renders 00:00); upper
        // bound is wide enough to absorb a heavily-loaded CI run
        // between serverStartedAt capture and the render assertion.
        expect(totalSec).toBeGreaterThanOrEqual(60);
        expect(totalSec).toBeLessThanOrEqual(75);
    });

    it("metrics cells populate from RTCPeerConnection.getStats() on the live transition (Phase B.1)", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");
        // First poll fires synchronously inside render() on the live
        // transition (so cells populate before the user sees them).
        // Wait for the latency cell to swap from the template's mock
        // '78 ms' to the polled value derived from the fake report's
        // roundTripTime of 0.078s — same number, but the path through
        // pollStats() proves the wiring.
        await waitFor(
            () =>
                container.querySelector('[data-metric="latency"]').textContent ===
                "78 ms",
        );
        // Dropped: pulled directly from remote-inbound-rtp.packetsLost.
        expect(
            container.querySelector('[data-metric="dropped"]').textContent,
        ).toBe("3");
    });

    it("bitrate cell renders Mbps once a delta is computable (poll #2)", async () => {
        // First poll caches; second-and-onward computes (bytesSent_now
        // - bytesSent_prev) * 8 / dMs. Use vitest fake timers to
        // advance the 1Hz setInterval deterministically — wall-clock
        // waits would race the test runner's tick scheduling.
        vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
        try {
            const container = document.createElement("div");
            const opts = defaultMounts();
            // Sample 1: 1MB at t=1_000_000ms.
            opts._fakePc._fakeStatsReport = new Map([
                [
                    "outbound-rtp-video",
                    {
                        type: "outbound-rtp",
                        kind: "video",
                        bytesSent: 1_000_000,
                        timestamp: 1_000_000,
                    },
                ],
                [
                    "remote-inbound-rtp-video",
                    {
                        type: "remote-inbound-rtp",
                        kind: "video",
                        roundTripTime: 0.05,
                        packetsLost: 0,
                    },
                ],
            ]);
            const handle = mountLivePanel(container, opts);
            container.querySelector(".live-go-live").click();
            // The Go Live click chain awaits multiple promises before
            // landing in live. Pump microtasks until the panel is in
            // live phase + the synchronous first pollStats() resolved
            // (latency cell rewritten to '50 ms' from the template's
            // '78 ms' default).
            for (let i = 0; i < 200 && handle.getState() !== "live"; i++) {
                await Promise.resolve();
            }
            for (
                let i = 0;
                i < 200 &&
                container.querySelector('[data-metric="latency"]').textContent !==
                    "50 ms";
                i++
            ) {
                await Promise.resolve();
            }
            // Sample 2: +1MB at t=+1000ms. With dBytes=1_000_000,
            // dMs=1000, expected bps = 1_000_000 * 8 * 1000 / 1000 =
            // 8_000_000 bps = 8.0 Mbps.
            opts._fakePc._fakeStatsReport.set("outbound-rtp-video", {
                type: "outbound-rtp",
                kind: "video",
                bytesSent: 2_000_000,
                timestamp: 1_001_000,
            });
            // Advance the fake clock to fire the next interval tick;
            // pollStats then returns a Promise that resolves on
            // microtask drain.
            vi.advanceTimersByTime(1000);
            for (
                let i = 0;
                i < 200 &&
                container.querySelector('[data-metric="bitrate"]').textContent ===
                    "2.8 Mbps";
                i++
            ) {
                await Promise.resolve();
            }
            expect(
                container.querySelector('[data-metric="bitrate"]').textContent,
            ).toBe("8.0 Mbps");
        } finally {
            vi.useRealTimers();
        }
    });

    it("latency falls back to candidate-pair RTT when remote-inbound-rtp has no roundTripTime yet", async () => {
        // Real-stream early-frames path: the candidate pair is up
        // (transport RTT available immediately) but the receiver
        // hasn't sent its first RTCP receiver report back, so
        // remote-inbound-rtp/video.roundTripTime is undefined. The
        // panel should fall back to candidate-pair.currentRoundTripTime
        // so the latency cell isn't stuck on the template default
        // through the first second or two of every stream.
        const container = document.createElement("div");
        const opts = defaultMounts();
        opts._fakePc._fakeStatsReport = new Map([
            [
                "outbound-rtp-video",
                {
                    type: "outbound-rtp",
                    kind: "video",
                    bytesSent: 50_000,
                    timestamp: 1_000_000,
                },
            ],
            [
                "remote-inbound-rtp-video",
                {
                    type: "remote-inbound-rtp",
                    kind: "video",
                    // roundTripTime intentionally absent — the receiver
                    // report hasn't arrived yet.
                    packetsLost: 0,
                },
            ],
            [
                "candidate-pair-nominated",
                {
                    type: "candidate-pair",
                    nominated: true,
                    currentRoundTripTime: 0.012, // 12 ms transport
                },
            ],
        ]);
        const handle = mountLivePanel(container, opts);
        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");
        await waitFor(
            () =>
                container.querySelector('[data-metric="latency"]').textContent ===
                "12 ms",
        );
        expect(
            container.querySelector('[data-metric="dropped"]').textContent,
        ).toBe("0");
    });

    it("simulateOnly Go Live keeps the mocked metric values (no PC to poll)", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts({ simulateOnly: true });
        // simulateOnly bypasses createPeerConnection; assert + signal
        // intent. (And reaching into _fakePc.getStats would be a
        // misleading test under simulateOnly anyway.)
        opts.createPeerConnection = vi.fn(() => {
            throw new Error("simulateOnly should not create a PC");
        });
        // simulateOnly also skips /start, so this test would fail if
        // pollStats fired against a non-existent PC.
        opts.apiStartLive = vi.fn(async () => {
            throw new Error("simulateOnly should not call /start");
        });
        const handle = mountLivePanel(container, opts);
        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");
        // Cells stay at the template's mock literals (the demo's
        // visible payoff). No PC, no real polling, no cell rewrites.
        expect(
            container.querySelector('[data-metric="latency"]').textContent,
        ).toBe("78 ms");
        expect(
            container.querySelector('[data-metric="bitrate"]').textContent,
        ).toBe("2.8 Mbps");
        expect(
            container.querySelector('[data-metric="dropped"]').textContent,
        ).toBe("0");
    });

    it("destroy() removes the settings-updated listener", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);
        await waitFor(() => opts.fetchSettings.mock.calls.length >= 1);
        handle.destroy();
        // After destroy, dispatching the event should NOT trigger
        // another fetchSettings — the listener was removed.
        const before = opts.fetchSettings.mock.calls.length;
        document.dispatchEvent(new CustomEvent("openmarquee:settings-updated"));
        await tick();
        expect(opts.fetchSettings.mock.calls.length).toBe(before);
    });

    it("destroy() tears down PC + tracks and clears the DOM", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        handle.destroy();
        expect(opts._fakePc.closed).toBe(true);
        expect(container.innerHTML).toBe("");
    });

    it("pagehide releases the camera even from preview (panel-mount preview should not leak the camera light when the tab closes)", async () => {
        // The "Stop returns to preview" change keeps the camera open
        // across Stop, but tab-close / app-background must still
        // release it — the camera light staying on after the tab
        // disappears is exactly the kind of bug operators would
        // notice. pagehide is the browser's last reliable hook.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        await waitFor(() => handle.getState() === "preview");
        const stream = await opts.getUserMedia.mock.results[0].value;
        const track = stream.getVideoTracks()[0];
        expect(track.stopped).toBe(false);

        window.dispatchEvent(new Event("pagehide"));
        expect(track.stopped).toBe(true);

        handle.destroy();
    });

    it("PC connectionState=failed mid-stream resets the panel out of live", async () => {
        // §5.11 failure modes: phone loses connectivity, PC enters
        // disconnected/failed. Without this listener, the backend times
        // out after 10s but the panel keeps showing "Live." indefinitely.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        opts._fakePc._setConnectionState("failed");
        await waitFor(() => handle.getState() === "error");

        expect(container.querySelector(".live-status").textContent).toMatch(
            /Connection failed/,
        );
        // Tracks were stopped so the camera light goes off; PC closed
        // by failTo's teardownPC call.
        expect(opts._fakePc.closed).toBe(true);
    });

    it("PC connectionState=disconnected also resets out of live", async () => {
        // disconnected can be transient in WebRTC, but for v1 we treat
        // it the same as failed — operator can re-tap Go Live to retry.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        opts._fakePc._setConnectionState("disconnected");
        await waitFor(() => handle.getState() === "error");
        expect(container.querySelector(".live-status").textContent).toMatch(
            /disconnected/,
        );
    });

    it("simulateOnly: Go Live skips PC creation + /api/live/start, still flips to live", async () => {
        // The openmarquee.com/demo bundle has no real peer — the panel
        // still needs to demo end-to-end (camera open, live state,
        // Tailscale warning) without actually negotiating WebRTC.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, {
            ...opts,
            simulateOnly: true,
        });

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        // Camera was opened (the local preview is the visible payoff
        // of the demo) but no PC was created and no /start was called.
        expect(opts.getUserMedia).toHaveBeenCalledTimes(1);
        expect(opts.createPeerConnection).not.toHaveBeenCalled();
        expect(opts.apiStartLive).not.toHaveBeenCalled();
        // Live state surfaces normally — Stop visible, LIVE pill on the viewfinder.
        expect(container.querySelector(".live-stop").hidden).toBe(false);
        expect(container.querySelector(".live-live-pill").hidden).toBe(false);
    });

    it("simulateOnly: Stop returns to preview without calling /api/live/stop", async () => {
        // The session_id was minted locally — the backend never knew
        // about it, so /stop would 404. Skip the call entirely.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, {
            ...opts,
            simulateOnly: true,
        });

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        container.querySelector(".live-stop").click();
        await waitFor(() => handle.getState() === "preview");

        expect(opts.apiStopLive).not.toHaveBeenCalled();
    });

    it("connectionstatechange listener ignores our own pc.close() teardown", async () => {
        // Regression: teardownPC nulls state.pc BEFORE close() so the
        // 'closed' connectionstatechange that fires from our own
        // teardown doesn't re-enter failTo and double-render. Without
        // this ordering, Stop would briefly land in 'error' before
        // settling on 'idle'.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountLivePanel(container, opts);

        container.querySelector(".live-go-live").click();
        await waitFor(() => handle.getState() === "live");

        const pc = opts._fakePc;
        // Capture original close so we can fire 'closed' after the
        // panel's stop logic has nulled state.pc.
        const origClose = pc.close.bind(pc);
        pc.close = function () {
            origClose();
            pc._setConnectionState("closed");
        };

        container.querySelector(".live-stop").click();
        await waitFor(() => handle.getState() === "preview");
        // Did NOT pass through 'error' — the listener saw state.pc !== pc
        // (already nulled by teardownPC) and skipped failTo. Stop lands
        // in preview (Phase 12.2 followup: camera stays open).
        expect(handle.getState()).toBe("preview");
    });

    // --- VLC (RTSP) source (STREAM/VLC slice 5) ---------------------------

    function mountVlc(overrides = {}) {
        // Mount directly in VLC mode by pre-seeding the persisted
        // source-mode pref — sidesteps racing the camera-mode
        // mount-init that the default (Camera) mount would run.
        globalThis.localStorage.setItem(
            "openmarquee:live:source-mode",
            "vlc",
        );
        const container = document.createElement("div");
        const opts = defaultMounts(overrides);
        const handle = mountLivePanel(container, opts);
        return { container, opts, handle };
    }

    it("renders the source toggle with Camera selected by default", () => {
        const container = document.createElement("div");
        mountLivePanel(container, defaultMounts());
        expect(container.querySelectorAll(".live-source-opt").length).toBe(2);
        const camera = container.querySelector('[data-source="camera"]');
        const vlc = container.querySelector('[data-source="vlc"]');
        expect(camera.classList.contains("is-selected")).toBe(true);
        expect(vlc.classList.contains("is-selected")).toBe(false);
        // Camera mode: viewfinder shown, VLC panel hidden.
        expect(container.querySelector(".live-stage").hidden).toBe(false);
        expect(container.querySelector(".live-vlc-panel").hidden).toBe(true);
    });

    it("switching to VLC shows the URL panel + Start streaming, hides the viewfinder", () => {
        const container = document.createElement("div");
        mountLivePanel(container, defaultMounts());
        container.querySelector('[data-source="vlc"]').click();
        expect(container.querySelector(".live-vlc-panel").hidden).toBe(false);
        expect(container.querySelector(".live-stage").hidden).toBe(true);
        expect(container.querySelector(".live-start-vlc").hidden).toBe(false);
        expect(container.querySelector(".live-go-live").hidden).toBe(true);
    });

    it("Start streaming in VLC mode calls the rtsp API and goes live", async () => {
        const { container, opts, handle } = mountVlc();
        container.querySelector(".live-vlc-url").value =
            "rtsp://laptop:8554/live";
        container.querySelector(".live-start-vlc").click();
        await waitFor(() => handle.getState() === "live");
        expect(opts.apiStartRtspLive).toHaveBeenCalledWith(
            "rtsp://laptop:8554/live",
        );
        // The WebRTC start path must NOT have fired.
        expect(opts.apiStartLive).not.toHaveBeenCalled();
        expect(container.querySelector(".live-stop").hidden).toBe(false);
        // VLC mode has no viewfinder (camera stage hidden) and no
        // PC.getStats() metrics grid.
        expect(container.querySelector(".live-stage").hidden).toBe(true);
        expect(container.querySelector(".live-metrics-grid").hidden).toBe(true);
    });

    it("VLC start with an empty URL surfaces a message and skips the API", async () => {
        const { container, opts, handle } = mountVlc();
        container.querySelector(".live-vlc-url").value = "   ";
        container.querySelector(".live-start-vlc").click();
        await tick();
        expect(opts.apiStartRtspLive).not.toHaveBeenCalled();
        expect(handle.getState()).toBe("idle");
        expect(
            container.querySelector(".live-status").textContent,
        ).toMatch(/stream URL/i);
    });

    it("VLC live → Stop calls stopLive and returns to idle", async () => {
        const { container, opts, handle } = mountVlc();
        container.querySelector(".live-vlc-url").value =
            "rtsp://laptop:8554/live";
        container.querySelector(".live-start-vlc").click();
        await waitFor(() => handle.getState() === "live");
        container.querySelector(".live-stop").click();
        await waitFor(() => handle.getState() === "idle");
        expect(opts.apiStopLive).toHaveBeenCalledWith(
            "33333333-3333-3333-3333-333333333333",
        );
        // VLC has no preview phase — Stop lands in idle, not preview.
        expect(handle.getState()).toBe("idle");
    });

    it("VLC start persists the URL + source mode to localStorage", async () => {
        const { container, handle } = mountVlc();
        container.querySelector(".live-vlc-url").value = "rtsp://host:8554/x";
        container.querySelector(".live-start-vlc").click();
        await waitFor(() => handle.getState() === "live");
        expect(
            globalThis.localStorage.getItem("openmarquee:live:url"),
        ).toBe("rtsp://host:8554/x");
        expect(
            globalThis.localStorage.getItem("openmarquee:live:source-mode"),
        ).toBe("vlc");
    });

    it("the source toggle is disabled while a VLC stream is live", async () => {
        const { container, handle } = mountVlc();
        container.querySelector(".live-vlc-url").value = "rtsp://host:8554/x";
        container.querySelector(".live-start-vlc").click();
        await waitFor(() => handle.getState() === "live");
        for (const opt of container.querySelectorAll(".live-source-opt")) {
            expect(opt.disabled).toBe(true);
        }
    });

    it("a 409 from the rtsp start surfaces the take-over prompt", async () => {
        const conflict = new Error("live_already_active");
        conflict.code = "live_already_active";
        const { container, handle } = mountVlc({
            apiStartRtspLive: vi.fn(async () => {
                throw conflict;
            }),
        });
        container.querySelector(".live-vlc-url").value = "rtsp://host:8554/x";
        container.querySelector(".live-start-vlc").click();
        await waitFor(() => handle.getState() === "take-over-prompt");
        expect(container.querySelector(".live-take-over").hidden).toBe(false);
    });

    it("Take over in VLC mode calls the rtsp takeover API", async () => {
        const conflict = new Error("live_already_active");
        conflict.code = "live_already_active";
        const { container, opts, handle } = mountVlc({
            apiStartRtspLive: vi.fn(async () => {
                throw conflict;
            }),
        });
        container.querySelector(".live-vlc-url").value = "rtsp://host:8554/x";
        container.querySelector(".live-start-vlc").click();
        await waitFor(() => handle.getState() === "take-over-prompt");
        container.querySelector(".live-take-over").click();
        await waitFor(() => handle.getState() === "live");
        expect(opts.apiTakeoverRtspLive).toHaveBeenCalledWith(
            "rtsp://host:8554/x",
        );
        // The WebRTC takeover path must NOT have fired.
        expect(opts.apiTakeoverLive).not.toHaveBeenCalled();
    });

    it("VLC Take over with an empty URL surfaces a message and skips the API", async () => {
        const conflict = new Error("live_already_active");
        conflict.code = "live_already_active";
        const { container, opts, handle } = mountVlc({
            apiStartRtspLive: vi.fn(async () => {
                throw conflict;
            }),
        });
        container.querySelector(".live-vlc-url").value = "rtsp://host:8554/x";
        container.querySelector(".live-start-vlc").click();
        await waitFor(() => handle.getState() === "take-over-prompt");
        // Clear the URL, then tap Take over — it must not call the API.
        container.querySelector(".live-vlc-url").value = "   ";
        container.querySelector(".live-take-over").click();
        await tick();
        expect(opts.apiTakeoverRtspLive).not.toHaveBeenCalled();
        expect(
            container.querySelector(".live-status").textContent,
        ).toMatch(/stream URL/i);
    });

    it("switching from camera to VLC releases the camera", async () => {
        // Mount in the default camera mode so mount-init opens the
        // camera; the switch to VLC must then stop its tracks so the
        // camera light goes off.
        const track = makeFakeTrack();
        const stream = makeFakeStream({ tracks: [track] });
        const container = document.createElement("div");
        const handle = mountLivePanel(
            container,
            defaultMounts({ getUserMedia: vi.fn(async () => stream) }),
        );
        await waitFor(() => handle.getState() === "preview");
        expect(track.stopped).toBe(false);

        container.querySelector('[data-source="vlc"]').click();

        expect(track.stopped).toBe(true);
        expect(container.querySelector(".live-vlc-panel").hidden).toBe(false);
    });
});
