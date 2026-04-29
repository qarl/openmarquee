# Phase B Flock — Scope Notes

Status: **partially shipped**. First written 2026-04-29 by
jimmy:openmarquee-code from a code archaeology pass; updated
2026-04-29 after the first three sub-phases shipped + QA's framing
clarified the remaining items against SYSTEM_SPEC §13's canonical
list. Items below match §13 / QA's framing rather than the original
draft's speculation.

§13 calls out three Phase B items: **peer health endpoint** +
**introduction protocol (gossip-on-add)** + **magicDNS auto-
discovery**. The original draft of this doc proposed a "periodic
probe consumer" that wasn't in §13; that idea is dropped (peers
discover each other on-demand via /api/system/info reads at render
time, not via a periodic backend worker).

---

## Phase A: where we are (factual)

The flock data model and CRUD surface shipped in earlier phases.
What's live today:

- `FlockPeer` model (`backend/openmarquee/flock.py`) carries
  `id`, `address`, `name`, `sync`, `added_at`, `last_seen_at`. Plus
  four health fields — `model`, `mode`, `signal`, `uptime`.
- `api_flock.py` exposes the CRUD surface (`GET / POST / PATCH /
  DELETE /api/flock`), the manifest pull route
  (`/api/flock/manifest`), and the sync ingress endpoints
  (`/api/flock/notify`, `/api/flock/sync-announce`).
- `flock_sync.py` implements the push-on-change notify path. Pull-
  on-notify is the receiver semantics.
- The flock UI (`ui/src/flock.js`) renders the peer grid.

## Phase B sub-phases

### B.1 — Backend: per-device `/api/system/info` endpoint ✅ SHIPPED

Commit `8e1923e`. Reads `/proc/device-tree/model` (with /proc/cpuinfo
fallback), `/proc/net/wireless` link-quality column scaled to 0-100,
`/proc/uptime` two-unit-truncated, and the configured display mode.
macOS/dev-box fallback returns sentinel values matching the old
`SELF_PLACEHOLDER_*` constants in flock.js. Source field reports
`proc` / `fallback` / `mixed` so a partial-success read is
debuggable from the wire alone.

### B.2 — Frontend: self-card reads `/api/system/info` ✅ SHIPPED

Commit `92acb09`. Replaces the `SELF_PLACEHOLDER_*` constants in
`flock.js` with reads from `/api/system/info`. The self-card stats
row now ticks against /proc-driven values on real hardware. The
bespoke `output_mode → slug` derivation in render() is gone — the
backend's `_format_mode` now owns the slug shape, fixing a pre-
existing inconsistency where the UI emitted "ws2812-strip" while
the backend used "ws281x-strip". Mount-time-gap and failure-path
fallbacks preserved via parameter defaults on `selfCardHTML`.

### gossip-on-add — Introduction protocol per §13 ✅ SHIPPED

Commit `a05184b`. Closes §13's "Peer introduction (gossip on add)"
verbatim: when peer B is added to A's flock, A reciprocally hello-
pings B (so B adds A) AND notifies existing peers C/D/E (so each
adds B). Full-mesh after settling — operator only has to "Add
Peer" on one device.

Implementation:
- `POST /api/flock/hello` accepts `{address: string}`. Idempotent;
  does NOT 403 unknown senders (the entire point of an introduction
  protocol is bootstrap). Does NOT cascade — the loop-prevention
  invariant.
- `FlockSync.gossip_add(new_peer_address)` fans out via httpx, one
  POST per existing peer + one to the new peer. Per-peer failures
  swallowed via `asyncio.gather(return_exceptions=True)` — eventual-
  consistency model handles dropouts.
- Membership gossip is intentionally NOT gated by
  `flock_sync_enabled` — flock membership is a property of the
  flock, not of content sync.

### B.3 — Out-of-sync diff ✅ SHIPPED (draft, four TODO(qarl-confirm) blocks for review)

Backend `5e8023f`, frontend `7a8eda9`, demo wire fix `1a5ec00`.
QA verifier 5/5 PASS, regression clean across 6 prior suites.

`FlockPeer.items_behind: int | None` is computed during the
existing `pull_from_peer` reconcile pre-apply and stamped onto the
peer record. Frontend folds the count into the existing sync-state
pill: when sync=True + online + items_behind > 0 the pill reads
"K items behind" instead of "syncing"; caught-up (0) and never-
pulled (null) stay at "syncing". Mock-backend synthesizes per
peer at /api/flock GET-time so the demo Flock tab exercises the
new affordance even on stale seed.json.

Four decision points shipped as defaults with TODO(qarl-confirm)
blocks for him to flip:

  1. Semantic direction (we're behind them vs they're behind us).
  2. sync=False handling (None vs what-if-preview).
  3. Tombstone inclusion in the count.
  4. Stale items_behind on sync=True→False flip.

