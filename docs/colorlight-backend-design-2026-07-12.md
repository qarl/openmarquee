# Colorlight 5A-75B renderer output backend — design doc

Status: **v2 — reviewed + green-lit for Phase 0** (admin adjudications + QA review folded; see §11). 2026-07-12.
Author: Jimmy-openmarquee-code. Arc: repurpose the ThinkSIGN outdoor LED sign as an openMarquee target.
Phase-0 code landed alongside this revision: `renderer/src/colorlight_logic.rs` (pure serializer + 12 host tests, incl. the two synthetic catches in §11.B; fmt+clippy clean).

## 1. Goal & scope

Add a native Colorlight 5A-75B output backend to the Rust renderer so openMarquee drives qarl's sign directly over raw Ethernet — no Falcon Player middleman.

- **Target sign:** 128×96 px, P8.2 HUB75, 12× 32×32 modules, wired **parallel=4 × chain=3** into 4 of the card's 8 HUB75 outputs. Fed over Ethernet from a Pi.
- **In scope (this arc):** the serializer + transport + a full **hardware-less test suite** (the load-bearing requirement — see §6). CLI/env config only.
- **Out of scope (follow-ups):** UI config of the Colorlight target; HUB75-*direct* (GPIO/HAT) backend; receiver auto-discovery/negotiation (passive-listen card, not needed).
- **Hard requirement (qarl, via admin):** the driver must be provably correct on a laptop with **no card and no eyeball**. qarl's eyes are the *last* step (pixel-on-glass sanity), not the first.

## 2. Protocol summary

Extracted byte-exact from **FPP** `src/channeloutput/ColorLight-5a-75.cpp/.h` (authoritative sender) + **hkubota**'s RE blog (independent hex dumps) + the **`colorlight` v0.1.0** Rust crate (asamonik, a faithful FPP subset). q3k/chubby75 is *hardware* RE only — no protocol doc, no pcaps.

- **Pure raw Layer-2.** No IP/UDP. `AF_PACKET`/`SOCK_RAW`. Dest MAC **`11:22:33:44:55:66`** (fixed), src MAC **`22:22:33:44:55:66`** (fixed). EtherType field = `opcode<<8 | data[0]` (this reconciles the "two EtherType families" — same bytes, two views).
- **Per-frame packet order:** **brightness `0x0A` → pixel rows `0x55` (row 0…N-1 ascending) → display/latch `0x01`**. The `0x01` frame latches the streamed buffer onto the LEDs.
- **Pixel-row packet `0x55`** (8-byte header, absolute wire offsets):

  | off | field | value |
  |----|----|----|
  | 12 | opcode | `0x55` |
  | 13–14 | row # (big-endian; 13 is also the EtherType low byte) | `0..127` |
  | 15–16 | pixel **offset** (in pixels, BE) | 0 for single-packet rows |
  | 17–18 | pixel **count** (in pixels, BE) | 96 |
  | 19 | magic | `0x08` |
  | 20 | magic | `0x88` |
  | 21… | payload, **3 bytes/pixel** | RGB **or** BGR (§10 risk) |

  ≤497 px/packet (MTU); my 96-px rows are a single packet each. **Rows are emitted ascending with no bank interleaving** — the card does the HUB75 1/16-scan internally.
- **Brightness `0x0A`** (77 B): off 13/14/15 = R/G/B brightness `0..255` linear (FPP scales a 0–100% config ×2.55), off 16 = `0xFF`. *Firmware-sensitive — may no-op on old FW.*
- **Display/latch `0x01`** (112 B): the commit. off 13 = `0x07` (from a PC sender), brightness fields at 35/38/39/40.
- **Discovery `0x07`/reply `0x08`:** optional; skip if the card is known.
- **Cadence:** 20 Hz default / ~60 Hz reliable; FPP budgets ≤22 ms/frame; **requires a 1000 Mbps link** (FPP refuses <1000); no inter-packet gap needed (`sendmmsg`); a dropped `0x55` leaves that row **holding its previous content** (not black); dark only on link loss.

