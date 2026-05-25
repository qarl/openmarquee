---
date: 2026-05-25
type: scope
surface: backend-rust
---

# Backend Python + Rust renderer improvement targets — 2026-05-25 survey

QA-pivot deliverable per round-8 scoping dispatch (parallel to code2's UI
survey 822d7d2). 6 candidate functions across `backend/openmarquee/` and
`renderer/src/` for the code-quality loop's next several rounds, sorted
by value. Skip-list per the dispatch:

- scheduled_fetch_items refactor (1e6e8d7)
- atomic_write_text/bytes dedupe (6a17c5c)
- _coerce_to_schedule migration coverage + _resolve_legacy_name hoist
- Schedule extras forward-compat fix + MIGRATION_HANDLED_TOP_LEVEL
- list_in_playlist_order split → list_for_playback / list_full_library
  (7a34813)
- render_auto_text_for_layer dispatch table (e2e09d5)
- LruMap::insert PathBuf::clone skip on existing-key (c852746)
- summarize_samples percentile off-by-one + PhaseStats (17824b2)
- mem.rs parse_status/parse_meminfo → parse_kv_kb_lines (c326f25)

Plus security-audit work, blinds parity, all 9 audit findings — assume
those surfaces are recently-touched and skip.

## Methodology

Three-pass survey across `backend/openmarquee/` (16104 LOC) and
`renderer/src/` (38548 LOC):

1. **Size + test-coverage scan** — `wc -l` sorted modules; cross-
   referenced module names against `backend/tests/test_<name>.py`
   filename map. Surfaced auth_middleware.py (345 LOC) as the largest
   security-relevant module with no direct sibling test.
2. **Smell-pattern greps** — `model_copy`, `time.sleep` in async-
   adjacent contexts, broad `except Exception` patterns, off-by-one
   shapes (`(x * pct) / 100`), `\.clone()` on hot paths,
   `unwrap()`/`expect()` outside test gates.
3. **Near-duplicate scan** — function-name patterns (`parse_*`,
   `find_*_slide`, `_coerce_to_*`) across files for DRY candidates.
   Plus Rust-specific: enum-name → variant `match` dispatchers.

Time-boxed at 90 min total. Targets ranked by leverage (correctness
beats DRY beats coverage beats perf beats API hygiene).

## Targets (sorted by value — highest first)

### 1. `_coerce_to_collection` migration drops forward-compat extras

- **Type:** **correctness bug** (likely; pattern-identical to a recent
  schedule.py fix that DID have the bug)
- **File:line:** `backend/openmarquee/playlist.py:432-482`
- **Sketch:** v4 fast-path (line 425-430) routes through
  `PlaylistCollection.model_validate(data)` which preserves unknown
  top-level fields via `extra="allow"` + Pydantic's model_extra. But
  the v1/v2/v3 migration paths construct `PlaylistCollection(playlists=
  migrated)` explicitly with kwargs only — Pydantic's extras
  preservation only applies via `model_validate`, NOT via direct
  `__init__`. Any forward-compat top-level field present on a v3 file
  (or that a future v5 might carry) gets silently dropped during the
  v3→v4 migration. **This is bug-grade and identical-shape to the bug
  code2 fixed in `_coerce_to_schedule` via `MIGRATION_HANDLED_TOP_
  LEVEL = frozenset(Schedule.model_fields.keys()) | {"default_playlist_
  name"}` + `extras = {k: v for k, v in data.items() if k not in
  MIGRATION_HANDLED_TOP_LEVEL}` + `Schedule(**extras, ...explicit
  kwargs)`.** Apply the same pattern here.
- **Realistic impact:** Low today (no current forward-compat field is
  on the wire for playlists), but the SAME class of bug DID surface
  in schedule and was deemed worth fixing. Fix-now is cheap and
  blocks an entire bug-class for the playlist surface.
- **Fix shape:** Define `MIGRATION_HANDLED_TOP_LEVEL = frozenset(
  PlaylistCollection.model_fields.keys()) | {"item_ids", "playlists"}`
  near the function; carve out + splat extras in the v1/v2/v3 return
  constructions; add a regression test mirroring `test_v1_migration_
  preserves_unknown_top_level_fields` in test_schedule.py.
