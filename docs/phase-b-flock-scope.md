# Phase B Flock — Scope Notes

Status: **draft, awaiting qarl review**. Written 2026-04-29 by
jimmy:openmarquee-code from a code archaeology pass on `flock.py`,
`api_flock.py`, `flock_sync.py`, and `flock.js`. The Phase A
landmarks below are factual; the Phase B sub-phase decomposition is
proposed and likely needs adjustment before any commit.

---

## Phase A: where we are

The flock data model and CRUD surface shipped in earlier phases.
What's live today:

- `FlockPeer` model (`backend/openmarquee/flock.py`) carries
  `id`, `address`, `name`, `sync`, `added_at`, `last_seen_at`. Plus
  four health fields — `model`, `mode`, `signal`, `uptime` — that
  are all `None`-default and explicitly flagged as "Populated by
  Phase B health probes" in the field docstrings.
- `api_flock.py` exposes the CRUD surface (`GET / POST / PATCH /
  DELETE /api/flock`), the manifest pull route
  (`/api/flock/manifest`), and the sync ingress endpoints
  (`/api/flock/notify`, `/api/flock/sync-announce`).
- `flock_sync.py` implements the push-on-change notify path. Pull-
  on-notify is the receiver semantics. Loop prevention is at the
  HTTP-layer hook, not in storage.
- The flock UI (`ui/src/flock.js`) renders the peer grid. For the
  current device's own card it falls back to module-scoped
  `SELF_PLACEHOLDER_MODEL = "Pi Zero 2 W"`,
  `SELF_PLACEHOLDER_SIGNAL = 100`,
  `SELF_PLACEHOLDER_UPTIME = "up since boot"`. The TODO comment at
  `ui/src/flock.js:143` reads:
  > `TODO(phase-b): replace with real reads via the per-peer health
  > endpoint that'll replace these with /proc/cpuinfo,
  > /proc/net/wireless, /proc/uptime reads`.

So the data shape exists end-to-end; Phase B is about populating
it from real sources and propagating between peers.

## What Phase B needs to deliver

The minimum-viable Phase B is "the flock UI shows real model /
mode / signal / uptime for every peer, including the current
device". Decomposing into shippable commits:

### B.1 — Backend: per-device `/api/system/info` endpoint

A new local endpoint that reads `/proc/cpuinfo` (model),
`/proc/net/wireless` (signal), `/proc/uptime` (uptime), and the
configured display mode (mode = `output_mode-WxH` slug). Returns
the four values as a Pydantic model. Pure-local read, no flock
involvement yet.

This is the data SOURCE — once it exists, the local UI can use it
for the self-card (replacing `SELF_PLACEHOLDER_*` in `flock.js`),
and the flock probe consumer (B.3) has something to fetch from
each peer.

Decision points needing qarl input:
- Does `mode` belong on `/api/system/info` or on `/api/settings`
  (where `display_width` / `display_height` already live)? Probably
  the former — the flock card cares about the device-as-peer view,
  not the operator-edit view.
- macOS / dev-laptop fallback: `/proc/*` files don't exist outside
  Linux. What does `/api/system/info` return when `Path('/proc')`
  is missing? Probably hardcoded sentinel values matching the
  current `SELF_PLACEHOLDER_*`, gated on `os.uname().sysname`.

### B.2 — Frontend: self-card reads `/api/system/info`

Replace the three `SELF_PLACEHOLDER_*` constants in `flock.js` with
a `fetchSystemInfo()` call on mount. Same pattern as
`refreshPreviewAspect` in `stream-panel.js`: fetch on mount, cache
the result, render against it. No periodic refresh needed for the
self-card — uptime ticks slowly enough that a once-per-mount read
is fine.

Decision points needing qarl input:
- Mount-time fetch is fire-and-forget; shows the placeholder values
  until the fetch lands. Acceptable, or should the cells render
  em-dashes until the fetch completes? (The earlier flock self-card
  conversation explicitly chose placeholders over em-dashes for
  Phase A — same choice probably applies here.)