### Geometry (128×96, 32×32 modules, parallel=4, chain=3)
FPP: `m_rows = outputs·panelH`, `rowWidth = longestChain·panelW`. For us:
- **128 row packets** (indices 0–127); `output = row/32`, `y_in_panel = row%32` (rows 0–31→output0 … 96–127→output3).
- **96 px per row** = the 3-module chain end-to-end.
- ⚠️ **In the card's native coords the framebuffer is 96 wide × 128 tall — TRANSPOSED from our 128w×96h canvas.** Which physical axis is "width" vs "row-index" is **cabling-dependent** (per-module orientation + chain direction). FPP also **reverses chain order by default** (`chain = longestChain-1 - panel.chain`). The exact remap can only be pinned against qarl's actual wiring (see §7).

## 3. Where it fits — OutputMode: **PEER** (recommendation)

The renderer's `OutputMode` (main.rs:313) is `Hdmi | Mock | Hub75 | Ws2812b`. `Hub75` is explicitly the **GPIO-direct** driver ("64×64 RGB matrix at 30 fps over the GPIO HUB75 driver", main.rs:2110-2112) — panel-write stubbed for v1.

**Add `OutputMode::Colorlight` as a peer**, leaving the GPIO-direct `Hub75` stub as its own follow-up. *(APPROVED — admin, 2026-07-12.)*
- Reasoning: `Hub75` (GPIO bit-bang off a HAT) and Colorlight (L2 Ethernet → a receiver card that itself drives HUB75) are **distinct transports/hardware/wiring/perf**. The design brief wants **both** (the ~$35 HAT mode *and* this network retrofit). *Filling* the stub conflates GPIO with network; *replacing* it drops a wanted path. As peers they share the composited RGB framebuffer and differ only in the output transport.

## 4. Module architecture

Split for host-testability (mirrors the existing `hdmi.rs` / `hdmi_logic.rs` split):

- **`colorlight_logic.rs` — pure serializer, no socket, host-testable on macOS.** `serialize_frame(fb: &Rgb888Frame, cfg: &ColorlightConfig, brightness) -> Vec<Vec<u8>>` (a Vec of L2 frames: 0x0A, 128× 0x55, 0x01). All the byte-layout + geometry-remap + color-order + LUT logic lives here. This is what the golden + loopback tests exercise.
- **`colorlight.rs` — Linux-only transport.** `#[cfg(target_os="linux")]`; opens the raw socket, blasts the frames on the configured NIC. Thin.
- **Config (env, matching the renderer convention — `OPENMARQUEE_COLORLIGHT_*`):** `IFACE`, `WIDTH`=128, `HEIGHT`=96, `PARALLEL`=4, `CHAIN`=3, `COLOR_ORDER` (rgb/bgr), `BRIGHTNESS`, and the **geometry-remap params** (which 4 of 8 outputs, chain order, per-module orientation/offset). **These MUST equal FPP's blessed config** (§7) — captured as a documented source-of-truth and asserted in tests. Nothing hard-coded.
- **Crate decision — DECIDED (admin, 2026-07-12): raw `AF_PACKET` via `libc`/`nix`, no `pnet`.** The renderer already carries `libc` + `nix`; the transport is ~50 lines either way; the serializer is byte-exact from spec (not delegated to a crate), so `pnet` buys nothing at runtime; and the Pi Zero 2 W ethos is tight deps. (The `colorlight` crate stays a read-only *reference*, not a dependency.)
- Needs **`CAP_NET_RAW`** (root or `setcap cap_net_raw+ep`).

## 5. Renderer integration point

