// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mountStreamPanel } from "./stream-panel.js";

beforeEach(() => {
    vi.stubGlobal("RTCPeerConnection", undefined);
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
        apiStartStream: vi.fn(async () => ({
            session_id: "11111111-1111-1111-1111-111111111111",
            sdp_answer: "v=0\r\nfake-answer\r\n",
        })),
        apiTakeoverStream: vi.fn(async () => ({
            session_id: "22222222-2222-2222-2222-222222222222",
            sdp_answer: "v=0\r\nfake-answer\r\n",
        })),
        apiStopStream: vi.fn(async () => undefined),
        getUserMedia: vi.fn(async () => makeFakeStream()),
        createPeerConnection: vi.fn(() => fakePc),
        _fakePc: fakePc,
        ...overrides,
    };
}

// --- Tests --------------------------------------------------------------

describe("mountStreamPanel", () => {
    it("renders Go Live in idle, Stop hidden, warning hidden", () => {
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountStreamPanel(container, opts);

        expect(container.querySelector(".stream-go-live").hidden).toBe(false);
        expect(container.querySelector(".stream-stop").hidden).toBe(true);
        expect(container.querySelector(".stream-warning").hidden).toBe(true);
        expect(handle.getState()).toBe("idle");
    });

    it("Go Live → live: opens camera, negotiates, flips to live phase", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "live");

        expect(opts.apiGetStatus).toHaveBeenCalledTimes(1);
        expect(opts.getUserMedia).toHaveBeenCalledTimes(1);
        const constraints = opts.getUserMedia.mock.calls[0][0];
        // Default tier = back camera, no audio (per §5.11 NO AUDIO posture).
        expect(constraints.video.facingMode).toBe("environment");
        expect(constraints.audio).toBe(false);
        expect(opts.apiStartStream).toHaveBeenCalledTimes(1);
        // Live state: warning + stop visible, go-live hidden.
        expect(container.querySelector(".stream-stop").hidden).toBe(false);
        expect(container.querySelector(".stream-go-live").hidden).toBe(true);
        expect(container.querySelector(".stream-warning").hidden).toBe(false);
    });

    it("Stop → idle: posts session_id, returns to idle, stops local tracks", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "live");

        // Capture the local stream's tracks so we can verify they got
        // stopped on teardown — the camera light staying on after Stop
        // is exactly the kind of bug operators would notice.
        const stream = opts.getUserMedia.mock.results[0].value;
        const track = (await stream).getVideoTracks()[0];

        container.querySelector(".stream-stop").click();
        await waitFor(() => handle.getState() === "idle");

        expect(opts.apiStopStream).toHaveBeenCalledTimes(1);
        expect(opts.apiStopStream.mock.calls[0][0]).toBe(
            "11111111-1111-1111-1111-111111111111",
        );
        expect(track.stopped).toBe(true);
        expect(opts._fakePc.closed).toBe(true);
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
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        expect(opts.getUserMedia).not.toHaveBeenCalled();
        expect(opts.apiStartStream).not.toHaveBeenCalled();
        expect(container.querySelector(".stream-take-over").hidden).toBe(false);
        expect(container.querySelector(".stream-cancel-takeover").hidden).toBe(false);
        expect(container.querySelector(".stream-go-live").hidden).toBe(true);
    });

    it("/start returning 409 mid-flight transitions to take-over-prompt and tears down the half-open PC + camera", async () => {
        // Regression for the leak the pre-commit subagent caught: prior
        // version flipped phase to take-over-prompt without closing the
        // PC or stopping the camera tracks, so a subsequent Take Over
        // overwrote the references and orphaned them — camera light
        // stayed on, PC kept ICE'ing.
        const container = document.createElement("div");
        const conflict = Object.assign(new Error("stream_already_active"), {
            code: "stream_already_active",
            activeSessionId: "44",
            status: 409,
        });
        const stream = makeFakeStream();
        const fakePc = makeFakePc();
        const opts = defaultMounts({
            apiStartStream: vi.fn(async () => {
                throw conflict;
            }),
            getUserMedia: vi.fn(async () => stream),
            createPeerConnection: vi.fn(() => fakePc),
        });
        // _fakePc on opts is the default fixture; override here so we
        // can assert against the one actually used.
        opts._fakePc = fakePc;
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        expect(opts.getUserMedia).toHaveBeenCalled();
        expect(container.querySelector(".stream-take-over").hidden).toBe(false);
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
        const conflict = Object.assign(new Error("stream_already_active"), {
            code: "stream_already_active",
            activeSessionId: "44",
            status: 409,
        });
        const stream = makeFakeStream();
        const fakePc = makeFakePc();
        const opts = defaultMounts({
            apiStartStream: vi.fn(async () => {
                throw conflict;
            }),
            getUserMedia: vi.fn(async () => stream),
            createPeerConnection: vi.fn(() => fakePc),
        });
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        container.querySelector(".stream-cancel-takeover").click();
        await tick();
        expect(handle.getState()).toBe("idle");
        // Already torn down by goLive's catch; cancelTakeover's defensive
        // teardown is a no-op against an already-closed PC, which is the
        // important contract — calling teardown twice doesn't blow up.
        expect(fakePc.closed).toBe(true);
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
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        container.querySelector(".stream-take-over").click();
        await waitFor(() => handle.getState() === "live");

        expect(opts.apiTakeoverStream).toHaveBeenCalledTimes(1);
        expect(opts.apiStartStream).not.toHaveBeenCalled();
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
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "take-over-prompt");

        container.querySelector(".stream-cancel-takeover").click();
        await tick();
        expect(handle.getState()).toBe("idle");
        expect(opts.apiTakeoverStream).not.toHaveBeenCalled();
    });

    it("Flip Camera replaces the track without renegotiating SDP", async () => {
        const container = document.createElement("div");
        // Two distinct fake streams so we can watch the track swap.
        const firstStream = makeFakeStream();
        const secondStream = makeFakeStream();
        const opts = defaultMounts({
            getUserMedia: vi
                .fn()
                .mockResolvedValueOnce(firstStream)
                .mockResolvedValueOnce(secondStream),
        });
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "live");

        const senderBefore = opts._fakePc.getSenders()[0];
        const trackBefore = senderBefore.track;

        container.querySelector(".stream-flip-camera").click();
        await waitFor(() => senderBefore.track !== trackBefore);

        // facingMode flipped to "user" on the second getUserMedia call.
        expect(opts.getUserMedia.mock.calls[1][0].video.facingMode).toBe("user");
        // PC was NOT torn down — the same instance, same sender.
        expect(opts._fakePc.closed).toBe(false);
        expect(opts.apiStartStream).toHaveBeenCalledTimes(1);
        // First stream's tracks were stopped so the camera light goes
        // out on the previous lens.
        expect(firstStream.getVideoTracks()[0].stopped).toBe(true);
    });

    it("getUserMedia rejection lands in error phase with a message", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts({
            getUserMedia: vi.fn(async () => {
                throw new Error("Permission denied");
            }),
        });
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "error");

        expect(container.querySelector(".stream-status").textContent).toMatch(
            /Permission denied/,
        );
        // Go Live becomes available again on error so the operator can
        // retry after granting permission.
        expect(container.querySelector(".stream-go-live").hidden).toBe(false);
    });

    it("destroy() tears down PC + tracks and clears the DOM", async () => {
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "live");

        handle.destroy();
        expect(opts._fakePc.closed).toBe(true);
        expect(container.innerHTML).toBe("");
    });

    it("PC connectionState=failed mid-stream resets the panel out of live", async () => {
        // §5.11 failure modes: phone loses connectivity, PC enters
        // disconnected/failed. Without this listener, the backend times
        // out after 10s but the panel keeps showing "Live." indefinitely.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "live");

        opts._fakePc._setConnectionState("failed");
        await waitFor(() => handle.getState() === "error");

        expect(container.querySelector(".stream-status").textContent).toMatch(
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
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "live");

        opts._fakePc._setConnectionState("disconnected");
        await waitFor(() => handle.getState() === "error");
        expect(container.querySelector(".stream-status").textContent).toMatch(
            /disconnected/,
        );
    });

    it("connectionstatechange listener ignores our own pc.close() teardown", async () => {
        // Regression: teardownPC nulls state.pc BEFORE close() so the
        // 'closed' connectionstatechange that fires from our own
        // teardown doesn't re-enter failTo and double-render. Without
        // this ordering, Stop would briefly land in 'error' before
        // settling on 'idle'.
        const container = document.createElement("div");
        const opts = defaultMounts();
        const handle = mountStreamPanel(container, opts);

        container.querySelector(".stream-go-live").click();
        await waitFor(() => handle.getState() === "live");

        const pc = opts._fakePc;
        // Capture original close so we can fire 'closed' after the
        // panel's stop logic has nulled state.pc.
        const origClose = pc.close.bind(pc);
        pc.close = function () {
            origClose();
            pc._setConnectionState("closed");
        };

        container.querySelector(".stream-stop").click();
        await waitFor(() => handle.getState() === "idle");
        // Did NOT pass through 'error' — the listener saw state.pc !== pc
        // (already nulled by teardownPC) and skipped failTo.
        expect(handle.getState()).toBe("idle");
    });
});