- **Effort:** **S** (~30 lines + 1 test; closely mirrors the schedule
  fix's commit shape).
- **Sister context:** code2's `Schedule` extras fix (cherry-picked
  earlier this session); the schedule's `MIGRATION_HANDLED_TOP_LEVEL`
  derivation pattern is the template.
- **Risk:** low. Pure additive (extras now flow through where they
  were dropped); the v1/v2/v3 file shapes don't have any extras in
  practice today so the test surface is the regression-lock for
  future-v5 forward-compat.

### 2. `auth_middleware.py` private helpers have no sibling test coverage

- **Type:** **missing test coverage on a security-relevant surface**
- **File:line:** `backend/openmarquee/auth_middleware.py:240-288` —
  four pure-function private helpers (`_is_whitelisted`,
  `_is_media_route`, `_token_from_query`, `_bearer_from_headers`) with
  ZERO direct unit-test coverage. (Their behavior is *indirectly*
  exercised by `test_auth.py` + `test_api_settings.py` +
  `test_csp_middleware.py` end-to-end, but no dedicated test exists
  that pins each helper's contract on its own.)
- **Sketch:** These helpers gate which paths bypass auth + which
  routes get the public-media-read carve-out. Direct tests would lock
  contracts like:
  - `_is_whitelisted("/healthz")` is True; `"/healthz/something"` is
    False; case sensitivity.
  - `_is_media_route("/api/content/<uuid>/asset")` is True; the
    canonical media subpaths (`asset.png`, `asset.mp4`, etc).
  - `_token_from_query(b"token=abc&other=def")` returns `"abc"`;
    handles malformed UTF-8 (line 264 has `UnicodeDecodeError` catch
    but no test).
  - `_bearer_from_headers(...)` handles missing header / wrong scheme
    / mixed-case "Bearer"/"bearer".
- **Why it matters:** A regression to one of these helpers (e.g.,
  silently widening `_is_whitelisted` during a refactor) opens an
  auth bypass that the end-to-end tests might not catch if they
  don't exercise the precise added-path.
- **Effort:** **S** (one new `test_auth_middleware.py` file; ~8-12
  small parametrized cases).
- **Sister context:** `_rate_limit.py` got `test_rate_limit.py` in
  Bundle B2 even though its behavior was indirectly exercised by
  `test_auth.py` integration tests — same precedent: security-
  relevant pure helpers earn their own coverage file.
- **Risk:** low (additive tests only; no production code change).

### 3. `playback.rs::advance` per-frame String alloc via TransitionContext clone

- **Type:** **perf** (hot path; heap alloc per advance call)
- **File:line:** `renderer/src/playback.rs:157` (`self.pending.clone()`)
  + `:180` (`self.current.clone()`)
- **Sketch:** `pub fn advance(&mut self, t_ms: u64) -> AdvanceCommand`
  is called once per frame from the IPC sidecar's tick loop.
  `TransitionContext` (defined at playback.rs:40) carries a
  `pub kind: String` field — so `self.pending.clone()` on every
  advance during a transition is a **heap allocation per frame for
  the kind string** (typically 3-7 chars: "cut" / "fade" / "wipe" /
  "blinds" / etc). At 30 fps during a 500ms transition window =
  ~15 string allocs per transition.
- **Realistic impact:** Small absolute number (~60 bytes per alloc on
  64-bit; the allocator's free-list will handle it), but for an
  embedded device on a Pi Zero 2 W where every alloc costs and where
  the renderer is otherwise meticulously zero-alloc, this stands out.
  Sister of the `PathBuf::clone` we just eliminated from LruMap
  (c852746).
- **Fix shape:** Borrow `self.pending` and `self.current` via
  `if let Some(transition) = &self.pending` — then read fields
  through the borrow. The promote-on-complete branch (line 161,
  `self.current = Some(transition.to_slide.clone())`) DOES need
  `transition.to_slide.clone()` (SlideContext is small, all primitives,
  cheap), but only fires once per transition not per frame. The
  PaintTransition + PaintSlide branches need only field reads, no
  ownership. AdvanceCommand::PaintTransition carries `kind: String`
  too (kind enum: see #5 below for a related dedupe target), so the
  String alloc CAN move to that path's return — but ideally
  AdvanceCommand should carry a `kind: &str` or a borrowed Cow if
  it's read-only.
- **Effort:** **S-M** (the borrow refactor of advance itself is small;
  the question is whether to widen scope to make
  AdvanceCommand::PaintTransition carry a borrowed kind, which
  forces a lifetime on AdvanceCommand. Recommend scoping to JUST the
  advance() body — keep AdvanceCommand owning the String for now.)
- **Sister context:** LruMap::insert PathBuf::clone elimination
  (c852746); same "borrow what you don't need to own" shape.
- **Risk:** low. Borrow-checker-enforced; tests at `playback.rs`'s
  `#[cfg(test)] mod tests` already cover the state-machine.

### 4. `find_text_slide` + `find_image_slide` + `find_video_slide` DRY

- **Type:** **DRY**
- **File:line:** `renderer/src/content.rs:320-337` (text), `:376-393`
  (image), `:445-462` (video)
- **Sketch:** Three near-identical loaders. Each does the same
  4-step ladder: read item.json bytes → parse ItemEnvelope →
  check kind matches → deserialize the inner Value as the target
  type. Differences:
  - Type parameter: `TextSlide` vs `ImageSlide` vs `VideoSlide`
  - Kind match: `"text_slide"` vs `"image"|"web"` vs `"video"`
  - `with_context` label suffix
  
  Extract `fn find_slide<T: serde::de::DeserializeOwned>(content_root:
  &Path, item_id: Uuid, accept_kinds: &[&str], kind_label: &str) ->
  Result<Option<T>>`. Each public function becomes a 1-line delegate.
  ~30 LOC saved.
- **Effort:** **S** (generic helper + 3 call-site rewrites).
- **Sister context:** Direct sibling of mem.rs's `parse_kv_kb_lines`
  const-generic dedupe (c326f25); same "extract the shared ladder,
  keep public API unchanged" shape.
- **Risk:** low. Each public function's signature stays unchanged;
  existing tests in `renderer/src/content.rs`'s `#[cfg(test)]`
  block + any integration test consuming these still works
  without modification.

### 5. Enum-name → variant `match` parsers in `hdmi_logic.rs` (DRY)

- **Type:** **DRY** (moderate value)
- **File:line:** `renderer/src/hdmi_logic.rs:3668` (parse_pattern_kind),
  `:3950` (parse_blend_mode), `:4515` (parse_motion_kind),
  `:4856` (parse_h_align). Four enum-name-to-variant `match` blocks
  with the same shape: `match s { "name1" => Variant1, "name2" =>
  Variant2, ..., _ => Default | None }`.
- **Sketch:** Could share via a macro (`enum_str_parser!(EnumKind {
  "name1" => Variant1, ...})`) or a static `phf::Map`-style table.
  Each parser is short (5-15 LOC) — the value is the **uniform
  default behavior** (warn-and-fall + tolerant unknown handling).
  But unlike the parse_kv_kb_lines case, the variation between these
  is structural (Option<T> vs T-with-default), so the macro-or-table
  needs to handle both shapes. Mild duplication overall; the dispatch
  could reasonably skip this with rationale.
- **Effort:** **M** (declarative macro is the cleanest landing; needs
  unit test for both Option<T> and T-with-default shapes).
- **Sister context:** Same shape as Python's `_DEFAULT_FORMAT` table
  + auto_render dispatch (e2e09d5). The Python side preferred a
  dispatch table over if/elif; the Rust side could too.
- **Risk:** low-medium. Macros add cognitive load — if the team
  finds the explicit match blocks more readable, skip with a
  rationale doc.

### 6. `_coerce_to_collection` test coverage

- **Type:** **missing test coverage**
- **File:line:** `backend/openmarquee/playlist.py:403-482` — the
  function exists and is heavily exercised end-to-end via test_
  playlist.py + test_seed.py round-trips, but **no test directly
  exercises the v1/v2/v3 migration paths in isolation** the way
  test_schedule.py covers `_coerce_to_schedule`. The schedule
  surface has `test_v1_migration_preserves_unknown_top_level_fields`
  + `test_v1_migration_explicit_kwargs_not_shadowed_by_extras_splat`
  + 4-5 other migration-path tests; the playlist surface has none
  with that focus.
- **Sketch:** Add ~5 tests in test_playlist.py covering: v1
  (`item_ids`-only) → expected v4 collection; v2 (dict by name, slide
  transitions) → v4; v3 (dict by name, item transitions) → v4; v4
  already-current → pass-through unchanged; v4 with unknown top-level
  field round-trips (bundles with Target #1's fix).
- **Effort:** **S** (~50 LOC of tests; ships as part of Target #1's
  bundle or as its own commit).
- **Sister context:** Code2's `_coerce_to_schedule` coverage work.
- **Risk:** low (additive tests only).
- **Bundle suggestion:** ship Target #1 + Target #6 together (fix +
  regression-lock); same shape as the schedule extras fix.

## Items ruled out

- **`time.sleep` calls in `wifi_station.py:313/419` + `web_render.py:
  410`** — flagged by the "blocking-IO in async" smell-grep, but read
  shows these are inside sync functions called via `apply_in_background`
  (threading) or os.killpg loops, NOT async paths. Not the leak shape.
- **`auth.py:166/314` broad `except Exception`** — documented
  fail-closed-on-corrupt-hash patterns; `log.warning(... exc_info=True)`
  preserves the backtrace for forensics. Correct, not a bug.
- **`auto_render.py:126/206` broad `except Exception`** — image-bg
  load fallback; each catch logs via `log.exception` + falls to the
  solid-color rendering. Operator-invisible perf-stat path is fine.
- **`api_system.py` 8+ broad excepts** — these are
  try-this-probe-then-fall-through patterns (fbset / sysfs / iw /
  airport / /proc/device-tree/model probes). Each catch logs +
  returns a sentinel (source="none"). Correct shape for hardware
  probing.
- **Renderer `.unwrap()` density** — sampled hdmi.rs (13049 LOC) has
  ONE bare `.unwrap()`; the bulk live in `#[cfg(test)]` blocks or
  on values where the invariant is obviously preserved (e.g.
  `*s.last().unwrap()` after non-empty guard in
  summarize_samples). Not the brittleness shape.
- **`content.rs::default_*` functions** (20+ one-liners) — these are
  serde-default callables; each is a single-line literal + a clear
  type signature. Dedup via macro would obscure rather than clarify.
- **`main.rs` 26 `.clone()` calls** — sampled spot reads show most
  are at startup / CLI-parse boundary where ownership-transfer is
  the design intent, not the hot path. Not the LruMap shape.
- **`hdmi.rs` 13049 LOC** — too large to scope-survey in 90min; worth
  its own dedicated audit round when QA wants the deeper sweep.
- **`seed.py` 1683 LOC** — the largest backend module, but mostly
  static data tables (color palettes, demo slide content). The
  generator functions are well-tested in test_seed.py. No obvious
  smell-grep hits.

## Recommended ordering

1. **Targets #1 + #6 bundled** — correctness fix + regression-lock
   for the playlist migration extras drop. Highest leverage and the
   pattern is already proven (code2's schedule fix). Single commit.
2. **Target #2** — auth_middleware private-helper test coverage.
   Security-relevant, S effort, pure additive.
3. **Target #3** — playback.rs::advance borrow refactor. S effort,
   borrow-checker enforced, sister of the LruMap fix's pattern.
4. **Target #4** — content.rs `find_*_slide` DRY. S effort, sister
   of the mem.rs parse dedupe.
5. **Target #5** — hdmi_logic.rs enum-parser macro. Skip with
   rationale if the team finds the explicit matches more readable.
