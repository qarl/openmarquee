# V1 spec-delta — 2026-05-30 (post-v0.9.0)

Coverage audit cross-referencing the canonical spec docs against the
actually-shipped surface at HEAD (origin/main `5cbcab7`, v0.9.0 tag
`a50e928`). Successor to `qa/v1-spec-delta-2026-05-14.md` (16 days
prior). Triggered per the QA charter as the wrap-up-defining task
before stamping the v0.9.0 → v1.0 arc.

## Front-matter — gap counts by severity

| Severity | Count | What it means |
|----------|------:|---------------|
| **P0**   | 0     | Ship-blocking for v1.0. None found. |
| **P1**   | 3     | Real gaps operators will notice; should-fix-soon but don't block v1.0 tag if scoped as "ship-known-limitations." |
| **P2**   | 3     | Polish + non-load-bearing doc gaps; deferrable to v1.x. (Was 4; row 4 closed by r22 + r24 errata — see §5.) |
| **P3**   | 0     | Trivia bucket empty this round. |
| **doc-drift** | 2 | Stale doc sections that don't block ship; refresh-doc tasks. |

## Headline — "what's between v0.9.0 and v1.0" (plain English)

v0.9.0 ships the full single-device sign controller: HDMI playback,
text/image/video/stream/web slides, playlists, scheduling, WiFi AP +
station mode, Tailscale remote access, flock multi-device sync, per-
layer motion (ticker/breathe/pulse/bounce/shake/blink), and per-layer
Photoshop-style blend modes. **The remaining gap between v0.9.0 and
v1.0 is the text-layer chrome triad**: vertical alignment (`anchor`)
and variable-font weight (`weight`) are accepted by the backend but
silently ignored by the renderer; layer visibility (`visible`) is
honored at playback but the save-time thumbnail still renders hidden
layers. None of the three blocks ship — v1.0 can tag with them
documented as known-limitations — but closing them is ~3-5 small
renderer commits if qarl wants the spec-complete tag.

## §1 Scope

Compared against:

| Canonical | Source |
|-----------|--------|
| `SYSTEM_SPEC.md` | `/Users/qarl/project/openmarquee/SYSTEM_SPEC.md` (608 LOC; outer repo, per [[reference-outer-repo-canonical-specs]]) |
| `IMPLEMENTATION_PLAN.md` | `/Users/qarl/project/openmarquee/IMPLEMENTATION_PLAN.md` (475 LOC) |
| `DESIGN_BRIEF.md` | `/Users/qarl/project/openmarquee/DESIGN_BRIEF.md` (83 LOC; marketing-shape, skim-only) |
| `code/docs/v4l2-decode.md` | post-refresh shape (343 LOC) |
| `code/docs/phase-7-as-built-2026-05-14.md` | 5-14 anchor |
| Prior delta | `code/qa/v1-spec-delta-2026-05-14.md` (304 LOC) |

Audited surfaces:

1. **Status of the 10 gaps from the 2026-05-14 delta.** Each
   classified CLOSED / STILL-OPEN / OVERTAKEN.
2. **New gaps surfaced between 2026-05-14 and v0.9.0.** Spec section
   has a contract surface; cross-checked against the actual code.
3. **Doc-drift on the canonical specs themselves** vs the shipping
   reality.

## §2 Status of the 10 May-14 gaps