### B.3 — Backend: flock probe consumer

A periodic worker that hits each peer's `/api/system/info` (using
the existing `address` field as the host part of the URL) and
writes the result back into the local flock storage's per-peer
`model` / `mode` / `signal` / `uptime` fields. Plus stamps
`last_seen_at` on success.

Cadence: probably 30 seconds (matches the current `last_seen_at`
freshness window the UI uses for online/offline rendering).
Concurrency: aiohttp fan-out across peers, bounded by a small
semaphore so a flock of 50+ doesn't thundering-herd Tailscale.

Decision points needing qarl input:
- Probe failure: stamp `last_seen_at = None` on N consecutive
  failures? Leave the last-known values in place when the peer
  goes offline, or null them out on first failure?
- Backoff: linear, exponential, or just-keep-trying-at-30s?
- Where does the worker live? `flock_sync.py` already has the push
  worker shape; could co-locate. Or new `flock_health.py` module.
- Is the probe also the `last_seen_at` source, or is that still
  driven by `flock_sync.py`'s notify ingress? (Today nothing
  populates `last_seen_at` automatically — the demo mock-backend
  stamps it at GET-time per the `/api/flock` handler.)

### B.4 — Frontend: out-of-sync indicator

The earlier flock UI conversation referenced a "sync paused" state
and an out-of-sync count. The flock.js comment at line 111 reads:
> `Phase B layers on "out-of-sync N items behind" once we have a
> real comparison`.

Likely shape: peer's manifest carries a content-set hash + count;
local device compares against its own; the UI surfaces the delta
on the peer card. Touches `flock_sync.py` (manifest shape extension),
`api_flock.py` (manifest endpoint augmentation), and `flock.js`
(rendering).

Decision points needing qarl input:
- Manifest hash shape: rolling content-set hash, per-item ETag
  list, or last-modified timestamp? Existing manifest endpoint
  shape would need to grow.
- Where does the indicator render? Subtitle on the peer card, or a
  badge alongside the sync-state pill?

### B.5 — Live updates (optional)

The flock UI today re-fetches `/api/flock` on mount + on user
actions (add / remove). Phase A behavior. Phase B could push
updates from the backend (server-sent events or polling) so a
peer's signal / uptime / out-of-sync count refreshes in place
without an operator action.

Decision points needing qarl input:
- Is this scope-creep for Phase B, or in-scope? The current UI
  surface is acceptable without it — the self-card + per-peer
  probe results that are 30s-stale match the operator's mental
  model of "this is the flock state right now".

---

## Suggested ordering

1. B.1 (backend `/api/system/info`) — small, no dependencies,
   shippable solo. Doesn't change the flock UI.
2. B.2 (frontend self-card) — depends on B.1, demo-visible win.
3. B.3 (probe consumer) — depends on B.1. Bigger scope; needs the
   most qarl input on the failure/backoff/cadence questions.
4. B.4 (out-of-sync indicator) — depends on B.3 conceptually but
   can be its own commit.
5. B.5 (live updates) — defer until after B.4 if at all.

B.1 + B.2 together would close the SELF_PLACEHOLDER TODO in
flock.js without needing any of the harder decisions in B.3+.
That's the smallest visible improvement, and a clean stopping
point if qarl wants to gate the rest of Phase B on real hardware
(B.3 only meaningfully exercises against multiple peers on a
Tailnet — Phase 6 hardware bring-up).

## Where this doc lives

`docs/phase-b-flock-scope.md`. The directory README lists planned
docs (hardware, dev-setup, architecture, building-the-image) but
explicitly says "as we write them"; a phase-scope sketch fits.

If qarl's preference is to keep planning docs in a separate
location (or out of the repo entirely), feel free to relocate or
delete — this is a decision-point dump, not a contract.
