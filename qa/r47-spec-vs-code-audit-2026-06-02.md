# r47 — SYSTEM_SPEC.md vs code audit (find every "already does X" claim that's a lie)

**Author:** jimmy:openmarquee-code2
**Date:** 2026-06-02
**Status:** SHIPPED on code2; cherry-picked to main
**Dispatch:** qarl-direct, after the §5.10 text-over-video gap surfaced on FYS
**Predecessors:**
  - r39 doc-drift fix (VideoSlide.duration_ms docstring) — `docs/`
  - r46 in flight (impl fix for §5.10 text-over-video)
  - All landed on main as of `4a0ba6d`+`221d02b`

## Goal

qarl's frustration: "we claim features ship when they don't."

This audit enumerates every concrete capability claim in
`SYSTEM_SPEC.md` (628 lines, 22 top-level sections), classifies
each against actual code, and adversarially verifies every
NOT_IMPLEMENTED finding through 3 independent skeptics with
distinct refutation lenses so we don't compound the trust
problem with another fallible single-Jimmy claim.

## Methodology

Workflow orchestration: `code/.claude/workflows/r47-spec-vs-code-audit.mjs`

  Phase 1 (Enumerate)    1 agent reads SYSTEM_SPEC, extracts capability claims
                         with schema {id, spec_ref, claim_quote, expected_code_surface}
  Phase 2 (Verify)       N parallel verifiers (8-claim batches) grep + read +
                         classify each claim as VERIFIED / PARTIAL / NOT_IMPLEMENTED /
                         NOT_VERIFIABLE_FROM_STATIC, with severity + fix LOC
  Phase 3 (Adversarial)  For each NOT_IMPLEMENTED, 3 skeptics with distinct
                         refutation lenses — alternate field names, indirect
                         implementation, wire-format translation — each tries
                         to REFUTE the verdict (default refuted=true, must
                         construct a concrete file:line counter-example to
                         flip). Majority-refutes (2+ of 3) → kill the finding.
  Phase 4 (Synthesize)   Aggregate into structured rows for this doc.

Stats:
  - 38 agents spawned (1 enumerator + 10 verifiers + 27 skeptics)
  - 1.5M subagent tokens
  - 16 min wall-clock
  - 74 capability claims audited (target was 30-60; spec was rich)
  - 18 aspirational claims explicitly parked from scope (§F)

## Executive summary

  Total claims audited:        74
  VERIFIED (claim matches):    53
  PARTIAL (gap exists):        12
  NOT_IMPLEMENTED (false):      9
  Adversarial flips:            0 (all 9 NOT_IMPLEMENTED survived 3-lens skeptic refute)

Severity histogram (PARTIAL + NOT_IMPLEMENTED):
  CRITICAL:     8  (7 NOT_IMPLEMENTED + 1 PARTIAL)
  MEDIUM:       5  (1 NOT_IMPLEMENTED + 4 PARTIAL)
  LOW:          9  (1 NOT_IMPLEMENTED + 7 PARTIAL + 1 VERIFIED-but-doc-drift)