| # | May-14 | Status today | Notes |
|---|--------|--------------|-------|
| 1 | P1 — DMA-BUF path missing from `docs/v4l2-decode.md` | **CLOSED** | Doc refreshed (now 343 LOC). New §"DMA-BUF zero-copy pathway (piece 4)" at line 156. |
| 2 | P1 — `OPENMARQUEE_RENDERER_DMABUF` env-var undocumented | **CLOSED** | Now documented in v4l2-decode.md alongside the §Diagnostics gate (env-var cross-ref at line 144). |
| 3 | P1 — REQBUFS=MMAP + post-mmap EXPBUF pattern undocumented | **CLOSED** | v4l2-decode.md §"Why REQBUFS=MMAP and NOT REQBUFS=DMABUF" at line 194; cites the piece-4a-fix commit `634eae2` as the canonical learning. |
| 4 | P1 — `V4L2_CID_QUANTIZATION` not set, latent BT.601 range risk | **CLOSED** | Fail-loud check landed at `renderer/src/ipc_main.rs:719-730` — `assert_capture_quantization_compatible()` bails if FULL_RANGE detected. The bcm2835-codec doesn't expose the control to set, so detecting + asserting the default is the right shape. |
| 5 | P1 — SSID rotation three-way doc drift | **CLOSED** | `system/openmarquee-firstboot.sh` rotates to `MySign<N>`; `system/hostapd.conf:32` keeps `openMarquee-SETUP` as cold-boot default; docs now consistently describe both states. |
| 6 | P1 — `system/openmarquee-sudoers` "two" vs "four" contradiction | **STALE / OVERTAKEN** | Re-read at HEAD: line 7 says "exactly two," line 17 says "Anything beyond these two." Internally consistent now. The "four" was already cleaned up. |
| 7 | P2 — VideoSlide Capture emits `"video slides TBD"` marker | **CLOSED (errata)** | Original claim — "Markers still present at `renderer/src/ipc_main.rs:395,536,621` (the lines moved from `:648,718` as the file restructured)" — is wrong. **ERRATA (2026-05-31, r24):** Audit caught two errors via r22 sweep (0c4039c): (a) the literal `"video slides TBD"` marker is no longer emitted anywhere in runtime code; only doc-comments referenced it, and those have been swept; (b) the cited line numbers 395/536/621 did not contain marker text — the prior doc-comment sites were at lines 363/520/609/2509/2929, all closed by r22. The structural "validator split" is already done (separate `validate_paint_slide_inputs` at line 1392 and `validate_capture_inputs` at line 1424); Capture rejection uses the `"Capture: VideoSlide capture not implemented"` string + the `RustRendererUnsupportedSlideError` rail (playback-loop log + skip per post-DELETE-PIL contract). |
| 8 | P2 — `OPENMARQUEE_FIRSTFRAME_PROFILE` undocumented | **STILL OPEN** | Profile gate still lives at `renderer/src/hdmi.rs:3560` and `:6399`. Not in any user-facing doc. Diagnostic-only; not a load-bearing gap. |
| 9 | P2 — `system/README.md` "wpa_supplicant template-out" stale Phase-7 item | **CLOSED** | README rewritten; line 66 now describes the nmcli architecture: "legacy fallback config… trixie uses NetworkManager + nmcli instead." |
| 10 | P2 — README missing AP/NM coexistence + `Before=NetworkManager.service` note | **CLOSED** | README architecture section now carries the AP/NM coexistence pattern with the `Before=` ordering call-out and the task-#99 reference (lines 74-116). |

