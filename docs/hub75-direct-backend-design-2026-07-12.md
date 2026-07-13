# HUB75-direct renderer output backend — design doc

Status: **v1 — DRAFT for admin (Jimmy-openmarquee) review.** 2026-07-13.
Author: Jimmy-openmarquee-code2. Arc: **fallback path** for driving JasonsSign2 (128×96 P8.2 HUB75, parallel=4×chain=3) if the Colorlight card is late or first-light-blocked. Colorlight (code1's arc, PR #69 merged) is the primary path; this fills the long-stubbed `OutputMode::Hub75` variant so the sign can light up over a $35 Pi + HAT if Colorlight slips.

Sibling reference: `docs/colorlight-backend-design-2026-07-12.md` — same design discipline; where this doc says "mirrors Colorlight §X," read that section.

## 1. Goal & scope

Fill the long-stubbed `OutputMode::Hub75` (main.rs:332 — variant declaration; stub eprintln at 2110–2121) with a real HUB75-direct GPIO/HAT driver so openMarquee can drive **1–2 chains** of JasonsSign2's HUB75 panels off a Pi + Adafruit HAT/Bonnet — no receiver card in the loop.

- **Target hardware:** Pi Zero 2 W + Adafruit RGB Matrix HAT/Bonnet, driving standard 32×32 P8.2 HUB75 modules (silkscreen literally reads "HUB75" per qarl's photo). Any HUB75-compatible driver works on them.
- **Sign wiring (fixed — qarl "don't reconfigure the chains"):** `parallel=4 × chain=3` → 12 modules total. In hzeller/Colorlight *card-native* coords that's **96 wide × 128 tall** (`chain·panel_w × parallel·panel_h`) — TRANSPOSED from our 128w × 96h display canvas, exactly the transpose Colorlight §2 documents.
- **Fallback coverage (this arc), in card-native (pre-transpose) coords:**
  - **1-chain fallback (Adafruit HAT single-output, guaranteed):** `parallel=1 × chain=3` = 3 modules = **96 × 32 card-native**.
  - **2-chain stretch (Bonnet or manual GPIO stitching):** `parallel=2 × chain=3` = 6 modules = **96 × 64 card-native**.
  - **3-chain ceiling (Bonnet with all three outputs enabled):** `parallel=3 × chain=3` = 9 modules = **96 × 96 card-native**.
  - **Full 4-chain coverage is out of scope** — hzeller's `rpi-rgb-led-matrix` supports at most 3 parallel chains on a standard Pi HAT/Bonnet, and running 4 would require reconfiguring the wiring (qarl vetoed).
  - Display-orientation (post-transpose): the visible strip corresponds to the top `parallel·32` display-rows across the full 128px display-width. Which physical section of qarl's sign that maps to depends on the physical cabling of the fallback chain; documented at Phase 1.
- **In scope:** pure logic module + config + tests + wire into `OutputMode::Hub75` + Pi bring-up runbook. CLI/env config only, no UI.
- **Out of scope:** UI config; full-sign coverage; Colorlight (that's `OutputMode::Colorlight`, PR #69); WS2812B (still stubbed).
- **Hard requirement (mirrors Colorlight §1):** the driver's pure-logic path must be provably correct on a laptop with **no HAT and no eyeball**. Physical bring-up on qarl's hardware is the last step, not the first.

## 2. Protocol summary

HUB75 is a **GPIO bit-bang protocol**, not a serialized frame protocol like Colorlight. There is no "wire format" to golden-capture with a logic analyzer at this altitude — the correctness story lives entirely at the pixel/geometry/color layer, and the transport is delegated to a mature C++ library. This is a real, deliberate departure from Colorlight (§7, §11).

- **Physical signals** per HUB75 spec: two RGB pixel pairs (R1,G1,B1 for top half, R2,G2,B2 for bottom half of a 32-row module), 5 address lines (A/B/C/D/E for 1/32-scan modules; 4 lines for 1/16-scan), CLK, LAT/STB, OE (output enable). 13 GPIO pins per chain.
- **Grayscale** via **binary coded modulation (BCM)** — for `pwm_bits=N` the driver emits N bit-planes per frame, each held OE-low for 2^i time units. Refresh rate = f(pwm_bits, chain_length, panel_rows, CPU clock). Standard tunable.
- **Cadence target:** 30 Hz per renderer spec §5. Reachable at 1-chain × pwm_bits=8 on Pi Zero 2 W (4-core A53 @ 1 GHz) per hzeller's published benchmarks. Full 3-chain × pwm_bits=11 would need a Pi 3+, out of scope.
- **Timing sensitivity:** BCM plane emission is **realtime-critical**. Missed deadlines = visible flicker/banding. hzeller's lib pins to an isolated CPU + uses `SCHED_FIFO`; we adopt that verbatim.

**Authoritative reference:** [hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) C++ library. This is *the* canonical HUB75-on-Pi driver — every Rust binding, every derivative (Adafruit's Python wrapper included), calls into it. The library is battle-tested on Pi Zero 2 W. **This design treats hzeller's lib as the transport black-box; correctness at the pixel-in-→pixel-on-panel level is delegated to it.**

## 3. Where it fits — FILL the existing `OutputMode::Hub75` stub

Colorlight's design doc (§3) proposes `OutputMode::Colorlight` as a **future peer** landing in Phase 2 (per its §9); today only `colorlight_logic.rs` is compiled (`#[allow(dead_code)]`, see main.rs:43–45 comment). The Hub75 variant already exists (main.rs:319–332 — `OutputMode` enum with `Hub75` at line 332; variant docstring `"v1-spec-delta #11 (slice e) -- 64x64 HUB75 RGB matrix panel reach gate. Spec §5: keep reachable; panel-write path stubbed for v1."`; runtime stub at 2110–2121).

**Filling this stub is the intended semantics of the variant** — no new enum variant needed for Hub75.

Once Colorlight lands its peer variant in Phase 2, the two form a clean split: `Hub75` = GPIO/HAT direct-drive; `Colorlight` = L2 Ethernet to a receiver card. Distinct transports / hardware / wiring / perf envelopes.

## 4. Module architecture

Split mirrors `hdmi.rs`/`hdmi_logic.rs` and Colorlight §4:

- **`hub75_logic.rs` — pure, host-testable on macOS.** No GPIO, no `#[cfg]` gates on the module itself.
  - `Hub75Config` struct (all env-driven, nothing hardcoded).
  - Config **validation** (chain × parallel ≤ HAT limits; panel dims; pwm_bits ≤ 11; letterbox math).
  - `apply_lut` / `apply_color_order` — pre-processing the framebuffer before handing to transport. Same LUT / color-order framework as Colorlight so a shared future refactor stays open.
  - `letterbox_and_scale` — canvas (128×96 native) → visible-region on the 1-or-2-chain subset. Pure geometry.
  - **This module is thinner than `colorlight_logic.rs`** (which is a full byte-serializer for L2 frames) precisely because hzeller's library owns the GPIO bit-banging. Honest — we don't invent a serializer we don't need to test.

- **`hub75.rs` — Linux-only transport.** `#[cfg(target_os="linux")]`.
  - Thin FFI shim over hzeller's C++ lib. **Decision below.**
  - Owns the `RGBMatrix` handle, applies `Hub75Config`, receives a pre-processed `Rgb888Frame` per tick, hands it to the lib (`SetImage` or per-pixel).
  - **On non-Linux the crate is a no-op stub** so `cargo test` on macOS still runs the pure logic. Same shape as `hdmi.rs`.

- **Config (env, matching renderer convention — `OPENMARQUEE_HUB75_*`):**
  - `HAT` = `adafruit-hat` | `adafruit-hat-pwm` | `regular` (GPIO pinmap, must match physical HAT).
  - `PANEL_ROWS` = 32 (P8.2 modules).
  - `PANEL_COLS` = 32.
  - `CHAIN` = 3 (modules per chain — matches qarl's fixed wiring).
  - `PARALLEL` = 1 (fallback default; 2 with Bonnet).
  - `PWM_BITS` = 8 (Pi Zero 2 W ceiling; 11 = higher color depth but insufficient CPU).
  - `PWM_LSB_NSEC` = 130 (hzeller default; tunable if flicker).
  - `COLOR_ORDER` = rgb (matches panels; per-panel override if needed).
  - `BRIGHTNESS` = 0–100 (%). hzeller lib scales.
  - `GPIO_SLOWDOWN` = 4 (Pi Zero 2 W recommendation from hzeller README).
  - `LIMIT_REFRESH_HZ` = 60 (safety cap so BCM doesn't runaway CPU).

- **Crate decision — DEFAULT PICK: `rpi-led-matrix` crate (Rust binding to hzeller C++ lib).**
  - Alternatives considered:
    - **Bare-metal ioctl / DMA** — 3+ weeks of BCM2835 GPIO/DMA plumbing. Out of scope for a *fallback*.
    - **Pure-Rust `rgb-matrix` crate** — young, no Pi Zero 2 W validation, no Adafruit-HAT preset. Uncertain.
    - **hzeller C++ lib via `bindgen` + hand-rolled FFI** — same call surface as `rpi-led-matrix` but we own the maintenance. Chose the crate to keep the shim thin.
  - **Trade-off flagged for admin:** the `rpi-led-matrix` crate adds a C++ dependency + `librgbmatrix.so` linkage. This is the exact opposite of Colorlight's "tight deps, libc/nix only" ethos. **Justification:** HUB75 bit-banging is genuinely hard + realtime-critical + already solved; reinventing it for a fallback is a work-avoidance trap. If admin objects, fallback plan = ship it C++-lib-only inside the `#[cfg(target_os="linux")]` block (invisible to macOS `cargo test`), and treat the dep as accepted-technical-debt with a "revisit if Colorlight is late enough to matter" note.

- **Privileges:** hzeller's lib needs `CAP_SYS_NICE` (for `SCHED_FIFO`) or root. On the deployed Pi we run the renderer as root or `setcap cap_sys_nice+ep`. Documented in the runbook.

## 5. Renderer integration point

Mirrors Colorlight §5 with two tweaks:

- **Frame tap:** same `glReadPixels` after GL compositing → RGB888. Sign-native FBO size = card-native geometry = `CHAIN·PANEL_W` wide × `PARALLEL·PANEL_H` tall. For the 1-chain fallback (`parallel=1, chain=3, panel=32×32`) that's **96×32**; readback is a few KB, trivial.
- **Headless compositing:** Pattern A from Colorlight's EGL spike (GBM+EGL minus DRM modeset/page-flip) applies verbatim. Colorlight already validated this path — Hub75 inherits it for free.
- **Geometry mismatch (content ≠ visible-region):** letterbox / crop the 128×96 display canvas down to the card-native (post-transpose) visible strip. Configurable "which portion of the canvas to show" so operators can pick between "full canvas letterboxed into 96×32" and "show a native-scale 96×32 slice." The transpose (display 128w×96h → card 96w×128t) is applied inside `hub75_logic` before the readback lands on the GPIO side.

## 6. Hardware-less test plan (2 layers) — honest scoping

**Fewer layers than Colorlight (§6, 3 layers) because HUB75 has no wire-format to conformance-test.** The 3-layer Colorlight plan exists precisely because we're byte-emulating a proprietary L2 protocol. Here the transport is a mature library — we test **around** it, not through it.

All layers run in `cargo test`, no HAT, no display, no qarl.

- **Layer 1 — pure-logic conformance (host).** For each behavior in `hub75_logic.rs`:
  - **Config validation:** `Hub75Config::validate()` catches (chain>16, parallel>3, pwm_bits>11, invalid HAT enum, zero dims, non-multiple-of-panel canvas). Panics never; errors always.
  - **Color-order remap:** RGB→BGR etc., byte-exact. Pins the same channel-order bug family Colorlight §6 pins.
  - **Gamma/brightness LUT:** identity + per-channel LUT round-trip. Ends (0, 255) explicit.
  - **Letterbox math:** canvas → visible-region, per HAT config. Bit-exact expected framebuffers.
  - **Geometry validation:** `SerializeError`-equivalent (never panic) on bad dims. Mirrors Colorlight §11.C `dimension_mismatch_is_an_error_not_a_panic`.
- **Layer 2 — cadence (host).** frame-emit rate, `LIMIT_REFRESH_HZ` cap, `PWM_BITS` → theoretical refresh estimate stays in the "will not flicker on Pi Zero 2 W" envelope. Static analysis, not live.

**What we deliberately DO NOT test at Phase 0:**
- The actual GPIO waveform (hzeller's lib owns this; qarl's eyes prove it at Phase 1).
- The refresh rate on real HW (Phase 1).
- Cross-panel color balance (Phase 1, qarl-blessed if he cares).

**Golden-capture equivalent:** none. There is no `.pcap` to bit-compare against. The "golden" for HUB75 correctness is **hzeller's library behaving correctly** — we don't re-litigate that. If qarl's panels show wrong colors at Phase 1, that's a config/wiring issue, not a serializer bug (because there IS no serializer).

## 7. Phase-1 bring-up sequence (qarl + admin owned)

- **Prereqs:** Pi Zero 2 W with Adafruit RGB Matrix HAT (single-chain) or Bonnet (up to 2 usable chains). One chain of qarl's sign (3 modules = 128×32 px) wired to the HAT.
- **First-light procedure:**
  1. Deploy renderer built with `--features hub75-direct`.
  2. Boot with `OPENMARQUEE_HUB75_HAT=adafruit-hat OPENMARQUEE_HUB75_PARALLEL=1 OPENMARQUEE_HUB75_CHAIN=3 OPENMARQUEE_HUB75_PWM_BITS=8`.
  3. Play a **known fiducial** — same wordmark asset Colorlight §11.A uses ("openMarquee →" with arrow — the arrow is the rotation/mirror tell). Reuse, don't reinvent.
  4. qarl's eyes: legible? no mirror/gap/misalignment on the 3-module chain? colors sane?
  5. If wrong colors → flip `OPENMARQUEE_HUB75_COLOR_ORDER=bgr` etc. until pure R/G/B slides land right.
- **Fiducial reuse (admin ask #A.1 from Colorlight review):** deliberate. Cross-backend fiducial consistency is a feature — same eye-test, same pass/fail criterion, no per-backend goldens to babysit.

## 8. Edge cases (design positions)

- **HAT absent / GPIO permission denied:** log + no-op transport (don't wedge renderer); mirrors Colorlight §8 "must never wedge the renderer".
- **Under-CPU (Pi Zero 2 W spinning):** hzeller's lib caps CPU via `LIMIT_REFRESH_HZ`; we set 60 Hz cap. If SCHED_FIFO fails to activate (no CAP_SYS_NICE), log a WARNING but continue (degraded refresh).
- **Chain length mismatch (config says 3, physical has 2):** invisible — the extra chain slot renders black. Not detectable from software.
- **All-black / idle:** same as Colorlight — send explicit black frames.
- **Config-vs-canvas geometry mismatch:** `SerializeError`-equivalent from `hub75_logic::validate` before any GPIO touches happen.

## 9. Phased plan

- **Phase 0 (now, no HAT):** this doc → admin review; `hub75_logic.rs` pure module + Layer-1 + Layer-2 host tests + `hub75.rs` `#[cfg]` skeleton + `main.rs` `OutputMode::Hub75` wired to the skeleton. macOS-buildable; Pi-buildable behind `--features hub75-direct` (opt-in until Phase 1 validated).
- **Phase 1 (HAT + one chain of qarl's sign):** cross-build for aarch64; deploy; first-light with the wordmark fiducial; per-panel config tuning; qarl-eyes blessing. If Colorlight has succeeded by this point, Phase 1 becomes a backlogged proof-of-life exercise; if Colorlight is delayed, Phase 1 becomes the shipping fallback.
- **Phase 2 (backlog):** stretch to 2 chains with Bonnet if useful; longer-term 3-chain / partial-sign coverage if qarl wants a permanent $35-tier BOM option (per DESIGN_BRIEF).

## 10. Risks / open questions (for admin review)

1. **C++ dep on `rpi-led-matrix` / `librgbmatrix`** — the top ethos-collision with the rest of the renderer. Live question for admin (§4 trade-off).
2. **CPU headroom on Pi Zero 2 W** — 1-chain × pwm_bits=8 is comfortable per hzeller's docs. 2-chain × pwm_bits=8 is borderline. Anything more = pwm_bits=6 or a Pi 3. Numbers get pinned at Phase 1.
3. **Coexistence with the running renderer's other GL/audio work** — `SCHED_FIFO` for the HUB75 thread contends with the paint thread. hzeller pins the driver thread to a specific core; on 4-core Pi Zero 2 W that's core 3. Blendr / cadence-worker changes recently added a cadence thread — those all need to coexist. **Not proven at Phase 0.**
4. **Adafruit HAT-vs-Bonnet feature detection** — must match `OPENMARQUEE_HUB75_HAT` to the physical HAT or timing goes wrong. No auto-detect; documented in runbook as "operator supplies."
5. **Long-term duplication with Colorlight** — LUT, color-order, letterbox logic are duplicated across `hub75_logic.rs` and `colorlight_logic.rs`. Phase 0 keeps them separate (dupe first, extract later per YAGNI). Flagged for later shared-module refactor if both backends ship.
6. **No wire-level correctness test** — this is the honest fact. If hzeller's lib ever regresses, our tests don't catch it. Acceptable for a fallback; if this becomes a primary backend, we owe a "known-good frame → visual diff against reference photo" test.

## 11. Notes vs Colorlight

- **Backend altitude:** Colorlight = protocol layer (byte-exact serialization); Hub75 = configuration layer (validate + hand to library). This asymmetry is the honest truth of the two hardware targets; the doc structure mirror is deliberate, but §6 is honestly thinner.
- **Fiducial reuse:** intentional. Both backends bless the same visual asset; qarl's eye-test criteria stay stable across backends.
- **Enum status:** Colorlight is a new peer; Hub75 fills a pre-existing stub. Both semantically clean.
- **Scope humility:** Hub75 is explicitly a **fallback** proof-of-life (1–2 chains of qarl's sign), not a full-sign replacement for Colorlight. Full-sign HUB75 direct-drive would take a Pi 3+ and reconfigured wiring — outside the deal.

## 12. Sacred-review pass (2026-07-13)

A first sacred-review subagent found 6 items against the initial draft; the 3 verifiable-against-code items were folded in before this doc was committed:

- **[BLOCK] Geometry math wrong (fixed):** original §1/§5/§7 said "1 chain = 128×32 = 4 modules" — off-by-axis. Fixed: 1 chain = `parallel=1 × chain=3` = 3 modules = **96×32 card-native** (transposed from 128w×96h display canvas per Colorlight §2). Cascaded through §1, §5, §7.
- **[MAJOR] Wrong `main.rs` line numbers (fixed):** original cited `main.rs:311` for the `Hub75` variant; the actual line is **332** (enum starts at 319, variant docstring 329–331, stub eprintln 2110–2121). Fixed in §1, §3.
- **[MAJOR] Colorlight-as-landed-peer (fixed):** original phrased `OutputMode::Colorlight` as already a variant; per main.rs:43–45 it's Phase-2 gated and today only `colorlight_logic.rs` is compiled `#[allow(dead_code)]`. Fixed in §3 to future-tense.

Items deliberately deferred (surfaced for admin's call at Phase 1, not blocking Phase 0):
- **[MINOR] HW assertions uncited:** the Pi Zero 2 W benchmark claims (§2), `PWM_LSB_NSEC=130`, `GPIO_SLOWDOWN=4` (§4), Bonnet 2-vs-3-chain (§7), core-3 pinning (§10) are pulled from hzeller README/community docs but not link-cited here. **Verify at Phase 1** on the physical HAT + Pi Zero 2 W — Bonnet in particular officially supports 3 outputs (with "Active-3" jumper), not 2, so the "up to 2" language should soften to "1 out of the box, up to 3 with Active-3 jumper" once verified.
- **[MINOR] `Hub75Config::validate()` bounds unjustified:** §6 caps `chain > 16` arbitrarily. Tie to a refresh-Hz sanity check (or hzeller's own maximum) rather than a hardcoded 16 when implementing.
- **[NIT] Framebuffer-aliasing contract not pinned:** Colorlight §11.C pins `serializer_does_not_alias_input_snapshot`. Hub75's pre-processing stage (`apply_lut` / `apply_color_order`) takes `&Rgb888Frame` too — mirror that test contract in `hub75_logic` when implementing.
