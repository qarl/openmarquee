# Recommended outer-repo edits — 2026-05-31

Inner-repo Jimmys (code2 / code1 / qa / www / admin) don't have
authority to edit the outer-repo specs at `~/project/openmarquee/`
per the standing topology rule. This doc captures two specific
edits from the r25 spec-delta audit
(`qa/v1-spec-delta-2026-05-30.md` §4.1 + §4.2) and the r26 v1.0
close-out (commit `c38e64d`) so the openmarquee admin Jimmy can
apply them mechanically.

Both are small doc-only edits. No code touched.

---

## Edit A — `IMPLEMENTATION_PLAN.md` milestone-pointer refresh (§4.2)

### Context

The "Status as of 2026-04-21" line at line 163 is 40 days stale.
It predates: Phase 6 HDMI bring-up, Phase 7 IPC sidecar +
transitions, V4L2 H.264 decode pieces 1–4f, the v0.6.0-beta tag
(2026-05-23), the v0.9.0 release (2026-05-30, tag `a50e928`), and
the v1.0 text-layer chrome triad close (2026-05-31, `c38e64d`).

A future reader walking the document's status pointer would think
they're seeing pre-hardware-arrival state, not late-v0.9.0 / pre-
v1.0 ship reality. That's the specific drift surfaced in r25 §4.2.

### Proposed diff

**File:** `IMPLEMENTATION_PLAN.md`
**Line:** 163

**Current:**
```
**Status as of 2026-04-21**: the *dry-land* half of the demo is shipped. Phases 0 / 2 / 3 / 4 / 5 / Phase-1 spike all landed — visible at `localhost:8000` via `bash scripts/dev.sh`. What remains is bringing up real hardware (Phases 6 / 7 / 9) when the Pi Zero 2 W kit arrives.
```

**Proposed:**
```
**Status as of 2026-05-31**: v0.9.0 shipped 2026-05-30 (tag `a50e928`) — single-device sign controller, fully-shipped Phases 0-7 + 9 plus the post-demo work (HUB75/WS2812B paths still pending Rust port). Text-layer chrome triad (anchor + visible-at-save + weight wire) closed `c38e64d` 2026-05-31. v1.0 is takeable on qarl greenlight; qarl is holding the tag while a few small product calls play out. The demo described below was hit + sailed past months ago — the live system is well beyond it. Phase-by-phase landed-status is captured in each phase section below.
```

### Rationale

- Anchors the pointer to a real recent date (today)
- Names both the v0.9.0 cut AND the r26 triad close so a reader can
  walk forward from this pointer to git for context
- Explicitly says "v1.0 is takeable, holding for qarl" so the doc
  matches the actual decision state, not a 6-week-old hardware-
  pending state
- "Demo was hit + sailed past months ago" gives readers a quick
  framing for why the table of demo-phase contents below is
  retrospective, not prospective

### Apply via

```
git -C ~/project/openmarquee/ diff -- IMPLEMENTATION_PLAN.md  # review
# Then edit line 163 with the proposed replacement above.
git -C ~/project/openmarquee/ add IMPLEMENTATION_PLAN.md
git -C ~/project/openmarquee/ commit -m "docs: refresh IMPL_PLAN milestone pointer to 2026-05-31"
git -C ~/project/openmarquee/ push origin main
```

---

## Edit B — `SYSTEM_SPEC.md` line 77 HUB75-row wording polish (§4.1)

### Context

Line 77 lives inside the §2.3 "Languages and stack" table. The
HUB75 output row opens with "*Pending Rust port (§7.2)*" then ends
with "the Rust replacement is future work." The "future work"
phrase is locally redundant with "Pending Rust port" earlier in
the same cell — a reader scanning quickly could mistakenly read
"future work" as a global statement (covering HDMI), even though
the table structure anchors it to HUB75.

This is small wording polish to make the cell internally consistent
with itself — both clauses now use the same "pending" framing.

### Proposed diff

**File:** `SYSTEM_SPEC.md`
**Line:** 77

**Current:**
```
| HUB75 output | *Pending Rust port (§7.2). Pre-v0.6 builds used `hzeller/rpi-rgb-led-matrix` via Python bindings; the v0.6 PIL teardown removed the Python driver, and the Rust replacement is future work.* |
```

**Proposed:**
```
| HUB75 output | *Pending Rust port (§7.2). Pre-v0.6 builds used `hzeller/rpi-rgb-led-matrix` via Python bindings; the v0.6 PIL teardown removed the Python driver, and the Rust replacement is pending (see §7.2).* |
```

### Rationale

- "the Rust replacement is pending (see §7.2)" matches the row's
  own opening "Pending Rust port (§7.2)" — internally consistent
  framing instead of "future work" vs "pending"
- The §7.2 cross-link inside the sentence walks a reader directly
  to the pending-port detail
- HDMI row above (line 76) is unambiguously live ("Rust renderer
  sidecar (openmarquee-render) using DRM/KMS atomic + EGL + GBM +
  GLES2 shader compositing; H.264 video decoded via V4L2 → dmabuf
  → GLES2 zero-copy") so removing "future work" from line 77
  prevents any chance of cross-row contagion

### r25 audit note

The original r25 audit (§4.1) proposed a bigger rewrite that
also pulled HDMI explicitly into line 77. On re-reading the table
structure, line 77 is HUB75-row-local, so mixing in HDMI would
muddy a row that's already clearly scoped. The smaller polish
above is the right shape.

### Apply via

```
git -C ~/project/openmarquee/ diff -- SYSTEM_SPEC.md  # review
# Then edit line 77 with the proposed replacement above.
git -C ~/project/openmarquee/ add SYSTEM_SPEC.md
git -C ~/project/openmarquee/ commit -m "docs: align HUB75 row 'pending' framing with row opener"
git -C ~/project/openmarquee/ push origin main
```

---

## Summary

| Edit | File | Type | LOC |
|------|------|------|----:|
| A    | `IMPLEMENTATION_PLAN.md` | milestone-pointer refresh | 1 line replace (long sentence) |
| B    | `SYSTEM_SPEC.md` | wording polish | ~5 words replace |

Both are mechanical, low-risk, doc-only. Either / both can be
applied independently; they don't depend on each other.

---

Filed by jimmy:openmarquee-code2 2026-05-31. Source: r25 audit
findings + r26 v1.0-takeable status.
