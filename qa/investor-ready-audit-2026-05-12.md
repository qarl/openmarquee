# Investor-readiness audit — 2026-05-12

Scope: "qarl hands a Pi to an investor and they get a working sign." Status per item; gaps in commit-able order.

## Shipped (DONE)

- **Phase B SD-card automation**: pi-gen + cloud-init + install.sh + flash-sd.sh. End-to-end flashable image exists (sweep #5 #2 closed).
- **First-boot oneshot**: `system/openmarquee-firstboot.sh` generates per-device 16-char AP passphrase, templates hostapd + welcome.html, writes `/var/openmarquee/wifi.json` (0600). f1cf12b.
- **Operator password + bearer auth (Phase A)**: welcome → set-password → login → bearer-protected `/api`. Argon2id with OWASP-floor params.

## Gaps (require code)

Each is a separate commit per the standing "small commits" rule.

### 1. MySignXXX identifier — single source of truth (commit 2 of arc)
- **Current**: SSID is `openMarquee-<MAC-suffix>` (firstboot, derived from wlan0 MAC last 2 octets); hostname is `openmarquee-<4hex>` (cloud-init, from `od -An -tx1 -N2 /dev/urandom`). Two unrelated 4-hex-char identifiers from different sources. Plus an existing free-form `sign_name` field in settings + a `tailscale_hostname` mirror.
- **Needed**: Generate `MySignXXX` (3 alphanumeric chars, ~46k space) in firstboot BEFORE AP-password. Persist to `/var/openmarquee/identity.json` as `{device_id}`. Apply to: `hostapd.conf` SSID, `/etc/hostname` + `hostnamectl`, `welcome.html` template, `/api/system/info` exposes `device_id`. The existing `sign_name → tailscale_hostname` mirror should pick up MySignXXX as its default at firstboot.
- **Open question for qarl** (per `feedback_handoff_narrowing_migrations`): is MySignXXX (a) a factory-generated ID **separate from** `sign_name` (which stays free-form for operator-chosen display), or (b) a **replacement** for `sign_name`? Default assumption pending qarl's call: (a) — MySign is the factory-anchored ID used for SSID/hostname/Tailscale identity; `sign_name` stays free-form and is operator-renameable but defaults to MySignXXX at firstboot. Surface this before implementing if qarl wants (b).
- **Tests**: firstboot integration, SystemInfo round-trip, regex `MySign[A-Z0-9]{3}`.

### 2. Remove "Save settings" button — autosave-only UX (commit 3 of arc)
- **Current**: `ui/src/settings.js:280` has form-level Save submit + 4 inline secret-save buttons (SSH key / Wi-Fi STA pw / Tailscale key / change-pw). Form-level auto-save not wired.
- **Needed**: Drop the form-level button; route field changes through `attachAutoSave` debounced PUTs (already in `auto-save.js`). Secret-input fields may keep inline Save (write-only-when-explicit). Anything that becomes autosave-on-blur needs a status pill.
- **Tests**: vitest unit; Playwright e2e — edit a field, PUT fires without button click.

### 3. Tailscale URL-auth — no pre-shared key (commit 4 of arc)
- **Current**: `settings.py:229` requires `tailscale_auth_key`; operator must paste a tskey from the admin console.
- **Needed**: Replace field with Enable button. `POST /api/system/tailscale/up` spawns `tailscale up` without `--auth-key`, parses stdout auth URL, returns it. Poll `tailscale status` until authenticated; surface state via `/api/system/info`. UI shows clickable URL + QR for cross-device sign-in.
- **Why important**: once authenticated, MySignXXX becomes the magic-DNS name on the operator's tailnet — composes with #1.
- **Tests**: backend contract on the new endpoint (mock `tailscale` binary); Playwright on the UI flow.

## Gating test (out of arc but flag)

**End-to-end real SD flash + Pi boot + investor smoke** on office hardware: flash → first boot → operator joins AP → sets pw → walks editor → HDMI shows correct output. Should run before the investor demo; not a code commit.

---

After audit lands: MySign → Save-button removal → Tailscale URL-auth.