Below text preserved for historical context — was the original
"PENDING" framing before the draft shipped:



QA's framing: surface "N items behind" on each peer card so the
operator sees content drift at a glance. The flock.js comment at
line 111 anchors this:
> Phase B layers on "out-of-sync N items behind" once we have a
> real comparison.

Decision points needing qarl input:

- **Source of truth for the diff**: cached manifest (populated by
  the existing pull worker in flock_sync.py:299) vs probe-on-demand
  during render. The pull-worker path is faster but stale-bound by
  the pull cadence; the probe-on-demand path is fresh but adds
  latency to the flock UI's render. (Cached is the right answer
  for §13's "no central coordinator, no distributed-state
  machinery" framing — a render-time probe IS distributed state.)
- **Display granularity**: integer items-behind count vs
  minutes-since-last-sync vs a binary in-sync/out-of-sync pill.
  Design probably has an opinion (chat2.md may have framed it; QA's
  earlier reports may reference it).
- **Hash shape on the manifest endpoint**: the existing
  `/api/flock/manifest` returns full content lists. For diff
  computation the local device hashes both sides (its own + the
  peer's cached manifest) and compares. Roll out depends on whether
  the manifest endpoint needs to grow a `content_set_hash` field
  for cheap-comparison or whether full-list-hash-on-the-fly is
  acceptable for flocks of <50 items.

Could ship as: a `/api/flock/{peer_id}/diff` endpoint that returns
`{items_behind: int}` computed against the cached manifest +
local content storage. UI consumes it on render. Renders "N items
behind" badge on peer cards when nonzero.

### B.5 — Tailscale magicDNS auto-discovery (PENDING qarl input) {#b5}

§13's third Phase B item. Today's UX: operator manually types the
peer's tailnet address into the "Add Peer" modal. Phase B can
auto-suggest peers from the local Tailscale node's `peers` list
(via `tailscale status --json`).

Decision points needing qarl input:

- **Shell-out vs Tailscale Local API**: `tailscale status --json`
  is the easy path but requires the binary to be in $PATH and
  privileges to query. The Local API at `/var/run/tailscale/
  tailscaled.sock` is more robust but needs Unix-socket plumbing
  through aiohttp (or httpx-uds).
- **Filtering**: not every tailnet peer is an openMarquee device.
  Probe each candidate's `/api/system/info` (or a marker endpoint
  like `/api/system/_is_openmarquee`) before adding to the
  suggestions list?
- **UX**: list suggestions inline in the Add Peer modal, or a
  separate "Discover" affordance? Real-time refresh, or one-shot
  on modal open?
- **Dev-box fallback**: same shape as `/api/system/info` —
  return empty suggestions on macOS / dev where Tailscale isn't
  installed, fall through to the manual-typed path that exists
  today.

Could ship as: a `GET /api/flock/discover` endpoint that returns
`{candidates: [{address, hostname, is_openmarquee}, ...]}`. The
Add Peer modal calls it on open and renders the list inline.

---

## What's been dropped

- **Periodic probe consumer (original B.3 in this doc's first
  draft)**. Not in §13. The flock self-card discovers /api/system/
  info on-demand at render; cross-peer health is via the existing
  `pull_from_peer` worker for content sync + the operator's manual
  "view this peer's flock card" UX. A separate periodic /info
  probe worker would duplicate effort — dropped.
- **Live updates (original B.5 in this doc's first draft)**. SSE
  / polling on the flock panel for live signal/uptime updates.
  Out of scope; the flock panel re-renders on operator action and
  on the existing 30s refresh cycle, which is fast enough for the
  operator's mental model of "this is the flock state right now".

---

## Suggested ordering (remaining)

- **B.4 gossip-on-add live-fire verification** — the protocol is
  fully shipped per §13 (a05184b); a single-backend demo can't
  exercise multi-peer round-trips. Real verification waits for
  Phase 6 hardware so two+ devices on a Tailnet can be observed
  reciprocally adding each other.
- **B.5 magicDNS auto-discovery** — bigger scope, Tailscale-binary-
  dependent. Could ship as a draft via the same TODO(qarl-confirm)
  pattern B.3 used: shell-out to `tailscale status --json`, return
  candidates with an `is_openmarquee` filter probe, dev-fallback
  to empty list. Implementation possible without Phase 6; live-
  fire verification waits for it.

## Where this doc lives

`docs/phase-b-flock-scope.md`. Updated alongside major Phase B
commits. If qarl prefers planning docs out of the repo entirely,
feel free to relocate or delete — this is a working artifact, not
a contract.
