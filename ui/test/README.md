# Live-takeover test harness

A standalone HTML page that publishes a fake "camera" feed against the
device's `/api/live/*` endpoints — a browser-side stand-in for an
operator tapping **Go Live** on their phone. Lets us diagnose Mode A
(phone-camera takeover) without needing a real phone-on-glass each
iteration.

## Quick start

1. **Log in to the device first.** Open `http://<device>/` in your
   browser and complete the first-run / password flow. The harness
   reuses the bearer token the operator UI stores in `localStorage`
   under `openmarquee_auth_token`.
2. **Open the harness:** `http://<device>/test/fake-camera.html`.
3. **(Optional) Override the source clip** via the URL param:
   `?src=https://example.com/clip.mp4` or
   `?src=/api/content/<id>/video`. Default is the bundled
   `/test/fixture.mp4` (~80 KB, 320×240 testsrc2 pattern, 5 s loop).
4. Click **Start fake camera**. The page captures the playing
   `<video>` element via `HTMLMediaElement.captureStream()`,
   negotiates SDP with the backend, and pushes frames as a real
   WebRTC publisher would.
5. The sign takes over: playback pauses and the harness's video
   paints to HDMI until you click **Stop** or close the tab.

## ⚠️ This takes over the sign

Starting the harness preempts the playlist exactly like a real phone
tapping Go Live does. The sign's display shows the harness video
(looped) instead of the playlist until:

- you click **Stop**, OR
- you close the tab (the page sends a best-effort
  `/api/live/stop` via `fetch(..., {keepalive: true})` on
  `beforeunload`), OR
- you cross the 10-second phantom-track watchdog without ever
  sending a real track (the backend auto-closes), OR
- another caller hits `/api/live/takeover` and force-stops you.

If the sign gets stuck in Live mode (rare — the watchdog should
catch most cases), call `POST /api/live/stop` with the active
session id from `GET /api/live/status`, or reach the operator UI's
Live panel and tap its Stop button.

## Conflict / "Take over"

If another session is already active (a real phone, or a sibling
harness instance), `POST /api/live/start` returns 409. The harness
surfaces this in the Status pane with the active session ID and
exposes a **Take over** button that calls `/api/live/takeover`
instead.

## Where this fits in the dev loop

- **Slice 1 (this file):** the harness itself + a light wire-shape
  regression test in `backend/tests/test_api_live.py`.
- **Slice 2:** point the harness at FYS (or the dev Pi, or a local
  backend) and observe what fails. Five candidate failure modes
  (a-e) are listed in the scope report — the harness narrows which
  one.
- **Slice 3:** targeted fix for whichever link Slice 2 reveals.
- **Slice 4:** failure-mode-specific regression test.

See `docs/STREAM_VLC_PROPOSAL.md` for the broader Mode-A architecture
(takeover semantics, transport choices, frame-handoff path) and
`SYSTEM_SPEC.md` §5.11 for the canonical Stream spec.

## Files in this directory

- `fake-camera.html` — the harness itself (single file; pure HTML +
  inline JS module; no build step).
- `fixture.mp4` — bundled 320×240 testsrc2 clip generated via
  `ffmpeg -f lavfi -i testsrc2=duration=5:size=320x240:rate=15 …`.
  Procedurally generated test pattern — no third-party content,
  trivially regeneratable.

## Currently ships everywhere `ui/` ships

`scripts/deploy.sh` rsyncs `ui/` to the target Pi with excludes only
for `src/`, `e2e/`, `node_modules`, test result dirs, and a few
config files — there is no `--exclude 'test/'` today, so this
harness DOES land on any Pi deployed that way (dev Pi, FYS prod).
The pi-gen image build follows the same pattern. The auth-middleware
`/test/` prefix-whitelist is therefore active everywhere too.

That's deliberately not gated: the page itself exposes no secrets
(it's static HTML + JS), and its only side-effect — calling
`/api/live/*` — requires the operator's bearer token, which the
harness reads from `localStorage`. A future slice can add
`--exclude 'test/'` to `scripts/deploy.sh` + the pi-gen image stage
if we want this stripped from production. Out of scope for the
Slice 1 dispatch.