The 9 NOT_IMPLEMENTED findings are NOT all the same shape:
  - **§5.10 / r46 shape** (feature never shipped): C014, C036 (PARTIAL),
    C074 — code is genuinely missing or stubbed.
  - **DELETE-PIL / spec-out-of-date shape** (implementation deliberately
    removed but spec text wasn't updated): C007, C030, C031, C032,
    C034, C046, C010 — spec describes the OLD behavior; code intentionally
    diverged.

Both classes erode operator trust equally — operators can't tell from the
spec which way an absent feature failed. The distinction matters for FIX
PATH: §5.10/r46 needs implementation work (a downstream dispatch like r46);
spec-out-of-date needs SYSTEM_SPEC.md rewrites (admin-Jimmy lane per
[[reference_outer_repo_canonical_specs]]).

## Top-3 most critical NOT_IMPLEMENTED findings (rank-ordered)

### 1. C074 §8 — Welcome screen on the SIGN with WiFi SSID + QR code

**Severity: CRITICAL  |  ~180 LOC fix estimate  |  Class: never shipped**

Spec quote: _"If no content has been uploaded, the playback engine
shows a welcome screen with the WiFi SSID and password, and a QR
code encoding the WiFi credentials"_

The WiFi welcome screen the spec promises lives ONLY in
`ui/welcome.html` — the captive-portal **web page** served to a
phone over WiFi. There is no playback-engine slide that paints
SSID/password/QR onto the sign itself. `seed.py` seeds the FREE
YOUR SIGN demo reel (15 frames) when content is empty instead;
`render_welcome_png` exists at seed.py:1144 but renders only the
literal text "Welcome" and is invoked only from tests.

This is the **highest-impact gap**: a first-boot user with a
sign and no phone has no way to discover the WiFi credentials.
The §8 claim that the sign teaches the operator how to connect
is structurally false.

### 2. C036 §5.10 — Text-over-video shader compositing (the gap qarl hit tonight)

**Severity: CRITICAL  |  Class: PARTIAL — dmabuf NV12 sample works, text overlay missing**

Already in flight as r46 per the dispatch. C036 documents the
PARTIAL verdict: the dmabuf-imported H.264 frame IS sampled via
`samplerExternalOES` in a single fragment shader pass
(`FS_NV12_DMABUF_TO_RGB` at hdmi_logic.rs:3223), but `paint_and_
present_one_video_slide_frame` (hdmi.rs:3539-3641) has NO text
layer compositing pass. The text bg-renders to black silently.

### 3. C034 §5.8 — Synchronous TextSlide re-render on display-dim change

**Severity: CRITICAL  |  ~80 LOC fix estimate  |  Class: deliberately removed**

Spec quote: _"On display_rotation/width/height change, every
saved TextSlide is synchronously re-rendered before PUT returns"_

The synchronous text-slide rerender was removed in DELETE-PIL
phase 3 (2026-05-13). `api_settings.py:57-62` documents the
removal explicitly; line 353 is literally `_ = dims_changed
# width/height deltas: no renderer action needed`. Operators
who change display dimensions see TextSlides at the OLD
dimensions until they manually re-save each one. The spec
promises an automatic re-render that no longer exists.

(The other 6 CRITICAL findings: C007 output_mode renderer
selection [4 modes collapsed to HDMI-only], C014 USB-WiFi-dongle
udev rule [doesn't exist; only proposed in r31/r34 audits],
C030 Welcome 3-slide seed, C031 Freedom 3-slide seed, C032
Friday-night Freedom schedule rule, C046 CBDT emoji bake pipeline
[replaced by COLRv1 vector raster in Slice 3D]. See full
NOT_IMPLEMENTED section below.)

## Subagent disagreements (adversarial flips)

**Zero (0) flips.** All 9 NOT_IMPLEMENTED findings survived 3-lens
skeptic adversarial review. Each skeptic was prompted to default
to refuted=true and only flip if it could construct a concrete
file:line counter-example through its assigned lens.

The high-confidence verdict on each finding has 3 independent
"could not refute" votes from 3 different refutation angles
(alternate field names, indirect implementations, wire-format
translation). This is the strongest signal the audit could
produce.


## NOT_IMPLEMENTED findings (survived adversarial review)

### C007 — §3.4 line 156 (severity: **CRITICAL**, ~5 LOC)

**Spec claim:** output_mode setting selects active renderer among hdmi/hub75/ws281x/composite.

**Quote:** _"`output_mode` picks the active renderer (`hdmi` | `hub75` | `ws281x` | `composite`)"_

**Code cite:** `backend/openmarquee/settings.py:39 (`OutputMode = Literal["hdmi"]`)`

**Evidence:** OutputMode is collapsed to `Literal['hdmi']` only. The DELETE-PIL purge (commented at settings.py:256-269 and visible in _coerce_legacy_output_mode) coerces legacy values {'hub75','ws281x','composite'} to 'hdmi' silently at load. Spec claim of 4 modes is false; only HDMI is selectable on HEAD. This is operator-visible doc drift: the spec promises selectable renderers that no longer exist.

**Adversarial review:** 3 of 3 skeptics could not refute. NOT_IMPLEMENTED stands.

---

### C014 — §4.1.1 line 191 (severity: **CRITICAL**, ~15 LOC)

**Spec claim:** A udev rule (99-openmarquee-usb-wlan.rules) renames USB WiFi dongle from wlan1 to wlan-dongle.

**Quote:** _"A udev rule (`system/99-openmarquee-usb-wlan.rules`, installed by `scripts/install.sh` §5b ...) renames the dongle from kernel-default `wlan1` to the predictable `wlan-dongle` name"_

**Code cite:** `NONE`

**Evidence:** No file matching 99-openmarquee-usb-wlan.rules exists in code2/system/. grep across the repo finds the rule referenced only in qa/recommended-outer-repo-edits-2026-05-31-dongle.md and qa/r31-dongle-topology-recommendation-2026-05-31.md as a proposed recommendation; not committed.

**Adversarial review:** 3 of 3 skeptics could not refute. NOT_IMPLEMENTED stands.

---

### C030 — §5.7 line 294 (severity: **CRITICAL**, ~5 LOC)

**Spec claim:** Default Welcome playlist contains three intro slides (Welcome/to/openMarquee) with distinct fonts/backgrounds/transitions, identified by stable DEFAULT_PLAYLIST_ID UUID.

**Quote:** _"the device's default — holds three intro text slides — `Welcome` → `to` → `openMarquee`"_

**Code cite:** `backend/openmarquee/seed.py:219-240 explicitly states Welcome+Freedom seed REPLACED by FREE YOUR SIGN demo reel; no Welcome playlist with three Welcome/to/openMarquee slides exists`

**Evidence:** seed.py:219-222 comment: 'Replaces the previous Welcome (3-slide) + Freedom (3-slide) seed per qarl's 2026-05-04 ask after the design handoff.' Line 236-238: 'The previous Welcome + Freedom 2-playlist split (and its Friday-night Freedom schedule rule) collapses into the demo reel above.' The default playlist is now seeded with the 19-slide FREE YOUR SIGN demo reel (_DEMO_REEL starting at line 458), not the Welcome/to/openMarquee trio. DEFAULT_PLAYLIST_ID still exists at playlist.py:76 but the playlist content is the demo reel.

**Adversarial review:** 3 of 3 skeptics could not refute. NOT_IMPLEMENTED stands.

---

### C031 — §5.7 line 295 (severity: **CRITICAL**, ~5 LOC)

**Spec claim:** A seeded playlist named Freedom holds three protest-poster-style slides (FREE/YOUR/SIGN).

**Quote:** _"A second seeded playlist named **Freedom** holds three protest-poster-style slides — `FREE` → `YOUR` → `SIGN`"_

**Code cite:** `backend/openmarquee/seed.py:236-240 explicit removal of Freedom playlist; the FREE/YOUR/SIGN beats now live as slides 1-3 of the unified demo reel, not as a separate Freedom playlist`

**Evidence:** seed.py:236-240 comment: 'The previous Welcome + Freedom 2-playlist split... collapses into the demo reel above. The "FREE / YOUR / SIGN" beats live in the reel frames 02-04.' The seed only creates the single default playlist with the demo reel slides; no second Freedom playlist is constructed. No PlaylistStorage.create() call for 'Freedom' exists anywhere in seed.py.

**Adversarial review:** 3 of 3 skeptics could not refute. NOT_IMPLEMENTED stands.

---

### C032 — §5.7 line 296 (severity: **CRITICAL**, ~5 LOC)

**Spec claim:** A schedule rule plays Freedom Friday 20:00-20:10 with catch-all default falling back to Welcome.

**Quote:** _"**Friday 20:00–20:10 plays Freedom**; the catch-all default falls back to Welcome at all other times"_

**Code cite:** `backend/openmarquee/seed.py:236-238 — Friday-night Freedom rule explicitly removed; no schedule rule construction exists in seed.py`

**Evidence:** seed.py:236-238: 'The previous Welcome + Freedom 2-playlist split (and its Friday-night Freedom schedule rule) collapses into the demo reel above.' Grepping for 'Friday', '20:00', or 'fri' across backend/openmarquee/ returns only comments documenting the REMOVAL (lines 157, 236-238). No ScheduleRule with days=['fri'] start_time='20:00' end_time='20:10' is constructed. The schedule_storage parameter is accepted (line 144) but never written to.

**Adversarial review:** 3 of 3 skeptics could not refute. NOT_IMPLEMENTED stands.

---

### C034 — §5.8 line 312 (severity: **CRITICAL**, ~80 LOC)

**Spec claim:** On display_rotation/width/height change, every saved TextSlide is synchronously re-rendered before PUT returns.

**Quote:** _"every saved TextSlide is re-rendered at the new effective dims (rotation-applied) ... Re-render runs synchronously before PUT returns"_

**Code cite:** `backend/openmarquee/api_settings.py:57-62, 346-353 (text_rerender deleted, dims_changed is now a no-op: `_ = dims_changed`)`

**Evidence:** The synchronous text-slide rerender was deliberately removed in DELETE-PIL phase 3 (2026-05-13). api_settings.py:57-62 documents: 'text_rerender removed... Display-dim changes now leave the stored PNGs intact'. PUT handler line 353 is literally `_ = dims_changed  # width/height deltas: no renderer action needed`. Only rotation triggers a renderer reopen (not a text rerender). The spec's claim that 'every saved TextSlide is re-rendered ... synchronously before PUT returns' is not implemented.

**Adversarial review:** 3 of 3 skeptics could not refute. NOT_IMPLEMENTED stands.

---

### C074 — §8 line 565 (severity: **CRITICAL**, ~180 LOC)

**Spec claim:** If no content uploaded, playback engine shows welcome screen with WiFi SSID/password and QR code.

**Quote:** _"the playback engine then shows a welcome screen with the WiFi SSID and password, and a QR code encoding the WiFi credentials"_

**Code cite:** `NONE — the only welcome screen with SSID/password/QR is the captive-portal web page ui/welcome.html templated by system/openmarquee-firstboot.sh:283-336; the playback engine path (backend/openmarquee/seed.py) seeds the FREE YOUR SIGN demo reel (15 frames) instead, and render_welcome_png at seed.py:1144 just renders the static literal text 'Welcome' (no SSID/password/QR) and is only called by tests.`

**Evidence:** Spec §8 line 565 explicitly says the welcome screen is 'a playback-engine slide, not a boot-time splash'. seed.py's `seed_if_needed` (called once at startup when content is empty) calls `_seed_demo_reel_slides` — 15 FREE YOUR SIGN frames — and never injects any slide containing the WiFi SSID/password or QR code. `render_welcome_png` exists at seed.py:1144 but only renders the literal text 'Welcome' with no credentials, and grep shows it's only invoked from tests (no production caller). playback.py:484 just sleeps and polls when no slides exist (no welcome fallback). The actual SSID/password/QR welcome surface is the web page `ui/welcome.html` templated by openmarquee-firstboot.sh — served via the captive portal to a phone, not painted by the playback engine onto the sign.

**Adversarial review:** 3 of 3 skeptics could not refute. NOT_IMPLEMENTED stands.

---

### C046 — §5.10a line 352 (severity: **MEDIUM**, ~0 LOC)

**Spec claim:** Color emoji codepoints extracted from Noto CBDT at PPEM=128, resampled to 96x96 RGBA, packed into PNG atlas pages.

**Quote:** _"codepoints in the emoji `unicode-range` ... are extracted from `noto-color-emoji.ttf`'s CBDT bitmap table at PPEM=128, resampled to 96×96 RGBA cells"_

**Code cite:** `renderer/src/sdf_atlas_emoji.rs:1-28 (header explicitly states Slice 3D retired the CBDT bake); renderer/src/glyph_cache_colr.rs:38-43 (COLR_CELL_PX=96, runtime COLRv1 vector rasterization via skrifa+tiny-skia)`

**Evidence:** The build-time CBDT bitmap atlas at PPEM=128 -> 96x96 PNG atlas pages described in the spec was REMOVED in Slice 3D. Emoji codepoints now route to a runtime COLRv1 vector cache: skrifa parses COLRv1 paint trees and tiny-skia rasterizes them at PPEM=96 (COLR_CELL_PX=96), not from CBDT bitmaps. Mechanism is entirely different from the spec claim.

**Adversarial review:** 3 of 3 skeptics could not refute. NOT_IMPLEMENTED stands.

---

### C010 — §3.4 line 164 (severity: **LOW**, ~3 LOC)

**Spec claim:** WiFi SSID suffix is derived from the board's MAC address to make each unit's network unique.

**Quote:** _"The SSID suffix is derived from the board's MAC address to make each unit's WiFi network unique"_

**Code cite:** `system/openmarquee-firstboot.sh:156-162, 200-205`

**Evidence:** generate_device_id() pulls 3 random alphanumeric chars from /dev/urandom, NOT MAC-derived. Source comment confirms: 'SSID is now the device_id verbatim (qarl 2026-05-12). Replaces the MAC-derived openMarquee-<suffix> form'. The MAC-derived suffix was deliberately removed and replaced with a random 3-char generator; spec is stale.

**Adversarial review:** 3 of 3 skeptics could not refute. NOT_IMPLEMENTED stands.

---

## PARTIAL/CRITICAL findings

### C036 — §5.10 line 322 (severity: **CRITICAL**, ~300 LOC)

**Spec claim:** HDMI shader compositor samples decoded H.264 frame as dmabuf-imported GLES2 texture and overlays text in single fragment-shader pass.

**Quote:** _"The Rust sidecar's shader compositor samples the decoded H.264 frame (as a dmabuf-imported GLES2 texture) and the text layer in a single fragment-shader pass per frame"_

**Code cite:** `renderer/src/hdmi_logic.rs:3223 (FS_NV12_DMABUF_TO_RGB samples external-OES dmabuf and writes RGB only; no text uniform/sampler); renderer/src/hdmi.rs:3539-3641 (paint_and_present_one_video_slide_frame has no text layer compositing)`

**Gap:** The dmabuf-imported decoded H.264 frame is sampled via GL_OES_EGL_image_external + GL_TEXTURE_EXTERNAL_OES in a single fragment shader (FS_NV12_DMABUF_TO_RGB) — that part of the claim is correct. However, the shader has only `u_tex_external`; text is not sampled or overlaid in the same pass, and paint_and_present_one_video_slide_frame does no text-layer composite. text_layers is only consumed by TextSlide/ImageSlide render paths, not by the VideoSlide bake. The 'samples ... the text layer in a single fragment-shader pass per frame' clause is not implemented.

---

## PARTIAL/MEDIUM findings

### C013 — §4.1 line 181 (~40 LOC)

**Spec claim:** WiFi watchdog cron runs twice a minute and escalates from NetworkManager restart to system reboot after 3 NM restarts within 600s.

**Code cite:** `scripts/wifi-watchdog.sh:1-53`

**Gap:** wifi-watchdog.sh exists and restarts NetworkManager after 3 consecutive ping failures, but: (1) header comment says 'fired every minute' not twice/minute; (2) no /etc/cron.d/openmarquee-wifi-watchdog drop-in is committed; (3) NO reboot-escalation logic — the script merely restarts NM and resets the counter; the 3-NM-restarts-in-600s -> reboot escalation does not exist.

---

### C041 — §5.10a line 339 (~0 LOC)

**Spec claim:** Each TextLayer's box is normalized {x,y,w,h} floats in [0,1] with validator constraints (w/h in [0.1,0.9], stays inside slide).

**Code cite:** `backend/openmarquee/content/__init__.py:131-134 (TextBox fields x,y in [-2.0,3.0], w,h in [0.01,5.0]); :125-128 (note explicitly removes _stays_inside_slide validator)`

**Gap:** TextBox has x/y/w/h floats but with widened bounds: x,y in [-2.0,3.0] and w,h in [0.01,5.0], NOT the spec's [0.1,0.9]. The 2026-05-19 schema-widen commit deliberately removed the stays-inside-slide model_validator to allow off-canvas placement for animated entrance/exit. Spec claim is doc-drift; field shape (x,y,w,h floats) is correct but the validator constraints do not match.

---

### C053 — §5.10b line 374 (~40 LOC)

**Spec claim:** WebSlide producer drives chromium-headless-shell to capture PNG at display dims and atomically replaces asset.png; self-gates under memory pressure.

**Code cite:** `backend/openmarquee/web_screenshot.py:109-200 (producer), web_render.py:111 (chromium-headless-shell preferred), content/storage.py:316 (atomic_write_bytes). Gap: no memory-pressure self-gating found (no psutil/MemAvailable/virtual_memory check)`

**Gap:** fetch_web_screenshot drives chromium via render_web_png; web_render.py line 111 lists chromium-headless-shell as the preferred binary. Save goes through storage.save_web which uses atomic_write_bytes at storage.py:316. However, grep across backend found no MemAvailable / psutil / virtual_memory / pressure-gating logic — only a single-flight asyncio.Lock plus nice/ionice priority. The 'self-gates u

---

### C059 — §5.11 line 428 (~40 LOC)

**Spec claim:** On phone disconnect, device waits ~10s for connected/failed before reverting to playlist.

**Code cite:** `backend/openmarquee/live.py:142 (_PHANTOM_TIMEOUT_SECONDS = 10.0)`

**Gap:** The 10s threshold exists and is documented to share the §5.11 PC-disconnect timeout, but it's wired ONLY as the initial-on_track phantom-session watchdog (line 165 _first_track_event + line 260 wait_for with that timeout). There is no explicit connectionState-monitor that waits ~10s for `connected`/`failed` after a `disconnected` state — alternative #2 in the doc comment (line 135-138) is explicit

---

## PARTIAL/LOW findings (doc drift)

- **C006** §3.4 line 128 — SettingsStorage persists settings.json with atomic writes and a schema-version e (cite: `backend/openmarquee/settings.py:379-476 + _atomic.py:32-50`)
- **C015** §4.1.1 line 193 — burn_sd_card.sh accepts --mgmt-wifi-ssid and --mgmt-wifi-password flags to preco (cite: `scripts/burn_sd_card.sh:101-126`)
- **C020** §5.2 line 251 — Procedural patterns render via Pillow once at slide entry and bake into the bg c (cite: `backend/openmarquee/auto_render.py:194-200 (DELETE-PIL phase 3b removed server-s`)
- **C025** §5.5 line 281 — Schedule page renders rule editor with day-of-week checkboxes, HH:MM windows (24 (cite: `ui/src/schedule.js:12-20 (DAYS), :289 (enabled checkbox), :310-318 (HH:MM + play`)
- **C044** §5.10a line 351 — Rust renderer paints text via per-codepoint quads sampling MSDF atlases baked at (cite: `renderer/build.rs:39 (CELL_PX=48), :60-65 (bake_codepoints hardcodes 0x20-0x7E +`)
- **C055** §5.11 line 389 — WebRTC peer connection between phone and device uses aiortc as subscriber with V (cite: `backend/openmarquee/live.py:37 (aiortc import), :187 (RTCPeerConnection() with n`)
- **C058** §5.11 line 425 — paint_external_frame IPC op pushes decoded RGB888 frames into sidecar (landed vi (cite: `renderer/src/ipc_main.rs:974 (run_external_frame_pump) + hdmi.rs:3323 (paint_and`)

## Full claim-by-claim table

| ID | §ref | Verdict | Sev | Claim | Code cite | Fix LOC |
| --- | --- | --- | --- | --- | --- | --- |
| C001 | §3.3 line 92 | **VERIFIED** |  | Each content item lives in a UUID subdir containing item.json (schema-versioned  | backend/openmarquee/content/storage.py:298-316 |  |
| C002 | §3.3.1 line 100 | **VERIFIED** |  | Every item.json on disk is a schema-versioned envelope wrapping an item discrimi | backend/openmarquee/content/storage.py:302-309 |  |
| C003 | §3.3.2 line 115 | **VERIFIED** | LOW | On load the device refuses envelopes whose schema_version doesn't match current  | backend/openmarquee/content/storage.py:449-454 | 1 |
| C004 | §3.3.2 line 122 | **VERIFIED** |  | Writes are atomic via sibling .tmp file plus rename for both envelope and asset. | backend/openmarquee/_atomic.py:32-70 + backend/openmarquee/content/storage.py:31 |  |
| C005 | §3.3.2 line 124 | **VERIFIED** |  | list_all() skips subdirs that aren't UUID-named or lack item.json. | backend/openmarquee/content/storage.py:526-546 |  |
| C006 | §3.4 line 128 | **PARTIAL** | LOW | SettingsStorage persists settings.json with atomic writes and a schema-version e | backend/openmarquee/settings.py:379-476 + _atomic.py:32-50 | 5 |
| C007 | §3.4 line 156 | **NOT_IMPLEMENTED** | CRITICAL | output_mode setting selects active renderer among hdmi/hub75/ws281x/composite. | backend/openmarquee/settings.py:39 (`OutputMode = Literal["hdmi"]`) | 5 |
| C008 | §3.4 line 158 | **VERIFIED** |  | display_rotation is honored by renderer at playback (rotates output); the UI swa | renderer/src/ipc_main.rs:1122-1136 + ui/src/api.js:618-628 + ui/src/rotation-rer |  |
| C009 | §3.4 line 161 | **VERIFIED** |  | A cross-field validator prevents disabling both wifi_ap_enabled and wifi_station | backend/openmarquee/settings.py:323-335 |  |
| C010 | §3.4 line 164 | **NOT_IMPLEMENTED** | LOW | WiFi SSID suffix is derived from the board's MAC address to make each unit's net | system/openmarquee-firstboot.sh:156-162, 200-205 | 3 |
| C011 | §4.1 line 174 | **VERIFIED** |  | openmarquee-firstboot.service rotates SSID to per-device MySignXXX and passphras | system/openmarquee-firstboot.sh:172-179, 205-206 |  |
| C012 | §4.1 line 177 | **VERIFIED** |  | ap0 is created at boot by openmarquee-ap0.service via iw dev wlan0 interface add | system/openmarquee-ap0.service:1-29, system/openmarquee-ap0-setup.sh:25-27 |  |
| C013 | §4.1 line 181 | **PARTIAL** | MEDIUM | WiFi watchdog cron runs twice a minute and escalates from NetworkManager restart | scripts/wifi-watchdog.sh:1-53 | 40 |
| C014 | §4.1.1 line 191 | **NOT_IMPLEMENTED** | CRITICAL | A udev rule (99-openmarquee-usb-wlan.rules) renames USB WiFi dongle from wlan1 t | NONE | 15 |
| C015 | §4.1.1 line 193 | **PARTIAL** | LOW | burn_sd_card.sh accepts --mgmt-wifi-ssid and --mgmt-wifi-password flags to preco | scripts/burn_sd_card.sh:101-126 | 10 |
| C016 | §4.2 line 207 | **VERIFIED** |  | dnsmasq binds only to ap0 (not wlan0) so home-WiFi DNS isn't intercepted. | system/dnsmasq.conf:11-16 |  |
| C017 | §4.3 line 215 | **VERIFIED** |  | Device clears tailscale_auth_key from settings.json after successful registratio | system/openmarquee-tailscale.sh:102-112 |  |
| C018 | §4.3 line 217 | **VERIFIED** |  | openmarquee-tailscale.service reads settings.json at boot and runs tailscale up  | system/openmarquee-tailscale.service:1-22 + system/openmarquee-tailscale.sh:16-6 |  |
| C019 | §5.2 line 251 | **VERIFIED** |  | TextSlide background slot supports solid color, ImageSlide, VideoSlide, or one o | backend/openmarquee/content/__init__.py:287-340 |  |
| C020 | §5.2 line 251 | **PARTIAL** | LOW | Procedural patterns render via Pillow once at slide entry and bake into the bg c | backend/openmarquee/auto_render.py:194-200 (DELETE-PIL phase 3b removed server-s | 5 |
| C021 | §5.2 line 251 | **VERIFIED** |  | A lazy in-validator migration converts legacy two-stop gradient envelopes into B | backend/openmarquee/content/__init__.py:420-460 |  |
| C022 | §5.2 line 251 | **VERIFIED** |  | TextSlide model validator enforces mutual exclusivity of the four background-sou | backend/openmarquee/content/__init__.py:462-485 |  |
| C023 | §5.3 line 262 | **VERIFIED** |  | Video content is processed client-side by ffmpeg.wasm and stored as a single H.2 | ui/src/video-upload.js:82-89 + ui/src/ffmpeg-pipelines.js:145-216 |  |
| C024 | §5.3 line 264 | **VERIFIED** |  | Rust renderer sidecar decodes MP4 via V4L2 with Pi hardware H.264 decoder and ha | renderer/src/v4l2.rs:1-82 + renderer/src/hdmi.rs:9704-9796 (dmabuf EGLImage impo |  |
| C025 | §5.5 line 281 | **PARTIAL** | LOW | Schedule page renders rule editor with day-of-week checkboxes, HH:MM windows (24 | ui/src/schedule.js:12-20 (DAYS), :289 (enabled checkbox), :310-318 (HH:MM + play | 30 |
| C026 | §5.5 line 283 | **VERIFIED** |  | Schedules are stored as JSON and evaluated by the playback engine. | backend/openmarquee/schedule.py:163 (ScheduleStorage with JSON persistence) + :1 |  |
| C027 | §5.6 line 287 | **VERIFIED** |  | POSTing /api/backgrounds/generate invokes provider-pluggable image-gen backend d | backend/openmarquee/api_backgrounds.py:78-95 (POST /generate); backgrounds.py:60 |  |
| C028 | §5.7 line 291 | **VERIFIED** |  | openmarquee-seeded.json marker is stamped after first-boot seed runs. | backend/openmarquee/dependencies.py:749 (returns 'openmarquee-seeded.json' path) |  |
| C029 | §5.7 line 293 | **VERIFIED** |  | Curated gradient/texture backgrounds are bundled at backend/openmarquee/seed_ass | backend/openmarquee/seed_assets/backgrounds/ directory; seed.py:113 (_default_bu |  |
| C030 | §5.7 line 294 | **NOT_IMPLEMENTED** | CRITICAL | Default Welcome playlist contains three intro slides (Welcome/to/openMarquee) wi | backend/openmarquee/seed.py:219-240 explicitly states Welcome+Freedom seed REPLA | 5 |
| C031 | §5.7 line 295 | **NOT_IMPLEMENTED** | CRITICAL | A seeded playlist named Freedom holds three protest-poster-style slides (FREE/YO | backend/openmarquee/seed.py:236-240 explicit removal of Freedom playlist; the FR | 5 |
| C032 | §5.7 line 296 | **NOT_IMPLEMENTED** | CRITICAL | A schedule rule plays Freedom Friday 20:00-20:10 with catch-all default falling  | backend/openmarquee/seed.py:236-238 — Friday-night Freedom rule explicitly remov | 5 |
| C033 | §5.8 line 310 | **VERIFIED** |  | Server-side Pydantic validators enforce settings ranges/patterns with failures s | backend/openmarquee/settings.py:83-376 (Field constraints + field_validator/mode |  |
| C034 | §5.8 line 312 | **NOT_IMPLEMENTED** | CRITICAL | On display_rotation/width/height change, every saved TextSlide is synchronously  | backend/openmarquee/api_settings.py:57-62, 346-353 (text_rerender deleted, dims_ | 80 |
| C035 | §5.8 line 312 | **VERIFIED** |  | UI re-mounts panels on openmarquee:settings-updated event using each slide's upd | ui/src/main.js:892-957 (settings-updated handler + mountDimensionedPanels); ui/s |  |
| C036 | §5.10 line 322 | **PARTIAL** | CRITICAL | HDMI shader compositor samples decoded H.264 frame as dmabuf-imported GLES2 text | renderer/src/hdmi_logic.rs:3223 (FS_NV12_DMABUF_TO_RGB samples external-OES dmab | 300 |
| C037 | §5.10 line 327 | **VERIFIED** |  | When source video is shorter than slide duration, playback loops the video; long | renderer/src/hdmi.rs:6451-6453 (if *next_sample_idx >= samples.len() { *next_sam |  |
| C038 | §5.10 line 329 | **VERIFIED** |  | Aspect-ratio mismatch between source video and panel handled cover-fit (scale-up | renderer/src/hdmi_logic.rs:2928-2955 (FS_NV12_COVER_TO_RGB), 2971-3010 (nv12_cov |  |
| C039 | §5.10 line 331 | **VERIFIED** |  | Audio is stripped from source videos at upload regardless of origin. | ui/src/ffmpeg-pipelines.js:214 (`"-an", // drop audio — signs don't speak`) |  |
| C040 | §5.10a line 337 | **VERIFIED** |  | TextSlide carries a non-empty text_layers list (min_length=1); fresh slides ship | backend/openmarquee/content/__init__.py:370-373 |  |
| C041 | §5.10a line 339 | **PARTIAL** | MEDIUM | Each TextLayer's box is normalized {x,y,w,h} floats in [0,1] with validator cons | backend/openmarquee/content/__init__.py:131-134 (TextBox fields x,y in [-2.0,3.0 |  |
| C042 | §5.10a line 347 | **VERIFIED** |  | Font size is anchored to box width: font_size_pct is percent of box.w*canvas.wid | ui/src/rasterize.js:158-171 (fontSizePx = boxW * pct / 100); ui/src/rasterize.js |  |
| C043 | §5.10a line 347 | **VERIFIED** |  | When rendered text overflows box on either axis, that axis is Lanczos-resized to | backend/openmarquee/seed.py:1276-1331 (fits-inside fast path then squish path wi |  |
| C044 | §5.10a line 351 | **PARTIAL** | LOW | Rust renderer paints text via per-codepoint quads sampling MSDF atlases baked at | renderer/build.rs:39 (CELL_PX=48), :60-65 (bake_codepoints hardcodes 0x20-0x7E + |  |
| C045 | §5.10a line 351 | **VERIFIED** |  | Two AA variants ship: FWIDTH using fwidth() and FIXED uniform AA half-width; Pi  | renderer/src/hdmi_logic.rs:1283 (FS_MSDF_FWIDTH using fwidth(d)), :1302 (FS_MSDF |  |
| C046 | §5.10a line 352 | **NOT_IMPLEMENTED** | MEDIUM | Color emoji codepoints extracted from Noto CBDT at PPEM=128, resampled to 96x96  | renderer/src/sdf_atlas_emoji.rs:1-28 (header explicitly states Slice 3D retired  |  |
| C047 | §5.10a line 353 | **VERIFIED** |  | Tofu fallback for unknown codepoints draws deterministic gray-with-outline recta | renderer/src/hdmi_logic.rs:1403-1418 (FS_TOFU shader: 50% gray with black outlin |  |
| C048 | §5.10a line 355 | **VERIFIED** |  | Per-codepoint dispatch order: emoji-range -> MSDF -> whitespace skip -> tofu. | renderer/src/hdmi_logic.rs:654-658 (comment documents dispatch order); :747-870  |  |
| C049 | §5.10a line 357 | **VERIFIED** |  | Text-slide canvas surfaces 8 resize handles plus move-by-drag with 5px click-vs- | ui/src/editor.js:97-104 (8 handles nw/n/ne/e/se/s/sw/w), :439 (DRAG_THRESHOLD_PX |  |
| C050 | §5.10a line 359 | **VERIFIED** |  | TextLayer carries name, visible, locked, outline, weight, opacity, anchor, motio | backend/openmarquee/content/__init__.py:137-247 (TextLayer) |  |
| C051 | §5.10a line 359 | **VERIFIED** |  | Legacy 'scroll' motion value migrates lazily to 'ticker' via an in-validator ren | backend/openmarquee/content/__init__.py:256-268 (_migrate_legacy_motion validato |  |
| C052 | §5.10b line 372 | **VERIFIED** |  | WebSlide carries url (http(s) allowlist), refresh_interval_s in [10,86400] defau | backend/openmarquee/content/__init__.py:604-659 (WebSlide) |  |
| C053 | §5.10b line 374 | **PARTIAL** | MEDIUM | WebSlide producer drives chromium-headless-shell to capture PNG at display dims  | backend/openmarquee/web_screenshot.py:109-200 (producer), web_render.py:111 (chr | 40 |
| C054 | §5.11 line 385 | **VERIFIED** |  | StreamSlide content variant (type: stream) carries stream_url, duration_ms, on_u | backend/openmarquee/content/__init__.py:547-601 (StreamSlide) |  |
| C055 | §5.11 line 389 | **PARTIAL** | LOW | WebRTC peer connection between phone and device uses aiortc as subscriber with V | backend/openmarquee/live.py:37 (aiortc import), :187 (RTCPeerConnection() with n | 20 |
| C056 | §5.11 line 393 | **VERIFIED** |  | Phone POSTs SDP offer to /api/live/start; backend creates PC, generates answer,  | backend/openmarquee/api_live.py:149-199 (POST /start) |  |
| C057 | §5.11 line 417 | **VERIFIED** |  | Device reports its hardware tier via /api/live/status so phone can clamp constra | backend/openmarquee/api_live.py:243-251 |  |
| C058 | §5.11 line 425 | **PARTIAL** | LOW | paint_external_frame IPC op pushes decoded RGB888 frames into sidecar (landed vi | renderer/src/ipc_main.rs:974 (run_external_frame_pump) + hdmi.rs:3323 (paint_and | 2 |
| C059 | §5.11 line 428 | **PARTIAL** | MEDIUM | On phone disconnect, device waits ~10s for connected/failed before reverting to  | backend/openmarquee/live.py:142 (_PHANTOM_TIMEOUT_SECONDS = 10.0) | 40 |
| C060 | §5.12 line 444 | **VERIFIED** |  | Flock peer-introduction: when device B is added, A pings B to reciprocally add A | backend/openmarquee/api_flock.py:159 (background.add_task(sync.gossip_add, peer. |  |
| C061 | §5.12 line 446 | **VERIFIED** |  | Flock API surface exposes /api/flock GET/POST, /manifest, /{peer_id} PATCH/DELET | backend/openmarquee/api_flock.py:106,111,130,163,181,272,410,440,454 |  |
| C062 | §6 line 459 | **VERIFIED** |  | Content REST API exposes GET /api/content, GET/{id}, GET/{id}/asset, GET/{id}/vi | backend/openmarquee/api.py:45 prefix='/api/content' + lines 316/555/591/684/778/ |  |
| C063 | §6 line 472 | **VERIFIED** |  | Playlist/schedule REST API exposes GET/PUT /api/playlist, GET /api/playlists, PU | backend/openmarquee/api_playlist.py:70,86,101,112,120,132 + api_schedule.py:19,2 |  |
| C064 | §6 line 487 | **VERIFIED** |  | Playback API exposes POST /api/playback/{start,stop} and GET /api/playback/state | backend/openmarquee/api_playback.py:43 prefix='/api/playback', lines 84 (GET /st |  |
| C065 | §7.1 line 508 | **VERIFIED** |  | Python playback engine drives Rust renderer sidecar subprocess (openmarquee-rend | renderer/src/main.rs:1172-1173 (--ipc-sidecar flag dispatches to ipc_main::run_i |  |
| C066 | §7.1 line 508 | **VERIFIED** |  | Sidecar transitions (fade/wipe/slide/iris/scroll/flip/marquee/dissolve/pixelate/ | renderer/src/hdmi_logic.rs:2713-2736 (fs_for_transition_kind matches all 15 name |  |
| C067 | §7.1 line 510 | **VERIFIED** |  | Default video decode path is MMAP NV12 with BT.709 GLES2 shader conversion; opt- | renderer/src/ipc_main.rs:694-706 (OPENMARQUEE_RENDERER_DMABUF env var gate; DmaB |  |
| C068 | §7.1 line 510 | **VERIFIED** |  | External-source video uses ffmpeg h264_v4l2m2m HW-decode to NV12; renderer inges | backend/openmarquee/stream_consumer.py:317 ('h264_v4l2m2m' codec) + 182 (pixel_f |  |
| C069 | §7.6 line 541 | **VERIFIED** |  | Renderer IPC ops: open, begin_slide, advance, begin_transition, capture, reconfi | renderer/src/playback.rs:223-262 (IpcRequest enum: Open/BeginSlide/Advance/Begin |  |
| C070 | §7.6 line 545 | **VERIFIED** |  | capture(path) writes current framebuffer to PNG on disk; used by /api/playback/c | renderer/src/ipc_main.rs:1357-1448 (capture_current_scene_to_png re-paints + cap |  |
| C071 | §7.6 line 553 | **VERIFIED** |  | AutoFallbackRenderer replays begin_slide against freshly-spawned sidecar on cras | backend/openmarquee/dependencies.py:29-359 (AutoFallbackRenderer with _swap_to_m |  |
| C072 | §7.6 line 555 | **VERIFIED** |  | Sidecar reads brightness from settings.json at boot and on settings change. | renderer/src/content.rs:565-619 (SettingsWatcher struct + check() with bootstrap |  |
| C073 | §8 line 565 | **VERIFIED** |  | Boot splash renders from kernel bring-up through to playback engine's first fram | code2/images/openmarquee/stage-openmarquee/01-plymouth-theme/files/openmarquee/o |  |
| C074 | §8 line 565 | **NOT_IMPLEMENTED** | CRITICAL | If no content uploaded, playback engine shows welcome screen with WiFi SSID/pass | NONE — the only welcome screen with SSID/password/QR is the captive-portal web p | 180 |

## §F. Outer-repo recommendations for admin-Jimmy (SYSTEM_SPEC.md rewrites)

Per the dispatch's §F: list SYSTEM_SPEC.md text that should be REWRITTEN
to match reality (the "spec catches up to code" version of r39). I do
NOT edit SYSTEM_SPEC.md myself — that's admin-Jimmy's lane per
[[reference_outer_repo_canonical_specs]].

### F.1 §3.4 line 156 — `output_mode` setting reality (C007, CRITICAL)

Spec promises 4-way HDMI/HUB75/WS2812B/composite renderer selection.
Code is `OutputMode = Literal["hdmi"]` — only HDMI is selectable.
HUB75/WS2812B/composite were retired in v0.6 pending Rust port.

Recommended SYSTEM_SPEC.md change: rewrite §3.4's `output_mode`
entry to say "currently `hdmi` only; legacy values are coerced at
load. HUB75/WS2812B/composite re-introduction is queued behind
their respective Rust ports — see §7.2-§7.4." (Already aspirational
in §3.2's renderer table; align §3.4's settings description.)

### F.2 §3.4 line 164 — SSID derivation reality (C010, LOW)

Spec says SSID suffix is MAC-derived. Code generates 3 random
alphanumeric chars (`generate_device_id` at openmarquee-firstboot.sh:
156-162). qarl explicitly changed this 2026-05-12.

Recommended SYSTEM_SPEC.md change: replace "MAC-derived" with
"a 3-char alphanumeric device_id generated from /dev/urandom at
first boot, stored in /var/openmarquee/device_id."

### F.3 §4.1.1 line 191 — USB-WiFi-dongle udev rule (C014, CRITICAL)

Spec describes a `99-openmarquee-usb-wlan.rules` udev rule renaming
USB dongle to `wlan-dongle`. The rule was RECOMMENDED in r31 +
r34 audit docs but NEVER landed in `system/`. Spec is describing
proposed-not-shipped state.

Recommended SYSTEM_SPEC.md change: either tag the udev-rule
description as "proposed (see r31 dongle topology audit; pending
implementation)" OR (preferable) land the rule first as a
downstream dispatch then leave §4.1.1 unchanged.

### F.4 §5.7 lines 294-296 — Seed content reality (C030, C031, C032, all CRITICAL)

Spec describes Welcome (3 slides) + Freedom (3 slides) + Friday-
night Freedom schedule rule. All THREE were collapsed into a single
FREE YOUR SIGN demo reel (15 frames) on 2026-05-04 per qarl's design
handoff. seed.py:219-238 documents the removal in detail.

Recommended SYSTEM_SPEC.md change: replace §5.7 lines 294-296 with
a description of the FREE YOUR SIGN demo reel (19 slides per the
codebase) — slide enumeration, the per-slide font choices, the
single default playlist, no Freedom playlist, no Friday-night
schedule rule. This is the biggest spec-out-of-date cluster in
the audit.

### F.5 §5.8 line 312 — TextSlide rerender on display-dim change (C034, CRITICAL)

Spec promises synchronous text-rerender before PUT returns. Removed
in DELETE-PIL phase 3 (2026-05-13). api_settings.py:57-62 documents
the removal explicitly.

Recommended SYSTEM_SPEC.md change: rewrite §5.8 to describe the
current shape: rotation changes trigger a renderer reopen but
NOT a text rerender; width/height changes are no-ops in renderer
land (TextSlide PNGs stay at their saved dimensions until
manually re-saved). This is a real operator-affecting gap if
display-dim change without re-save is a flow we expect.

ALTERNATIVE: ship the synchronous rerender as a downstream
dispatch (~80 LOC fix per the audit estimate), leave the spec
text unchanged. qarl's call — this is the only "spec is right;
code should catch up" candidate in the audit.

### F.6 §5.10a line 352 — CBDT emoji bake (C046, MEDIUM)

Spec describes CBDT bitmap atlas at PPEM=128 → 96x96 PNG pages.
This pipeline was REMOVED in Slice 3D (2026-05-19). Emoji now
routes to a runtime COLRv1 vector cache via skrifa+tiny-skia at
COLR_CELL_PX=96.

Recommended SYSTEM_SPEC.md change: rewrite §5.10a's emoji
paragraph to describe the COLRv1 vector pipeline (skrifa parser
+ tiny-skia rasterization + LRU runtime cache); the CBDT
mechanism no longer exists.

### F.7 §8 line 565 — Welcome screen on the sign (C074, CRITICAL)

Spec promises the playback engine paints a welcome screen with
WiFi SSID/password/QR when content is empty. No such code path
exists; the only SSID/QR surface is the captive-portal web page.

Decision required (qarl): is this a doc-rewrite (spec catches up
to code: the sign-side welcome was descoped) OR an
implementation-debt admission (we need to build it for the
no-phone first-boot UX)?

If doc-rewrite: replace §8 line 565 with a description of the
captive-portal web flow as the canonical first-boot UX.

If implementation-debt: leave the spec text unchanged, queue a
~180 LOC dispatch to build the sign-side welcome slide.

This is the single highest-impact spec/impl decision in the
audit. The audit doc cannot make this call; it requires
operator-side product judgment.

## Aspirational claims parked from scope

Skipped per §A scope rules (future / historical / aspirational /
marketing / operator-action / pure-design):


- **§2.1 lines 17-24**: _"Raspberry Pi Zero 2 W ($15) Quad-core Cortex-A53 @ 1.0 GHz, 512 MB LPDDR2 RAM"_ — Pure hardware fact, not a code-side capability claim
- **§3.2 line 77**: _"HUB75 output \| *Pending Rust port (§7.2). Pre-v0.6 builds used `hzeller/rpi-rgb-led-matrix`*"_ — Explicitly aspirational / pending future Rust port
- **§3.2 line 78**: _"WS2812B output \| *Pending Rust port (§7.3). Pre-v0.6 builds used `rpi_ws281x`*"_ — Explicitly aspirational / pending future Rust port
- **§5.2 line 254**: _"**Auto** (future). Dynamic slides rendered on the device at play time ... Ships as a preview-only pl"_ — Explicitly marked future / preview-only placeholder
- **§5.3 line 269**: _"The earlier dual-pipeline plan ... was simplified out 2026-04-26"_ — Historical narrative — describes what was removed
- **§5.10 line 333**: _"**Pre-bake button (future).** A per-slide opt-in lets the operator flatten the text+video composite "_ — Explicitly future-tagged 'Not in v1'
- **§5.10a line 359**: _"Render-side support for motion / opacity / anchor / blend lands in a later wave; until then the rend"_ — Explicitly aspirational — renderer support not yet shipped
- **§5.11 line 437**: _"**Out of scope for v1.** Recording the stream to disk; multi-publisher / picture-in-picture; cloud r"_ — Explicitly out-of-scope future items
- **§7.2 line 514**: _"HUB75 ... a Rust port of the HUB75 driver — including the bit-banged scan-rate / PWM timing — is fut"_ — Explicitly pending / future work
- **§7.3 line 518**: _"WS2812B is a designed-for output mode ... the Python renderer was removed in v0.6 and the Rust port "_ — Explicitly pending Rust port / future work
- **§7.4 line 522**: _"Composite output ... the v0.6 teardown also took the composite-specific dispatch out, queued for re-"_ — Explicitly pending future Rust port
- **§13 lines 621-628**: _"Native iOS app ... Template library ... Community content sharing ... HDMI input capture ... E-ink/E"_ — Section 13 is explicitly FUTURE CONSIDERATIONS
- **§9 lines 572-583**: _"User plugs the openMarquee board into their sign ... User scans the QR code with their phone camera"_ — Operator-action narrative, not system capability claims
- **§10 lines 588-594**: _"Small LED matrix signs: 128x64, 128x96 ... Any TV or monitor with HDMI input"_ — Display-target list / design intent, not code claims
- **§5.1 lines 230-244**: _"A left sidebar navigates between sections; panels stay mounted across clicks"_ — Pure-design layout statement
- **§1 line 8**: _"openMarquee is a self-contained display controller that replaces proprietary sign hardware and softw"_ — Product marketing summary, not a concrete code claim
- **§11 lines 599-605**: _"Cloud signage platforms ... Professional LED controllers"_ — Competitive positioning / marketing copy
- **§12 lines 610-616**: _"Small business owners with existing signs and terrible vendor software"_ — Target-users narrative, not code claim

## §G. Notes on classification edge cases

### G.1 Bound by "static-only" — the audit cannot certify performance claims

Several spec claims are about runtime numbers (fps targets, boot
time, transition durations). The audit flags these as
NOT_VERIFIABLE_FROM_STATIC; the workflow does not attempt to
verify them by running the system. Those claims need a runtime
soak (which by standing rule
[[feedback_perf_audit_enumerate_allocators]] requires careful
allocator-surface inventory) before they can be classified
VERIFIED or otherwise. The r38d SIGUSR1 dump is the closest
forensic mechanism available.

### G.2 PARTIAL/LOW = doc-drift candidates

The 7 PARTIAL/LOW findings are name/version drift between spec
prose and code identifiers (e.g. schema_version=3 in code vs
"(currently 1)" in spec text; `paint_external_frame` op named
differently from `begin_external_frames` in code). These are
mostly safe to fix in either direction (spec catches up to code
OR code renames to match spec) per local convention; they're
listed as ride-alongs for whoever opens an admin-Jimmy
SYSTEM_SPEC.md sweep.

### G.3 What the audit deliberately did NOT do

  - **Did not fix anything.** Per dispatch §H constraint, this is
    audit-only. r46 + future r48/r49 etc. land the implementation
    work.
  - **Did not audit IMPLEMENTATION_PLAN.md** — different drift
    class per dispatch §F constraint.
  - **Did not run code.** All verification was static — grep + read.
    Performance claims defaulted to NOT_VERIFIABLE_FROM_STATIC.
  - **Did not edit SYSTEM_SPEC.md.** §F is admin-Jimmy's lane;
    this doc lists recommended edits, doesn't apply them.

## §H. Lane

- Doc-only commit (audit doc + workflow script + commit message)
- Subagent review on the OVERALL audit doc before commit (separate
  from per-finding adversarial passes already run in §C)
- Standard /tmp clone + cherry-pick push if NFS-wedges
- /Users/qarl/.claude/projects/-Users-qarl-project-openmarquee-code/
  memory/ updates: none needed — feedback memory class is
  "deploy/build foot-guns", not "spec-vs-code audit findings"

## §I. Push posture

- 1 file committed: `qa/r47-spec-vs-code-audit-2026-06-02.md` (audit doc)
- NOT committed: `.claude/workflows/r47-spec-vs-code-audit.mjs`
  (workflow script — per standing rule "don't commit `.claude/`
  artifacts"; the §Methodology block above is the canonical
  description for reproducibility)
- NOT committed: `/tmp/r47-results.json` (transient intermediate)
- Pre-push hook applies only if renderer/ui/backend changed;
  audit doc lands as a docs-only commit per the lane.

---

End of r47 audit.