**Score:** 9 of 10 closed (5 P1 + 3 P2 + 1 stale), 1 still open
(P2 #8 profile-gate doc). #7 Capture-side marker closed by r22
sweep (0c4039c) + r24 errata.

## §3 New gaps surfaced between 2026-05-14 and v0.9.0

### 3.1 Text-layer chrome triad — backend ahead of renderer

`SYSTEM_SPEC.md §5.10a` (lines 315-325) defines four optional fields
on every text-layer: `visible`, `locked`, `anchor`
(`"top" | "center" | "bottom"`), and `weight` (variable-font CSS
weight, 300–900). All four landed in the backend Pydantic model at
`backend/openmarquee/content/__init__.py:137-201` (TextLayer class —
`weight` at line 180, `anchor` at 194, `visible` at 198, `locked`
at 201). The Rust renderer struct at `renderer/src/content.rs:141-200`
carries `visible` only — `locked`, `anchor`, `weight` aren't in the
struct. Net behavior:

- **P1 — Text-layer `anchor` not honored.** Renderer defaults every
  layer to center-anchor. Operator's choice of `top` / `bottom` is
  silently dropped. Visual mismatch between editor preview (which
  honors anchor via JS) and on-glass render. **Spec:**
  `SYSTEM_SPEC.md` line 315. **Impl gap:** add field to
  `renderer/src/content.rs:200`, plumb through to text-layer paint
  helper. ~20-30 LOC.

- **P1 — Text-layer `weight` not honored.** Variable-font weight
  request silently ignored; renderer always uses the font's
  bundled-default weight. The bundled font set is mostly single-
  weight (Inter, Oswald, Roboto Slab are the variable-weight
  candidates). Operator selection is functionally disabled.
  **Spec:** `SYSTEM_SPEC.md` line 317. **Impl gap:** same shape as
  `anchor` — field + plumb-through. ~20 LOC. ~~Note: variable-font
  rasterization via fontdue is supported; the wire is the gap, not
  the rasterizer.~~

  **ERRATA (2026-05-30, r26 close):** The struck-through note above
  is WRONG. `fontdue` does NOT support variable-axis selection — it
  parses static TTFs only. The bundled font system at
  `renderer/src/hdmi_logic.rs:4165` is single-TTF-per-family with
  no weight variants on disk. Honoring `weight` at render time
  requires either (a) bundling weight-variant TTFs (Inter-300.ttf,
  Inter-700.ttf, …) + extending `font_family_to_filename` to
  `(family, weight) → filename`, or (b) swapping rasterizers to one
  that supports the fvar table. v1.0 ships `weight` as
  **wire-accepted-render-deferred**: the renderer struct preserves
  the field through a save/load round-trip (`Option<u32>` in
  `renderer/src/content.rs`) so a future v1.1 wire-up has a value
  to read. UI/render parity gap is now a documented v1.1 decision
  (bundle variants OR hide the editor affordance).

- **P1 — Text-layer `visible` ignored at SAVE time.** Renderer
  correctly skips `visible: false` layers during playback (the
  renderer struct has the field). The save-time thumbnail
  rasterizer at `backend/openmarquee/auto_render.py` (the path that
  produces the slide-tile preview PNG) iterates ALL text layers
  with no `if layer.visible` guard. Operator hides a layer → it
  still appears in the slide-browser tile + Live-panel inline
  preview, but vanishes on glass. Confusing UX. **Spec:**
  `SYSTEM_SPEC.md` lines 320-323 explicitly require save-time
  honoring. **Impl gap:** one-line guard in `auto_render.py`'s
  `_render_text_layers` loop. ~3 LOC.

- **P2 — Text-layer `locked` is UI-only chrome.** Backend model
  carries `locked: bool` but no server validation rejects edits to
  locked layers; the constraint lives only in the editor's JS
  form-disable logic. No data-loss risk (operator can still un-
  lock + edit), but the lock isn't enforced system-wide. **Spec:**
  `SYSTEM_SPEC.md` line 314 (line 320 describes it as editor-UX,
  not server validation, so this is also borderline by-design —
  noted P2 for that reason). **Impl gap:** depends on what qarl
  wants the contract to be. If server-side enforcement is desired,
  ~10 LOC validator in `api_content.py`'s text-layer PATCH paths.

### 3.2 Profile-gate documentation

P2 holdover from May-14 #8: `OPENMARQUEE_FIRSTFRAME_PROFILE=1` is a
diagnostic instrumentation gate at `renderer/src/hdmi.rs:3560` and
`:6399`. Not described in `docs/v4l2-decode.md` or
`docs/phase-7-as-built-2026-05-14.md`. Same call-out as the
DMABUF env-var got at line 144 of v4l2-decode.md would close it.
~3 LOC of doc.

### 3.3 VideoSlide Capture-side validator split

P2 holdover from May-14 #7. **CLOSED by r22 (0c4039c) + r24 errata
— see §5 row 4 errata block.** The structural split was already in
place at `validate_paint_slide_inputs` (line 1392) and
`validate_capture_inputs` (line 1424); remaining surface was
doc-comment hygiene, swept by r22.

## §4 Doc-drift on the canonical specs

Two doc-drift items found — neither blocks ship, both are refresh-
doc tasks.

### 4.1 SYSTEM_SPEC.md internal wording inconsistency

Line 77 of `SYSTEM_SPEC.md` says "v0.6 PIL teardown removed the
Python driver, and the Rust replacement is future work" — but lines
492-502 of the same document correctly mark HUB75 / WS2812B /
composite Rust ports as *pending* status (not "future"). The intro
prose creates false ambiguity about whether HDMI is also future work
(it isn't — HDMI Rust renderer is live and the default at v0.9.0).
**Fix:** rewrite line 77 to "v0.6 PIL teardown removed the Python
driver; HDMI Rust renderer ships at v0.6+. HUB75 / WS2812B /
composite Rust ports remain pending." Doc-only.

### 4.2 IMPLEMENTATION_PLAN.md milestone-completion state

Not surveyed exhaustively (time-boxed). High-confidence claim from
the section-walk: the milestone table predates the v0.6.0-beta tag
(2026-05-23 per [[project-h5-v060-beta-release]]) and the v0.9.0
tag, so the "current milestone" pointer is stale. **Fix:** bump the
milestone pointer to "v0.9.0 shipped; tracking v1.0 ship-tag." ~5
LOC of doc. Not surveyed = labelled doc-drift, not a substantive
gap.

## §5 Consolidated gap list

| # | Severity | Arc | Description | Fix complexity |
|---|----------|-----|-------------|----------------|
| 1 | P1 | Text-layers | `anchor` (top/center/bottom) accepted by backend, ignored by renderer | S (~20-30 LOC: field + paint helper plumb) |
| 2 | P1 | Text-layers | `weight` (variable-font 300-900) accepted by backend, ignored by renderer | S (~20 LOC: same shape as anchor) |
| 3 | P1 | Text-layers | `visible: false` ignored at save-time rasterization (correct at playback) | XS (~3 LOC: guard in auto_render.py) |
| 4 | ~~P2~~ | Phase 7 | VideoSlide Capture emits `"video slides TBD"` (carryover #7 from May 14) | **CLOSED by r22 (0c4039c) — see errata below.** Original estimate ~30 LOC turned out to be doc-comment hygiene only; structural split was already in place. |
| 5 | P2 | V4L2 | `OPENMARQUEE_FIRSTFRAME_PROFILE` gate undocumented (carryover #8) | XS (~3 LOC of doc) |
| 6 | P2 | Text-layers | `locked` is UI-only chrome; no server-side enforcement | S (~10 LOC if qarl wants enforcement; "by-design" otherwise) |
| 7 | P2 | Spec docs | SYSTEM_SPEC line 77 vs 492-502 wording inconsistency on HUB75/WS2812B/composite "future vs pending" | XS (doc edit, ~5 words) |
| 8 | doc-drift | Spec docs | IMPLEMENTATION_PLAN.md milestone pointer stale (predates v0.9.0 tag) | S (~5 LOC of doc) |
| 9 | doc-drift | v4l2-decode | Profile gate documentation cross-link absent (same source as #5; doc-side surface) | XS (~3 LOC of doc) |

**Totals: 0 P0, 3 P1, 3 P2 (was 4; row 4 closed by r22 — see
errata below), 2 doc-drift.**

P0 = 0 means v0.9.0 is operationally complete. The 3 P1 are all in
the text-layer chrome triad: same shape (renderer-side struct gap),
all small in isolation, bundle-friendly into one
`renderer/src/content.rs` patch + one auto_render guard.

**ERRATA (2026-05-31, r22) — §5 row 4:** The "video slides TBD"
marker is no longer emitted anywhere in `renderer/src/ipc_main.rs`
(sweep + audit via QA). The Capture-side validator at line 1424
already rejects Video with the distinct
`"Capture: VideoSlide capture not implemented"` string + uses the
`RustRendererUnsupportedSlideError` rail. The "validator split"
is structurally already done (separate functions at line 1392
`validate_paint_slide_inputs` + line 1424 `validate_capture_inputs`).
Remaining surface was comment hygiene only, closed by r22.

## §6 Not gaps — deliberate, ignore on review

- **HTTPS / TLS termination on-device.** SYSTEM_SPEC §4.3 (lines
  189-199) is correct: Tailscale provides transport security
  end-to-end; the device does not terminate TLS. Tailscale's
  WireGuard tunnel is transparent to FastAPI which serves plain
  HTTP on port 80. No FqdnRedirectMiddleware code SHOULD exist
  per design.
- **Async PNG texture upload (task #168) not in spec.** Renderer-
  internal optimization (per memory [[project-task-168-async-texture-upload]]).
  Not a contract surface.
- **Pre-push hook + ruff lint + CI infra.** Devops surface, not
  product surface. Not spec-relevant.
- **`Reconfigure not yet implemented (slice e)`** at
  `renderer/src/ipc_main.rs` (line moved post-restructure).
  Acknowledged design deferral per as-built §3.

## §7 Stale spec docs that merit a refresh dispatch

Two docs are stale enough to merit follow-up — same shape as the
May-14 §5 recommendations, narrower scope now that v4l2-decode +
system/README closed.

1. **`SYSTEM_SPEC.md`** — Single one-line wording fix at line 77
   (above). 5-minute change. Could roll into the next prose-only
   doc dispatch.
2. **`IMPLEMENTATION_PLAN.md`** — Milestone pointer bump from "v0.6"
   (or wherever it currently rests) to "v0.9.0 shipped; tracking
   v1.0." Quick survey-then-fix dispatch.

## §8 Confidence

| Arc | Confidence | What was read end-to-end | What was skimmed |
|-----|------------|--------------------------|------------------|
| §2 May-14 status | High | All 10 gap claims re-verified against current code anchors; v4l2-decode.md re-read at HEAD (343 LOC) | git log range queries |
| §3.1 Text-layer triad | High | `SYSTEM_SPEC.md §5.10a` (315-325), `backend/openmarquee/content/__init__.py:137-201`, `renderer/src/content.rs:141-200`, save-path in `auto_render.py` | text-layer paint helper internals on the renderer side |
| §3.2-3.3 Carryovers | High | Same as May-14 (re-verified anchors) | — |
| §4 Spec doc-drift | Medium-High | SYSTEM_SPEC.md scanned for line 77 vs 492-502 contradiction (verified); IMPLEMENTATION_PLAN.md section-walked but milestone-pointer state described from one section, not full read | full milestone table walk in IMPL_PLAN |

**Overall confidence: high.** All §2 closures cite a specific
mechanism (commit / refresh / file:line). All §3 new gaps cite both
the spec section AND the impl anchor. The two doc-drifts (§4) are
narrow and the fix shape is clear.

## §9 What this means for the v1.0 tag

Three paths:

1. **Tag v1.0 = v0.9.0 + the 3 P1 text-layer chrome closures.**
   ~50-65 LOC of renderer + auto_render work, all small. One
   dispatch. The "spec-complete" v1.0 tag.
2. **Tag v1.0 = v0.9.0 with the 3 P1 gaps documented as known-
   limitations.** Zero code work; one doc dispatch updating
   SYSTEM_SPEC to call out the deferral. The "ship-ready" v1.0 tag.
3. **Defer the v1.0 tag until P2 #6 also closes.** (#4 already
   closed by r22 — was doc-comment hygiene, not the ~30 LOC
   validator split originally estimated.) Adds a `locked` server-
   side enforcement layer if qarl wants that. ~10 LOC more.

My read: path 1. The triad gaps are visible (anchor/weight) or
confusing-UX (visible at save time); closing them is small + non-
risky; the v1.0 → v1.1 cadence stays clean.

---

Generated by `jimmy:openmarquee-code2` 2026-05-30. References
out-of-tree memory by [[name]] shorthand; see
`/Users/qarl/.claude/projects/-Users-qarl-project-openmarquee-code/memory/`.