- **Frame tap:** after GL compositing, `glReadPixels` the default framebuffer → RGBA8 → RGB888 — the **exact pattern `live_preview.rs` already uses** (src/live_preview.rs:205-244). At **128×96 the readback is ~48 KB** (trivial), vs the 8 MB that makes it expensive at 1080p. Render at sign-native 128×96 (small FBO), no DRM scanout.
- **Headless compositing — SPIKE DONE (2026-07-12, verdict GO; see `docs/colorlight-egl-spike-2026-07-12.md`).** The GL context is GBM/DRM-bound today, but Colorlight needs no exotic capability. **Pattern A (recommended): the existing `hdmi.rs` GBM+EGL bring-up minus the DRM modeset/page-flip** — GBM surface at 128×96, composite via the existing paint path, then `glReadPixels` (no scanout). This is the proven Pi-headless shape (matusnovak `triangle_rpi4.c`) and a near-zero-delta from our own code. Pattern B (surfaceless context + FBO; `EGL_KHR_surfaceless_context`, confirmed on vc4/Mesa) is an optional later cleanup. One live-confirm step remains (dev Pi offline; won't touch live jasonssign1) but the integration shape is concrete + low-risk.
- **Geometry mismatch** (content ≠ 128×96): fit/letterbox to the panel by default, configurable (don't blow up).

## 6. Hardware-less test plan (3 layers) — the core deliverable

All layers run in `cargo test`, no rig, no display, no qarl.

- **Layer 1 — golden-pcap conformance. THE ONLY LAYER THAT PROVES PROTOCOL CORRECTNESS.** For each pattern, store the **pair** `(input framebuffer → expected packet bytes captured from FPP)`. Assert my serializer's output is **bit-exact**, or articulate + prove any diff is semantically inert (e.g. a frame counter the receiver ignores). Patterns (mapping-first, because a scrambled panel encodes as *protocol-legal* packets that no data test catches):
  1. **Single-pixel walk** — one lit pixel swept over all 128×96. *Highest value:* pins exact pixel→packet mapping; catches off-by-one at 32-px module seams + per-module mirror/rotation.
  2. **Per-module solids** — each of the 12 modules a distinct color. Reveals module ordering + which physical output each chain lands on.
  3. **Row & column sweeps** — a single H/V line marching. Catches scan-direction + row/col transpose.
  4. **Pure R / G / B** — pins **channel order** (RGB/BGR/GRB) at the byte position.
  5. **Linear gradient** — pins the **gamma/brightness LUT** (a naïve linear encode looks washed/crushed even if the sign lights).
  6. Solids / checkerboard / motion — color + cadence.
- **Layer 2 — loopback round-trip.** A software receiver-emulator parses my L2 frames per spec → reconstructs a framebuffer → asserts `== input` bit-exact. **CALIBRATION (stated loudly so no future reader miscalibrates): this only proves my encode/decode is self-consistent and lossless. It does NOT prove the wire format matches a real Colorlight. A green Layer 2 is not "correct" — only Layer 1 is.**
- **Layer 3 — cadence.** packets/frame (128 rows + 1 brightness + 1 latch), order (`0x0A`→`0x55` ascending→`0x01`), ≤22 ms/frame budget, frame rate.

## 7. Golden-capture sequencing (respects QA's blessing gate)

**No canonical pcaps exist** (research confirmed q3k/hkubota/kholia/etc. carry none) → we self-generate from FPP. Captures split by wiring-dependence:

- **Format goldens** (packet *structure*, channel order, LUT): packet structure is wiring-independent and can be pinned **now** against hkubota's hex dumps + FPP source. Channel-order/LUT depend on FPP's *panel* config, so those capture cleanly once FPP is configured.
- **Mapping goldens** (single-pixel walk, per-module solids, row/col sweeps): meaningful **only against qarl's blessed FPP config.** Sequencing (QA-owned):
  1. **Phase-A first-light:** QA configures FPP to qarl's *fixed* wiring, runs a test pattern; **qarl's eyes verify no mirror/gap/misalignment → this BLESSES the FPP wiring config.**
  2. **Only then** QA captures the mapping pcaps against the blessed config (the sender emits packets whether or not the card is listening; the card+eyes were needed for the *blessing*, not the capture).
  3. **My config is pinned to that blessed wiring** as a documented source-of-truth, referenced in the tests. Per QA: a config divergence scrambles the panel and **no internal test catches it — only the correctly-wired reference does.** qarl's wiring is fixed ("don't reconfigure the chains") — the driver conforms to it, not vice-versa.

## 8. Edge cases (design positions)

- **Link down / nothing on wire:** detect via `/sys/class/net/<if>/{carrier,speed}` (FPP-style) → log + no-op send; **must never wedge the renderer** (it keeps compositing).
- **Idle / all-black:** send explicit black frames (not silence) so the card holds a defined state, avoiding stale-row artifacts. Configurable.
- **First-frame cold-start:** frame 1 sends brightness + **all 128 row packets** + latch (full addressing) so cold-start isn't garbage.
- **Sub-1000 Mbps link:** warn/refuse like FPP.
- **Dropped packet:** hold-last is the card's behavior; the next latch still fires — acceptable.

## 9. Phased plan

- **Phase 0 (now, no card):** this doc → review; pin packet *structure* against hkubota/FPP; build `colorlight_logic` serializer + the Layer-2 emulator + Layer-3 cadence + the structure-level Layer-1 harness. Fully offline.
- **Phase 1 (card in hand; QA + qarl):** Phase-A blessing; capture mapping + color + LUT goldens; pin my config to the blessed wiring; validate the serializer against **all** goldens (Layer 1 complete).
- **Phase 2:** renderer integration (surfaceless-EGL spike → readback tap → `OutputMode::Colorlight`); HW-in-loop first-light with **my** driver; qarl's eyes = final pixel-on-glass.

## 10. Risks / open questions (for the review pass)

1. **Surfaceless EGL on vc4/Mesa** — top integration risk; a spike gates the integration shape (§5).
2. **Color order RGB/BGR** — panel-dependent, the biggest "lights up but looks wrong" risk; config knob + verify against pure-R/G/B goldens.
3. **Row-axis transpose + chain reversal + per-module orientation** — the remap is cabling-dependent and cannot be finalized until qarl's exact wiring is blessed (§7).
4. **Gamma/brightness LUT** — must match FPP's or images look washed/crushed; the gradient golden pins it.
5. ~~`pnet` dependency vs raw `AF_PACKET`~~ — **RESOLVED: raw `AF_PACKET` via libc/nix (admin, 2026-07-12).**
6. **Firmware sensitivity** — brightness may no-op on old FW; v13+ duplicates the brightness/latch packets. Read the card's FW at first-light.
7. **No ready-made goldens** — the mapping goldens need qarl's card for the one blessing/capture pass; everything downstream is offline.

## 11. v2 — review-pass additions (admin adjudications + QA review, 2026-07-12)

Folds QA's review (`qa/reports/2026-07-12/colorlight-design-review.md`) + admin's calls. Items are additive to the sections above.

### A. Golden patterns (adds to §6)
- **Human-blessing fiducial = the openMarquee wordmark** (admin A.1/E.3). Letters are inherently asymmetric (no rotational/mirror symmetry to fool the eye), so qarl can confidently bless "no mirror/gap/misalignment". Fallback if no rendered wordmark asset exists at Phase A: an ASCII text image `openMarquee →` (the arrow is the mirror/rotation tell). Ship both; QA picks whichever renders more legibly at 128×96 P8.2. This is the **defined Phase-A qarl-eyes artifact** (the machine patterns are unblessable by eye).
- **Module-seam pattern** — 1-px lines exactly on each 32-px seam (or alternating 32-px bars). Sharper than the row/col sweep for inter-module gap/overlap + seam off-by-one.
- **LUT endpoints** — explicit **all-black (0)** and **all-white (255)** goldens. The gradient pins the middle; clamp/round bugs live at the ends. All-white doubles as max-payload + PSU/thermal sanity (4× 350 W supplies) during Phase A.

### B. ⭐ Synthetic unit tests (adds to §6/§8) — the review's top catches; IMPLEMENTED in Phase 0
Two correctness paths the 128×96 config **structurally never exercises**, so **no capturable golden can cover them** — only synthetic tests can:
- **Multi-packet row split:** 96-px rows are single-packet (≤497), so the `0x55` pixel-offset/count fields + the split loop are never driven in production. Test drives a >497-px row (`chain=20` → 640 px) and asserts offset advances by 497, count = remainder. *(`synthetic_multi_packet_row_split_over_497px`.)*
- **Big-endian row#:** production rows are 0–127 (high byte always 0), so a BE-vs-LE row# bug is invisible to every golden. Test drives row# > 255 (`outputs=16` → 512 rows) and asserts the high byte lands at wire offset 13. *(`synthetic_big_endian_row_number_over_255`.)*

### C. Edge cases (adds to §8)
- **Mid-frame partial-update race:** `serialize_frame(&fb)` takes a shared ref and never mutates/aliases — the **contract is "caller passes a stable snapshot"** (the Phase-2 integration copies the `glReadPixels` buffer before serializing). Pinned by `serializer_does_not_alias_input_snapshot`.
- **Config-vs-FBO / geometry mismatch:** `serialize_frame` returns `SerializeError` (never panic/UB) on a framebuffer whose length ≠ `width·height·3`, or config dims that violate the transpose remap (`width==card_rows`, `height==row_width_px`). Pinned by `dimension_mismatch_is_an_error_not_a_panic` + `non_bijective_geometry_is_an_error`.

### D. Serializer ↔ rig interface (admin ask #4, QA §D)
- **`serialize_frame` emits FULL L2 frames** — 14-byte Ethernet header (dest MAC `11:22:33:44:55:66` + src + EtherType `opcode<<8|data0`) then payload. So Layer-1 pcap comparison verifies EtherType + dest MAC directly with **no header-strip step**, and the transport is a raw blast. Pinned by `every_frame_carries_full_l2_header_and_ethertype`.
- **Fixtures layout (canonical):** `qa/fixtures/colorlight-fpp-refs/<pattern>/{input.png, fpp.pcap, meta.json}` — the known input framebuffer, FPP's capture, and a manifest (wiring-rev, FPP-config ref, pattern params). Rig: `input → serialize → compare to the pcap's frames filtered by dest MAC 11:22:33:44:55:66, in order`. Capture on FPP's dedicated isolated Colorlight NIC; the rig filters by dest MAC regardless.

### E. Mapping-golden IMMUTABILITY (admin C.1 — STRICTER than convention)
Once blessed, a mapping golden's `(pcap, input, meta)` triple is **IMMUTABLE**. Regeneration REQUIRES a re-blessing pass with qarl's eyes — never a silent recapture (the one way this scheme fails quietly: a bad recapture bakes a wrong mapping into the "golden" and Layer 1 passes against a bad reference). Enforcement:
- Each `meta.json` records: capture-date, **wiring-revision-tag**, **FPP-config-checksum**, admin-signoff-hash.
- The **actual blessed FPP config** (`channeloutputs.json`) is committed at `qa/fixtures/colorlight-fpp-refs/blessed-fpp-config.json` — the source-of-truth as a file, not prose. Any change to it = a wiring revision → re-bless.
- A PR that changes any fixture triple MUST include (a) an updated `blessed-fpp-config.json` and (b) a new `blessing-YYYYMMDD-qarl.md` note stating what qarl visually verified. **QA + admin gate on this; missing either → no merge.** Any physical rewire (dead module, swapped chain) invalidates the mapping goldens → re-bless. `wiring_revision` is carried in `ColorlightConfig` so a code/golden drift is *detectable*.
- **Format-golden boundary (QA §7.3):** wiring-*independent* goldens assert ONLY framing (opcodes, magic `0x08/0x88`, header layout, packet order, EtherType) — **never payload pixel positions**, which encode the (unblessed) mapping. Phase 0's Layer-1 tests hold exactly this line.

### F. Phase-A prerequisites for QA (QA §E — flagged, QA-owned, not blocking)
- **FPP pattern-injection procedure** (how QA feeds FPP a *known* framebuffer so the (input, pcap) pair is pixel-exact) is QA's to work out at Phase A — flagged as prep; the input must be exactly known or the golden pair is meaningless.
- **Phase-A' host re-run:** the serializer + Layer-2 emulator + Layer-3 tests are **pure (no net/HW)** and run on QA's Mac via `cargo test` — confirmed (Phase 0 tests run in an isolated std-only crate; zero deps). QA independently greens them to catch works-on-my-box.
