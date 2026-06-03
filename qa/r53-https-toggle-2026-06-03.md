# r53 — SystemSettings.tailscale_https_enabled UI exposure

**Author:** jimmy:openmarquee-code2
**Date:** 2026-06-03
**Status:** SHIPPED on code2; cherry-picked to main
**Dispatch:** qarl-greenlit polish, closes 9-day backlog from r49 F078
**Predecessors:**
  - r49 UI-vs-model audit (12a986e7) — flagged F078 HIGH severity
  - [[project_https_phase_1_shipped_2026_05_24]] memory — noted
    "awaiting qarl admin-console HTTPS toggle" 9 days ago

## Goal

`SystemSettings.tailscale_https_enabled` (settings.py:240, default
True) was consumed by the backend at:
  - `backend/openmarquee/fqdn_redirect_middleware.py:120` —
    decides whether non-FQDN traffic gets 301'd to the canonical
    HTTPS FQDN
  - `scripts/install.sh` — gates the `tailscale serve --bg
    --https=443` provisioning command

The UI surfaced ZERO control for it. Operators wanting to flip
HTTPS off had to edit `/var/openmarquee/settings.json` directly,
or run the install.sh flow with the env var dance.

r53 adds the checkbox in the Tailscale fieldset of the Settings
page.

## Implementation

### `ui/src/settings.js` — 4 touchpoints

1. **HTML (template literal):** new `<label class="field-inline">`
   row at the bottom of the Tailscale fieldset, after the hidden
   auth-key input, containing:
   ```html
   <input type="checkbox" class="field-tailscale-https-enabled">
   <span>Enable HTTPS on Tailscale FQDN</span>
   ```
   Inline-style margin-top: 10px to space it from the auth-key
   secret-substitution sentinel above.

2. **Element lookup:** `tsHttpsEnabledEl` added next to the
   existing Tailscale element queries (line ~393).

3. **Hydration (loadSettings path):**
   ```js
   tsHttpsEnabledEl.checked = settings.tailscale_https_enabled !== false;
   ```
   The `!== false` shape handles 3 incoming wire values:
   - `true` → checked
   - `undefined` / `null` (legacy settings.json missing the key)
     → checked (matches the model's `default=True`)
   - `false` → unchecked
   This means a legacy device upgrading to a build with the new
   UI gets HTTPS-on by default (matching the model default and the
   device's at-rest HTTPS-on behavior).

4. **Serialization (collectPayload):**
   ```js
   tailscale_https_enabled: tsHttpsEnabledEl.checked,
   ```
   Bool (not nullable per the model); checkbox.checked is canonical.

### `ui/src/settings.test.js` — 2 new tests + 1 SAMPLE update

- `SAMPLE` now includes `tailscale_https_enabled: true` (matching the
  model default).
- NEW: "hydrates checkbox from settings and serializes on save" —
  loads settings with the field=false, asserts the checkbox starts
  unchecked, toggles it, asserts the saved payload reflects true.
- NEW: "hydrates as true when settings omits the key (model
  default)" — strips the field from SAMPLE entirely, asserts the
  checkbox renders checked. This pins the
  legacy-settings-json-missing-the-key invariant against future
  refactors.

## What I did NOT touch

- `backend/openmarquee/settings.py` — field already exists at :240
- `backend/openmarquee/fqdn_redirect_middleware.py` — reader path
  works
- `scripts/install.sh` — provisioning path works
- HTTPS server-side logic generally — per dispatch constraint

The field was always wired end-to-end on the backend; r53 only
fixes the UI surface gap.

## Test coverage

- Backend: no new tests (existing SystemSettings round-trip + the
  field-default tests cover the wire shape; r49 audit confirmed
  no backend gap)
- Frontend: 2 new vitest cases in `ui/src/settings.test.js`
  covering the read + write path AND the legacy-missing-key
  default-true hydration. JS syntax verified via `node --check`;
  vitest not runnable locally per
  [[feedback_npm_install_virtiofs_wedge]] (jsdom missing). Pre-push
  hook will warn-pass on the missing binary.

## Files changed

| File                       | Change                                          | LOC |
| -------------------------- | ----------------------------------------------- | --- |
| `ui/src/settings.js`       | HTML row + element lookup + hydrate + serialize | ~24 |
| `ui/src/settings.test.js`  | SAMPLE update + 2 new tests                     | ~40 |
| `qa/r53-https-toggle-...md`| This audit doc                                  | ~140|

## Sacred subagent review

Pending — runs before commit.

## §G Open questions

### G.1 Should the checkbox be gated on tailscale_enabled?

Currently the checkbox is always interactive. If the operator
disables Tailscale entirely (top of the fieldset), the HTTPS
toggle still appears interactive but has no effect — HTTPS-on-
tailnet only matters when the daemon is up.

**Recommendation:** leave as-is. Disabling/dimming the HTTPS row
based on the Tailscale-enabled state is a UX polish that's
inconsistent with how the Hostname field behaves (also always
visible/interactive when Tailscale is disabled). If qarl wants
the gating, it's a 3-LOC follow-up. Park unless flagged.

### G.2 Should there be a "what this does" hint paragraph?

The hint at the top of the Tailscale fieldset (line ~217)
explains Tailscale itself. The HTTPS row currently has just the
label. A 1-sentence hint below the checkbox (e.g. "Provisions a
Let's Encrypt cert via tailscale serve. Disable to serve plain
HTTP on port 80.") would help operators understand the choice.

**Recommendation:** add the hint if qarl wants it; the model
docstring already explains; the UI is fine without. Park.

## Lane

- Doc + UI-only commit: 1 audit doc + 2 ui/src/ files
- code2 push; cherry-pick to main via /tmp clone
- No SYSTEM_SPEC.md edits (§5.8 settings admin console paragraph
  may want a touch but that's admin-Jimmy lane per r49 §F.2)
- No code1 conflict — pure ui/src/ surface, distinct from r50
  text-over-video renderer work

## Push posture

- Backend pytest gate applies but no backend touched; expected
  pass-through
- Renderer cargo gate applies but no renderer touched; expected
  pass-through
- UI vitest warn-passes
- Standard /tmp clone + cherry-pick if NFS-wedges

---

End of r53 audit.
