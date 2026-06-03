# r49 — UI vs Pydantic-model audit (find every operator-facing field with no UI surface)

**Author:** jimmy:openmarquee-code2
**Date:** 2026-06-03
**Status:** SHIPPED on code2; cherry-picked to main
**Dispatch:** qarl-direct, after the TextLayer.outline gap was spotted in person
**Predecessors:**
  - r47 SYSTEM_SPEC.md vs code audit (spec → code drift, c57e109)
  - r48 in flight (code1's renderer V4L2 free-list refactor)
  - r46.1 hotfix landed in main (b2b225f)

## Goal

qarl noticed tonight that `TextLayer.outline: bool` is defined in the
Pydantic model AND honored by the Rust renderer (FS_MSDF_OUTLINE shader)
but NEVER EXPOSED in the UI editor. Operators can't reach it.

This is a different bug class from r47 (spec → code drift). This is
**model → UI drift**: the data shape promises a feature; the renderer
implements it; the operator can't use it.

This audit enumerates every operator-facing Pydantic model field and
classifies whether the UI surfaces it via read + write paths, with
adversarial verification of every NOT_EXPOSED/DEFAULT_ONLY finding
through 3 independent skeptic lenses — matching r47's rigor bar.

## Methodology

Workflow orchestration: `code/.claude/workflows/r49-ui-vs-model-audit.mjs`
(local-only per standing rule "don't commit `.claude/` artifacts").

  Phase 1 (Enumerate)    1 agent reads content/__init__.py + settings.py +
                         playlist.py + schedule.py; extracts operator-facing
                         fields with {id, model, field_name, field_type,
                         model_file_cite, purpose_summary}
  Phase 2 (Verify)       N parallel verifiers (8-field batches) grep ui/src/
                         + classify each as VERIFIED / READ_ONLY / NOT_EXPOSED /
                         DEFAULT_ONLY / NOT_APPLICABLE, with read+write cites,
                         severity, and renderer_honors flag (does
                         renderer/src/ read the field?)
  Phase 3 (Adversarial)  For each NOT_EXPOSED + DEFAULT_ONLY finding, 3 skeptics
                         with distinct refutation lenses (alternate field names,
                         indirect surface, non-editor surface) try to refute,
                         default refuted=true. Majority-refutes (2+ of 3) → kill.
  Phase 4 (Synthesize)   Aggregate into structured rows.

Stats:
  - 55 agents spawned (1 enumerator + 12 verifiers + 42 skeptics)
  - 1.88M subagent tokens
  - 12.3 min wall-clock
  - 92 operator-facing fields audited (target was 50-100; spec was rich)
  - 25 system-internal fields explicitly parked (id/timestamps/discriminators)

## Executive summary

  Total fields audited:    92
  VERIFIED (read+write):   74  (80%)
  READ_ONLY:                4
  DEFAULT_ONLY:             4
  NOT_EXPOSED:             10
  Adversarial flips:        0  (all 14 exposure gaps survived 3-lens skeptic refute)

Of the 18 non-VERIFIED findings:
  - **5 have operator-impact severity ≥ MEDIUM** (1 CRITICAL + 3 HIGH + 1 MEDIUM)
  - **13 are intentional-design LOW** — by-design v3+ playlist-owns-transitions
    migration, future-reserved fields, or other documented design choices

The 5 high-impact findings are real gaps an operator would notice.
The 13 LOW findings are listed for completeness but should not be
treated as bugs.

## Top-5 most critical findings (rank-ordered)

### 1. F013 — `TextLayer.outline` (NOT_EXPOSED, **CRITICAL**)

**THIS IS THE FIELD QARL NOTICED.** Renderer honors via FS_MSDF_OUTLINE
shader (hdmi.rs:2421-2554, dispatched when `layer.outline == true`).
Pydantic model defines it at content/__init__.py:188. Zero references
in ui/src/: layerFromWire never extracts it; performSave never serializes
it; layer-defaults.js omits it; the editor has no toggle or color
picker for it.

Operator impact: every TextLayer ships with `outline=false` (Pydantic
default); operators cannot turn on the outline effect through any
existing UI surface. The renderer-side code is dead in production
because nothing ever sets the flag.

Fix path: add an outline toggle (and likely outline-color + outline-width
controls) to the TextLayer accordion editor card. Estimate ~60 LOC in
editor.js + layer-defaults.js + supporting CSS.

### 2. F008 — `TextLayer.font_size_px` (READ_ONLY, HIGH)

Renderer honors directly (content.rs:165). UI READS `font_size_px`
from wire format ONLY to derive a `fontSizePct` value when
`font_size_pct` is absent (editor.js:1629-1630 backward-compat path);
on save, only `font_size_pct` is written. Operator works in
percentage-of-slide-width terms; pixel-precise font sizing is
impossible through the UI.

Likely-intentional (responsive design) — but it's worth confirming
the design intent. If pixel-precise font sizing was meant to be
operator-controllable, this is a gap.

### 3. F078 — `SystemSettings.tailscale_https_enabled` (NOT_EXPOSED, HIGH)

No admin console toggle for HTTPS provisioning. Field is consumed by
`fqdn_redirect_middleware.py:120` and the install.sh tailscale
provisioning flow, but zero UI surface lets the operator change it.
This is the **known gap** documented in
[[project_https_phase_1_shipped_2026_05_24]] as
"awaiting qarl admin-console HTTPS toggle".

Adversarial review: 3/3 skeptics confirmed the gap (no alternate
naming `httpsEnabled`/`provisionHttps`/etc.; no indirect transformer;
no non-editor surface).

Fix path: add an HTTPS-enabled checkbox to the Tailscale fieldset in
settings.js. Estimate ~15 LOC.

### 4. F081 — `PlaylistItem.transition_ms` (DEFAULT_ONLY, HIGH)

Renderer honors at content.rs:48 + inline-preview.js:322. UI shows a
transition KIND select chip on each playlist track block (cut/fade/wipe/
etc.) but provides NO duration control. Every entry hard-codes 500ms
at creation (playlist-track.js:508 + stream-upload.js:96 + web-slide.js:130).

Operator impact: operators can pick HOW the transition looks but not
how long it takes. A 500ms cut and a 500ms iris feel very different;
operators wanting smoother (e.g. 1200ms) or snappier (200ms)
transitions are stuck.

Fix path: add a transition_ms duration input (number/slider) to the
track block editor in playlist-track.js, paired with the existing
transition KIND select. Estimate ~40 LOC.

### 5. F010 — `TextLayer.weight` (DEFAULT_ONLY, MEDIUM)

Variable-font weight derived per-family in font-picker.js FONT_FAMILIES
table; used only for picker-tile CSS preview + document.fonts.load.
Never read from a wire TextLayer (layerFromWire omits it); never
written into the save payload. Renderer's TextLayer struct
(content.rs:120-198) ALSO has no weight field — so the field is
effectively model-only.

This means: variable fonts like Inter/Oswald ship with their
designer-chosen default weight; operators cannot pick a 300/Light or
700/Bold variant per layer.

Fix path: add weight to the TextLayer model + UI control + renderer
struct. Larger scope than the others (~80-100 LOC across 3 surfaces).

## Subagent disagreements (adversarial flips)

**Zero (0) flips.** All 14 NOT_EXPOSED + DEFAULT_ONLY + READ_ONLY-with-
operator-impact findings survived 3-lens skeptic refutation. Each
skeptic was prompted to default to refuted=true and only flip if a
concrete UI surface was found.

Matching r47's gold standard: zero false-positive risk on the audit.


## Full claim-by-claim table

| ID | Model | Field | Verdict | Sev | Renderer? | UI read | UI write |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F001 | TextBox | `x` | **VERIFIED** |  | yes | ui/src/editor.js:1645 | ui/src/editor.js:1504 |
| F002 | TextBox | `y` | **VERIFIED** |  | yes | ui/src/editor.js:1646 | ui/src/editor.js:1504 |
| F003 | TextBox | `w` | **VERIFIED** |  | yes | ui/src/editor.js:1647 | ui/src/editor.js:1504 |
| F004 | TextBox | `h` | **VERIFIED** |  | yes | ui/src/editor.js:1648 | ui/src/editor.js:1504 |
| F005 | TextLayer | `text` | **VERIFIED** |  | yes | ui/src/editor.js:1623 | ui/src/editor.js:1489 |
| F006 | TextLayer | `name` | **VERIFIED** |  | no | ui/src/editor.js:1624 | ui/src/editor.js:1490 |
| F007 | TextLayer | `font_family` | **VERIFIED** |  | yes | ui/src/editor.js:1626 | ui/src/editor.js:1492 |
| F008 | TextLayer | `font_size_px` | **READ_ONLY** | HIGH | yes | ui/src/editor.js:1629 | NONE |
| F009 | TextLayer | `font_size_pct` | **VERIFIED** |  | yes | ui/src/editor.js:1627-1631 | ui/src/editor.js:1493 |
| F010 | TextLayer | `weight` | **DEFAULT_ONLY** | MEDIUM | no | ui/src/font-picker.js:13-38 | NONE |
| F011 | TextLayer | `text_color` | **VERIFIED** |  | yes | ui/src/editor.js:1625 | ui/src/editor.js:1491 |
| F012 | TextLayer | `text_align` | **VERIFIED** |  | yes | ui/src/editor.js:1634 | ui/src/editor.js:1496 |
| F013 | TextLayer | `outline` | **NOT_EXPOSED** | CRITICAL | yes | NONE | NONE |
| F014 | TextLayer | `opacity` | **VERIFIED** |  | yes | ui/src/editor.js:1640 | ui/src/editor.js:1502 |
| F015 | TextLayer | `anchor` | **NOT_EXPOSED** | LOW | no | NONE | NONE |
| F016 | TextLayer | `visible` | **VERIFIED** |  | yes | ui/src/editor.js:1641 | ui/src/editor.js:1503 |
| F017 | TextLayer | `locked` | **NOT_EXPOSED** | LOW | no | NONE | NONE |
| F018 | TextLayer | `motion` | **VERIFIED** |  | yes | ui/src/editor.js:1080 | ui/src/editor.js:665 |
| F019 | TextLayer | `motion_intensity` | **VERIFIED** |  | yes | ui/src/editor.js:1112 | ui/src/editor.js:673 |
| F020 | TextLayer | `motion_phase` | **VERIFIED** |  | yes | ui/src/editor.js:1113 | ui/src/editor.js:677 |
| F021 | TextLayer | `motion_speed` | **VERIFIED** |  | yes | ui/src/editor.js:1114 | ui/src/editor.js:681 |
| F022 | TextLayer | `blend` | **VERIFIED** |  | yes | ui/src/editor.js:1106 | ui/src/editor.js:667 |
| F023 | TextLayer | `auto_mode` | **VERIFIED** |  | yes | ui/src/editor.js:1090 | ui/src/editor.js:660 |
| F024 | TextLayer | `auto_format` | **VERIFIED** |  | yes | ui/src/editor.js:1092 | ui/src/editor.js:662 |
| F025 | TextLayer | `box` | **VERIFIED** |  | yes | ui/src/editor.js:449-452,1645-1648 | ui/src/editor.js:603,1504 |
| F026 | BackgroundPattern | `pattern` | **VERIFIED** |  | yes | ui/src/state-from-item.js:38-41; ui/src/editor.js:1362,1387 | ui/src/editor.js:1376,1512 |
| F027 | BackgroundPattern | `color_a` | **VERIFIED** |  | yes | ui/src/state-from-item.js:39; ui/src/editor.js:420,1368 | ui/src/editor.js:1400,1513 |
| F028 | BackgroundPattern | `color_b` | **VERIFIED** |  | yes | ui/src/state-from-item.js:40; ui/src/editor.js:421,1368 | ui/src/editor.js:1406,1514 |
| F029 | BackgroundPattern | `density` | **VERIFIED** |  | yes | ui/src/state-from-item.js:41; ui/src/editor.js:422 | ui/src/editor.js:1413,1515 |
| F030 | TextSlide | `name` | **VERIFIED** |  | no | ui/src/editor.js:1663-1664 | ui/src/editor.js:113,631,1507 |
| F031 | TextSlide | `duration_ms` | **VERIFIED** |  | yes | ui/src/editor.js:1666 | ui/src/editor.js:117,1517 |
| F032 | TextSlide | `text_layers` | **VERIFIED** |  | yes | ui/src/editor.js:1668-1669; ui/src/state-from-item.js:30 | ui/src/editor.js:1488,1518 |
| F033 | TextSlide | `background_color` | **VERIFIED** |  | yes | ui/src/editor.js:1665 | ui/src/editor.js:1508 |
| F034 | TextSlide | `background_image_slide_id` | **VERIFIED** |  | yes | ui/src/editor.js:1694 | ui/src/editor.js:1509 |
| F035 | TextSlide | `background_video_slide_id` | **VERIFIED** |  | no | ui/src/editor.js:1711 | ui/src/editor.js:1510 |
| F036 | TextSlide | `background_pattern` | **VERIFIED** |  | yes | ui/src/state-from-item.js:35 | ui/src/editor.js:1511 |
| F037 | TextSlide | `transition` | **NOT_EXPOSED** | LOW | yes | NONE | NONE |
| F038 | TextSlide | `transition_ms` | **NOT_EXPOSED** | LOW | yes | NONE | NONE |
| F039 | ImageSlide | `name` | **VERIFIED** |  | yes | ui/src/image-upload.js:208 | ui/src/image-upload.js:89 |
| F040 | ImageSlide | `duration_ms` | **VERIFIED** |  | yes | ui/src/image-upload.js:209 | ui/src/image-upload.js:90 |
| F041 | ImageSlide | `transition` | **NOT_EXPOSED** | LOW | no | NONE | NONE |
| F042 | ImageSlide | `transition_ms` | **NOT_EXPOSED** | LOW | no | NONE | NONE |
| F043 | VideoSlide | `name` | **VERIFIED** |  | yes | ui/src/video-upload.js:358 | ui/src/video-upload.js:277 |
| F044 | VideoSlide | `duration_ms` | **VERIFIED** |  | yes | ui/src/video-upload.js:360 | ui/src/video-upload.js:278 |
| F045 | VideoSlide | `transition` | **NOT_EXPOSED** | LOW | no | NONE | NONE |
| F046 | VideoSlide | `transition_ms` | **NOT_EXPOSED** | LOW | no | NONE | NONE |
| F047 | StreamSlide | `name` | **VERIFIED** |  | no | ui/src/stream-upload.js:198 | ui/src/stream-upload.js:122 |
| F048 | StreamSlide | `stream_url` | **VERIFIED** |  | no | ui/src/stream-upload.js:199 | ui/src/stream-upload.js:123 |
| F049 | StreamSlide | `duration_ms` | **VERIFIED** |  | yes | ui/src/stream-upload.js:200-201 | ui/src/stream-upload.js:124 |
| F050 | StreamSlide | `on_unreachable` | **VERIFIED** |  | no | ui/src/stream-upload.js:203 | ui/src/stream-upload.js:125 |
| F051 | StreamSlide | `transition` | **DEFAULT_ONLY** | LOW | yes | ui/src/stream-upload.js:196 | NONE |
| F052 | StreamSlide | `transition_ms` | **DEFAULT_ONLY** | LOW | yes | ui/src/stream-upload.js:197 | NONE |
| F053 | WebSlide | `name` | **VERIFIED** |  | no | ui/src/web-slide.js:249 | ui/src/web-slide.js:174 |
| F054 | WebSlide | `url` | **VERIFIED** |  | no | ui/src/web-slide.js:250 | ui/src/web-slide.js:175 |
| F055 | WebSlide | `refresh_interval_s` | **VERIFIED** |  | no | ui/src/web-slide.js:251 | ui/src/web-slide.js:176 |
| F056 | WebSlide | `duration_ms` | **VERIFIED** |  | yes | ui/src/web-slide.js:252-253 | ui/src/web-slide.js:177 |
| F057 | WebSlide | `transition` | **READ_ONLY** | LOW | yes | ui/src/web-slide.js:247 | ui/src/web-slide.js:178 |
| F058 | WebSlide | `transition_ms` | **READ_ONLY** | LOW | yes | ui/src/web-slide.js:248 | ui/src/web-slide.js:179 |
| F059 | SystemSettings | `sign_name` | **VERIFIED** |  | no | ui/src/settings.js:739 | ui/src/settings.js:887 |
| F060 | SystemSettings | `flock_sync_enabled` | **VERIFIED** |  | no | ui/src/flock.js:529 | ui/src/main.js:859 |
| F061 | SystemSettings | `ui_first_run_seen` | **VERIFIED** |  | no | ui/src/main.js:288 | ui/src/main.js:294 |
| F062 | SystemSettings | `output_mode` | **VERIFIED** |  | no | ui/src/settings.js:741 | ui/src/settings.js:888 |
| F063 | SystemSettings | `display_width` | **VERIFIED** |  | yes | ui/src/settings.js:745 | ui/src/settings.js:889 |
| F064 | SystemSettings | `display_height` | **VERIFIED** |  | yes | ui/src/settings.js:746 | ui/src/settings.js:890 |
| F065 | SystemSettings | `display_rotation` | **VERIFIED** |  | yes | ui/src/settings.js:747 | ui/src/settings.js:891 |
| F066 | SystemSettings | `brightness` | **VERIFIED** |  | yes | ui/src/settings.js:748 | ui/src/settings.js:892 |
| F067 | SystemSettings | `gamma` | **VERIFIED** |  | yes | ui/src/settings.js:749 | ui/src/settings.js:893 |
| F068 | SystemSettings | `wifi_ap_enabled` | **VERIFIED** |  | no | ui/src/settings.js:750 | ui/src/settings.js:894 |
| F069 | SystemSettings | `wifi_ssid` | **VERIFIED** |  | no | ui/src/settings.js:751 | ui/src/settings.js:895 |
| F070 | SystemSettings | `wifi_password` | **VERIFIED** |  | no | ui/src/settings.js:757 | ui/src/settings.js:896 |
| F071 | SystemSettings | `wifi_station_enabled` | **VERIFIED** |  | no | ui/src/settings.js:759 | ui/src/settings.js:897 |
| F072 | SystemSettings | `wifi_station_ssid` | **VERIFIED** |  | no | ui/src/settings.js:760 | ui/src/settings.js:898 |
| F073 | SystemSettings | `wifi_station_password` | **VERIFIED** |  | no | ui/src/settings.js:761 | ui/src/settings.js:899 |
| F074 | SystemSettings | `timezone` | **VERIFIED** |  | yes | ui/src/settings.js:773 | ui/src/settings.js:901 |
| F075 | SystemSettings | `tailscale_enabled` | **VERIFIED** |  | no | ui/src/settings.js:767 | ui/src/settings.js:902 |
| F076 | SystemSettings | `tailscale_auth_key` | **VERIFIED** |  | no | ui/src/settings.js:769 | ui/src/settings.js:904 |
| F077 | SystemSettings | `tailscale_hostname` | **VERIFIED** |  | no | ui/src/settings.js:768 | ui/src/settings.js:903 |
| F078 | SystemSettings | `tailscale_https_enabled` | **NOT_EXPOSED** | HIGH | no | NONE | NONE |
| F079 | PlaylistItem | `item_id` | **VERIFIED** |  | yes | ui/src/playlist-track.js:309 | ui/src/playlist-track.js:565 |
| F080 | PlaylistItem | `transition` | **VERIFIED** |  | yes | ui/src/playlist-track.js:310 | ui/src/playlist-track.js:566 |
| F081 | PlaylistItem | `transition_ms` | **DEFAULT_ONLY** | HIGH | yes | ui/src/playlist-track.js:311 | NONE |
| F082 | Playlist | `name` | **VERIFIED** |  | yes | ui/src/playlist-track.js:305 | ui/src/playlist-track.js:106 |
| F083 | Playlist | `items` | **VERIFIED** |  | yes | ui/src/playlist-track.js:306 | ui/src/playlist-track.js:567 |
| F084 | ScheduleRule | `name` | **VERIFIED** |  | no | ui/src/schedule.js:285 | ui/src/schedule.js:285 |
| F085 | ScheduleRule | `days` | **VERIFIED** |  | no | ui/src/schedule.js:300 | ui/src/schedule.js:362 |
| F086 | ScheduleRule | `start_time` | **VERIFIED** |  | no | ui/src/schedule.js:310 | ui/src/schedule.js:365 |
| F087 | ScheduleRule | `end_time` | **VERIFIED** |  | no | ui/src/schedule.js:314 | ui/src/schedule.js:366 |
| F088 | ScheduleRule | `playlist_id` | **VERIFIED** |  | no | ui/src/schedule.js:280 | ui/src/schedule.js:367 |
| F089 | ScheduleRule | `enabled` | **VERIFIED** |  | no | ui/src/schedule.js:289 | ui/src/schedule.js:368 |
| F090 | Schedule | `rules` | **VERIFIED** |  | no | ui/src/schedule.js:125 | ui/src/schedule.js:360 |
| F091 | Schedule | `default_playlist_id` | **VERIFIED** |  | no | ui/src/schedule.js:119 | ui/src/schedule.js:377 |
| F092 | Schedule | `tz` | **READ_ONLY** | LOW | no | ui/src/schedule.js:121 | NONE |

## §F. Outer-repo recommendations for admin-Jimmy (SYSTEM_SPEC.md candidates)

Per the dispatch §F: list operator-facing fields whose SYSTEM_SPEC.md
description claims operator-controllability that the UI doesn't deliver.
These are downstream SYSTEM_SPEC rewrites for admin-Jimmy.

After cross-checking against r47's spec audit findings:

### F.1 §5.10a TextLayer outline + auxiliary text effects (related to F013)

SYSTEM_SPEC §5.10a (text-layer outline/stroke/shadow) implies
operator-controllable text effects. The renderer honors outline;
the UI does not surface it. Either:

  (A) DOC: tag §5.10a's outline mention as "renderer-supported;
      editor surface pending"
  (B) IMPL: ship the editor controls (downstream dispatch, see §G
      below)

This is a candidate for both lanes — F.1 is the audit's note for
admin-Jimmy that the spec text may currently OVER-promise operator
reach. qarl product judgment required.

### F.2 §5.8 Admin console + HTTPS toggle (F078)

SYSTEM_SPEC §5.8 settings page section describes operator-controllable
network settings. tailscale_https_enabled exists in the model but
not in the UI. The MEMORY entry [[project_https_phase_1_shipped_2026_05_24]]
already acknowledges "awaiting qarl admin-console HTTPS toggle" —
this audit confirms the gap is still open.

Recommended SYSTEM_SPEC.md change: if the spec text claims an HTTPS
toggle in §5.8 settings, either tag it as "implementation pending"
OR (preferable) land the toggle as a downstream dispatch then leave
§5.8 unchanged.

### F.3 §5.4 Playlist transition_ms (F081)

If SYSTEM_SPEC §5.4 (playlists / transitions) claims operator-
selectable transition duration, the spec is currently OVER-promising
— UI only exposes transition KIND. The dispatch did not call out a
specific spec line, so this is a flag for admin-Jimmy to check
SYSTEM_SPEC §5.4 + §7.1 for transition-duration phrasing.

(No other field-level discrepancies surfaced as SYSTEM_SPEC.md
candidates beyond these three.)

## §G. Downstream dispatch candidates (impl work, per severity)

Not the audit's job to fix; listed here so qarl + QA can route:

  CRITICAL  F013 TextLayer.outline — ~60 LOC editor + defaults + CSS
  HIGH      F078 tailscale_https_enabled — ~15 LOC settings.js Tailscale
            fieldset
  HIGH      F081 PlaylistItem.transition_ms — ~40 LOC playlist-track.js
            duration input on track block
  HIGH      F008 TextLayer.font_size_px — confirm intent first (likely
            by-design); if implementation needed, ~30 LOC editor
  MEDIUM    F010 TextLayer.weight — ~80-100 LOC (model + UI + renderer
            struct add)

The 14 LOW-severity findings (legacy slide-level transitions; future-
reserved fields like TextLayer.anchor / locked / Schedule.tz) are
NOT recommended for impl work — they're by-design per the v3+
architecture migration. If anything, candidates for MODEL CLEANUP
(remove the legacy fields entirely) but that's a separate cleanup
dispatch, not an exposure fix.

## True exposure gaps (operator-impact severity ≥ MEDIUM)

These are findings where the operator would reasonably want to control the
field but cannot. Ranked by severity.

### F013 — `TextLayer.outline` (NOT_EXPOSED, **CRITICAL**)

**Type:** `bool`  
**Model:** `backend/openmarquee/content/__init__.py:188`  
**Renderer honors:** **YES**  
**Purpose:** Editor-set stroke outline around glyphs (render support pending).

**UI read:** _none_  
**UI write:** _none_  

**Evidence:** Grep of ui/src/ for `\.outline`, `layer.outline`, `text_outline`, `hasOutline`, `outlineEnabled`, `field-outline` returns ZERO matches as a TextLayer property (only icon stroke widths and `.rule-invalid` outline CSS in unrelated files). layerFromWire (editor.js:1621-1652) does not extract outline; the save payload at editor.js:1488-1505 does not include outline. Yet renderer/src/content.rs:187 deserializes `outline: bool` and hdmi.rs:2421-2554 dispatches FS_MSDF_OUTLINE shader when layer.outline is true. Operator-relevant + renderer-honored + zero UI surface = exactly the qarl-frustration outline-gap shape.

**Adversarial review:** 3/3 skeptics could not refute via alternate-field-names, indirect-surface, or non-editor-surface lenses. Verdict stands.

---

### F008 — `TextLayer.font_size_px` (READ_ONLY, **HIGH**)

**Type:** `Optional[int]`  
**Model:** `backend/openmarquee/content/__init__.py:169`  
**Renderer honors:** **YES**  
**Purpose:** Absolute pixel font size; renderer/canvas-side glyph metric input.

**UI read:** `ui/src/editor.js:1629`  
**UI write:** _none_  

**Evidence:** layerFromWire only reads wire.font_size_px to derive a fontSizePct value when font_size_pct is absent ((wire.font_size_px / width) * 100, editor.js:1629-1630); the editor's slider (.field-font-size) writes font_size_pct only (editor.js:1493) and the save payload never re-emits font_size_px. Renderer honors font_size_px (renderer/src/content.rs:131). Operators effectively control font size only through the percentage slider; legacy/imported font_size_px values are silently converted away on the first save. Severity HIGH because operator-relevant control exists via a sibling field (pct) but legacy or scripted px values cannot be edited as px and are lossy-converted.

---

### F078 — `SystemSettings.tailscale_https_enabled` (NOT_EXPOSED, **HIGH**)

**Type:** `bool`  
**Model:** `backend/openmarquee/settings.py:240`  
**Renderer honors:** no  
**Purpose:** Whether to provision HTTPS via tailscale serve + 301-redirect non-FQDN traffic to the canonical HTTPS FQDN.

**UI read:** _none_  
**UI write:** _none_  

**Evidence:** grep across ui/src/ for 'tailscale_https_enabled' and 'tailscaleHttps' returns zero matches. Field is consumed only by backend (api_settings, fqdn_redirect_middleware.py:120) and is not surfaced anywhere in the settings.js Tailscale section, leaving operators with no way to toggle HTTPS provisioning. MEMORY notes 'awaiting qarl admin-console HTTPS toggle' — i.e., known operator-facing gap.

**Adversarial review:** 3/3 skeptics could not refute via alternate-field-names, indirect-surface, or non-editor-surface lenses. Verdict stands.

---

### F081 — `PlaylistItem.transition_ms` (DEFAULT_ONLY, **HIGH**)

**Type:** `int`  
**Model:** `backend/openmarquee/playlist.py:123`  
**Renderer honors:** **YES**  
**Purpose:** Authoritative (v3+) transition duration ms (0-5000) owned by the playlist entry.

**UI read:** `ui/src/playlist-track.js:311`  
**UI write:** _none_  

**Evidence:** transition_ms is round-tripped: read from server (playlist-track.js:311 default 500), stamped onto block dataset.transitionMs (701), and re-emitted by collectTrackEntries (567). However, the only UI control on a track block is the transition KIND select chip (.track-block-transition at 592-601) and the slide-level duration prompt. No control exists to edit transition_ms duration; new entries hard-code 500ms (line 508, 316). stream-upload.js/web-slide.js also hard-code 500ms. inline-preview.js honors the value (322).

**Adversarial review:** 3/3 skeptics could not refute via alternate-field-names, indirect-surface, or non-editor-surface lenses. Verdict stands.

---

### F010 — `TextLayer.weight` (DEFAULT_ONLY, **MEDIUM**)

**Type:** `Optional[int]`  
**Model:** `backend/openmarquee/content/__init__.py:180`  
**Renderer honors:** no  
**Purpose:** CSS-style font weight (100-900) used for variable-weight bundled families like Inter/Oswald.

**UI read:** `ui/src/font-picker.js:13-38`  
**UI write:** _none_  

**Evidence:** Weight is derived per font family in FONT_FAMILIES table (font-picker.js:13-38) and used only for fontWeight CSS styling of the picker tiles (font-picker.js:112,126) and document.fonts.load (editor.js:868). It is NEVER read from a wire TextLayer, never written into the save payload at editor.js:1488-1505, and the renderer's TextLayer struct (renderer/src/content.rs:120-198) has no `weight` field. There is no per-layer weight control; the operator cannot pick a non-default weight for a variable font.

**Adversarial review:** 3/3 skeptics could not refute via alternate-field-names, indirect-surface, or non-editor-surface lenses. Verdict stands.

---


## Intentional-design findings (LOW; documented or by-design)

These NOT_EXPOSED/DEFAULT_ONLY/READ_ONLY findings are by-design per the v3+
playlist-owns-transitions architecture, future-reserved fields, or other
documented design choices. Listed for completeness; not actionable.

| ID | Field | Verdict | Renderer? | Design note |
| --- | --- | --- | --- | --- |
| F015 | `TextLayer.anchor` | NOT_EXPOSED | no | forward-compat (renderer struct also lacks field) |
| F017 | `TextLayer.locked` | NOT_EXPOSED | no | UI lock toggle never built |
| F037 | `TextSlide.transition` | NOT_EXPOSED | yes | v3+ playlist owns transitions; slide-side legacy |
| F038 | `TextSlide.transition_ms` | NOT_EXPOSED | yes | v3+ playlist owns transitions; slide-side legacy |
| F041 | `ImageSlide.transition` | NOT_EXPOSED | no | v3+ playlist owns transitions; slide-side legacy |
| F042 | `ImageSlide.transition_ms` | NOT_EXPOSED | no | v3+ playlist owns transitions; slide-side legacy |
| F045 | `VideoSlide.transition` | NOT_EXPOSED | no | v3+ playlist owns transitions; slide-side legacy |
| F046 | `VideoSlide.transition_ms` | NOT_EXPOSED | no | v3+ playlist owns transitions; slide-side legacy |
| F051 | `StreamSlide.transition` | DEFAULT_ONLY | yes | v3+ playlist owns transitions; slide-side legacy |
| F052 | `StreamSlide.transition_ms` | DEFAULT_ONLY | yes | v3+ playlist owns transitions; slide-side legacy |
| F057 | `WebSlide.transition` | READ_ONLY | yes | v3+ playlist owns transitions; slide-side legacy |
| F058 | `WebSlide.transition_ms` | READ_ONLY | yes | v3+ playlist owns transitions; slide-side legacy |
| F092 | `Schedule.tz` | READ_ONLY | no | reserved for future zoned evaluator (None=use device local) |

## §H. Notes on classification edge cases

### H.1 The "renderer_honors" axis matters for prioritization

The audit added a renderer_honors boolean per field. Findings where
**(NOT_EXPOSED + renderer_honors=true)** are the worst class — the
behavior code exists, is wired, and ships, but the operator can't
reach it. This is the F013 outline / F081 transition_ms shape.

Findings where (NOT_EXPOSED + renderer_honors=false) are merely
model dead code — the field is declared, never read, but causes no
operator surprise. Lower urgency.

### H.2 By-design DEFAULT_ONLY for legacy fields is correct

10 of the 14 LOW findings are slide-level `transition` / `transition_ms`
that intentionally defer to playlist-level after v3 (per playlist-
track.js + stream-upload.js + web-slide.js explicit comments). The
audit correctly classifies these as DEFAULT_ONLY but the right fix
is to NOT add UI for them — instead consider model cleanup to
remove the legacy fields entirely.

### H.3 25 system-internal fields parked

The enumerator correctly skipped 25 system-internal fields (`id`,
`created_at`, `updated_at`, `type` discriminators, `schema_version`
markers, `item_ids` computed legacy field). These are not operator-
controllable; their absence from UI editors is correct.

### H.4 What the audit deliberately did NOT do

- **Did not fix anything.** Per dispatch constraint, audit-only.
  Downstream dispatches per severity ranking in §G.
- **Did not audit api_*.py wrapper models.** TextLayerUpload /
  TextSlideUpload / PlaylistCreate / etc. are wire-format
  variants of the audited models; their fields are a subset.
- **Did not check renderer/src/content.rs deserialize shape.**
  The audit's renderer_honors flag is a grep-based heuristic; a
  field could be deserialized into a struct that's never read,
  which would still appear as "renderer_honors=true". Confidence
  is high for the 4 ranked findings; lower for the LOW set.
- **Did not edit SYSTEM_SPEC.md.** §F is admin-Jimmy's lane;
  this doc lists recommended edits, doesn't apply them.
- **Did not audit the schedule.tz / future-zoned-evaluator path.**
  F092 Schedule.tz is reserved for future use; the audit correctly
  flagged it as READ_ONLY/LOW with the design intent annotation.

## §I. Lane

- Doc-only commit (audit doc only)
- Subagent review on the OVERALL audit doc before commit (separate
  from per-field adversarial passes in §C run by the workflow)
- Standard /tmp clone + cherry-pick push if NFS-wedges
- No memory updates required — feedback memory class is
  "deploy/build foot-guns", not "ui-vs-model audit findings"

## §J. Push posture

- 1 file committed: `qa/r49-ui-vs-model-audit-2026-06-03.md` (audit doc)
- NOT committed: `.claude/workflows/r49-ui-vs-model-audit.mjs`
  (workflow script — per standing rule "don't commit `.claude/`
  artifacts"; the §Methodology block above is the canonical
  description for reproducibility)
- NOT committed: `/tmp/r49-results.json` (transient intermediate)
- Pre-push hook applies only if renderer/ui/backend changed; audit
  doc lands as a docs-only commit per the lane.

---

End of r49 audit.
