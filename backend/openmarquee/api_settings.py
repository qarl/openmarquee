"""REST API for device system settings.

GET /api/settings — current settings (defaults if nothing persisted yet)
PUT /api/settings — replace settings
PATCH /api/settings/{wifi-ap-password,wifi-station-password,
                     tailscale-auth-key} — rotate one secret;
                     requires the operator's current login password
                     as a re-auth gate (Batch 20.4).

Writes validate the whole model, so partial updates are an explicit GET,
mutate, PUT client round-trip. The UI does exactly that on Save; keeping
the server PUT-only means we never have to reason about which field is
authoritative in a race.

Secret redaction (Batch 20.4 / phase A.4): GET responses replace the
three secret fields -- wifi_password (AP WPA2 passphrase),
wifi_station_password (operator's home WiFi passphrase), and
tailscale_auth_key -- with the sentinel "<set>" when populated or None
when unset. The 20.4 dispatch's rationale: sweep #5 #1 originally
flagged that the operator's home WiFi password was readable in
cleartext via GET /api/settings. Returning the sentinel keeps the wire
shape stable (UI still reads the field) while the actual value never
leaves the device after the initial PATCH.

PUT secret handling (Batch 20.4): the three secret fields are
INTENTIONALLY ignored on PUT -- whatever the caller sends gets
replaced with the stored value before persistence. The redacted GET
returns "<set>" for those fields, the UI dutifully echoes "<set>"
back on Save, and we shouldn't accidentally persist the sentinel as
the real password. Updates go through the PATCH endpoints which gate
on the operator's current login password. This is the "restrict"
option from the 20.4 dispatch — cleaner separation than a sentinel-
passthrough hack in PUT.

PUT also fires a side-effect when display dims change (rotation OR
width OR height): every saved text slide gets re-rendered at the new
effective dims so its asset.png matches what the device will display
instead of being letterboxed / cover-fitted from stale dims. The
re-render runs SYNCHRONOUSLY before PUT returns 200 — operators expect
a rotation flip to "do something" and the UI re-mounts every panel
immediately on the openmarquee:settings-updated event, so an async
BackgroundTask raced the re-mount and the new slide-browser fetched
GET /api/content before the rerender bumped updated_at on disk
(QA-flagged race 2026-04-30). Synchronous trades a ~1s rotation save
for a guaranteed-coherent post-save state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

# text_rerender removed in DELETE-PIL phase 3 (qarl-direct 2026-05-13):
# the Python+PIL rasterizer is gone; operator-saved PNGs are baked
# browser-side via Canvas2D. Display-dim changes now leave the stored
# PNGs intact — the device's playback engine cover-fits at panel-
# native dims, and the operator's next edit-and-save in the editor
# re-bakes at the new dims (Option α from the dispatch).
from openmarquee import wifi_station
from openmarquee.api_auth import _strip_bearer
from openmarquee.auth import AuthStorage, verify_password, verify_token
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    get_auth_storage,
    get_content_storage,
    get_playback_loop,
    get_settings_storage,
)
from openmarquee.playback import PlaybackLoop
from openmarquee.settings import SettingsStorage, SystemSettings
from openmarquee.wifi_prefill import read_system_wifi

router = APIRouter(prefix="/api/settings", tags=["settings"])

SettingsDep = Annotated[SettingsStorage, Depends(get_settings_storage)]
ContentDep = Annotated[ContentStorage, Depends(get_content_storage)]
AuthDep = Annotated[AuthStorage, Depends(get_auth_storage)]
LoopDep = Annotated[PlaybackLoop, Depends(get_playback_loop)]

log = logging.getLogger(__name__)

# 2026-07-14 (Option D): a dedicated audit logger for security-sensitive
# operator actions (currently: WiFi-password reveals). Distinct logger
# name so it's filterable; routes to journald on the device, giving the
# operator a "when did I reveal that" retrospection trail. It records the
# ACTION + SSID + OUTCOME only — NEVER a secret VALUE (auditing the PSK
# would defeat the containment the reveal flow is meant to preserve).
_audit_log = logging.getLogger("openmarquee.audit")


def _scrubbed_error_summary(exc: Exception) -> str:
    """Return a log-safe summary of a validation exception that does NOT
    contain the rejected input value.

    Pydantic's `str(ValidationError)` and the default `.errors()` both
    include the failing input verbatim. On the PATCH-secret path the
    input IS a secret -- if a `log.warning("...: %s", exc)` line
    serialises that string into journald/syslog, anyone with shell on
    the Pi reads the password via `journalctl -u openmarquee`. The
    `include_input=False` knob (pydantic >=2.4) suppresses the `input`
    key, BUT a `field_validator` that does
    `raise ValueError(f"..., got {value!r}")` will land its ValueError
    instance in `ctx['error']` -- and `repr()`ing that dict re-prints
    the value via the ValueError's __repr__. So we ALSO strip `ctx`
    here and serialise only loc/type/msg, which can never carry the
    raw input as long as field_validator raise-messages don't embed
    `{value!r}`.

    Important convention for future contributors: field_validator
    raise messages on SECRET fields (wifi_password,
    wifi_station_password, tailscale_auth_key) MUST stay free of
    `{value!r}` -- otherwise that string lands here via msg even
    without ctx. The non-secret validators in settings.py (wifi_ssid,
    timezone) do use {value!r}; safe for them, dangerous to copy
    onto a secret.

    For non-Pydantic exceptions we fall back to `type(exc).__name__`;
    the message body could contain arbitrary text and isn't worth
    trusting for log-safety.
    """
    try:
        # Pydantic v2 ValidationError. Strip both `input` (the raw
        # rejected value) AND `ctx` (which may carry a ValueError whose
        # message re-includes it). Keep loc + type + msg + url for the
        # operator's audit trail.
        keep = ("loc", "type", "msg", "url")
        sanitised = [
            {k: err[k] for k in keep if k in err} for err in exc.errors(include_input=False)
        ]
        return repr(sanitised)
    except (AttributeError, TypeError):
        return type(exc).__name__


# 20.4: sentinel returned by GET in place of each of the three secret
# fields. Picked for readability + the leading `<` so a careless eye-
# ball matcher doesn't mistake it for a real password. The UI tests
# for this exact string when rendering the "•••• Set" / "Change..."
# affordance.
SECRET_SENTINEL = "<set>"

# 20.4: the secret fields that get redacted on GET, preserved-from-
# stored on PUT, and rotated via PATCH. Keeping the list here (rather
# than scattered across the response-model logic) means a future
# secret-field addition lands in one place.
SECRET_FIELDS: tuple[str, ...] = (
    "wifi_password",
    "wifi_station_password",
    "tailscale_auth_key",
)


def _redact_secrets(dump: dict[str, Any]) -> dict[str, Any]:
    """Apply the 20.4 redaction rule to a SystemSettings.model_dump().

    Each SECRET_FIELDS key gets replaced with SECRET_SENTINEL when the
    stored value is truthy (non-empty + non-None), or None when unset.
    Caller mutates the dict in place and returns it.

    2026-07-03 (qarl handover B1): also masks every
    `wifi_networks[i].password` — the multi-network schema stores
    PSK per entry, so a bare SECRET_FIELDS scan misses them. Each
    entry's `password` becomes SECRET_SENTINEL when populated or
    None when unset; the entry's other fields (ssid, autoconnect,
    priority) round-trip verbatim so the UI can render them.
    """
    for key in SECRET_FIELDS:
        value = dump.get(key)
        if value:
            dump[key] = SECRET_SENTINEL
        else:
            dump[key] = None
    for entry in dump.get("wifi_networks") or []:
        if not isinstance(entry, dict):
            continue  # defensive: model_dump always emits dicts
        if entry.get("password"):
            entry["password"] = SECRET_SENTINEL
        else:
            entry["password"] = None
    return dump


@router.get("/wifi-station-state")
async def get_wifi_station_state() -> dict[str, Any]:
    """Return the station-mode status for the Settings Join section.

    Shape: {state, detail, ssid, connected_ssid, connected_probe_ok}.
    Called by settings.js on panel load and on each Rescan click (it is
    not polled; `state`/`detail` currently have no UI consumer).

    Two DIFFERENT questions, deliberately kept apart:

    * `state` / `detail` / `ssid` — the in-memory APPLIER status: what
      the last apply() *in this process* was doing. `ssid` is that
      attempt's TARGET, not the live link. Right answer to "how is the
      join I just submitted going?"; wrong answer to "what are we on?"
      (a sign that booted already-connected never ran an apply, so it
      reports idle/None while happily associated).

    * `connected_ssid` — the LIVE association, read fresh from
      NetworkManager, same source the boot card uses so the two
      surfaces can't disagree about what network the sign is on.

    * `connected_probe_ok` — whether that live read actually ANSWERED.
      Load-bearing: a null `connected_ssid` is ambiguous between
      "definitely not connected" and "we couldn't tell", and rendering
      the latter as "Not connected" on a sign that IS connected is the
      bug class this endpoint exists to avoid. False => the caller must
      say "unknown", never "not connected".

    This reads `active_wlan0_ssid_probe`, NOT `active_wlan0_ssid`: the
    latter fails soft to None for no-nmcli / timeout / non-zero rc as
    well as for genuinely-not-connected, so it cannot answer the
    probe_ok question at all. The blocking nmcli call runs off the event
    loop at the probe's own default timeout (matching the boot card — an
    aggressive override would turn a slow-but-connected Pi into a false
    "not connected").
    """
    state = wifi_station.current_state()
    connected_ssid: str | None = None
    connected_probe_ok = False
    try:
        from openmarquee.network_supervisor import active_wlan0_ssid_probe

        connected_probe_ok, connected_ssid = await asyncio.to_thread(active_wlan0_ssid_probe)
    except Exception:  # noqa: BLE001 - fail-soft: the applier fields still answer
        log.debug("wifi-station-state: live SSID probe raised", exc_info=True)
        connected_probe_ok, connected_ssid = False, None
    return {
        "state": state.state,
        "detail": state.detail,
        "ssid": state.ssid,
        "connected_ssid": connected_ssid,
        "connected_probe_ok": connected_probe_ok,
    }


@router.get("")
async def get_settings(storage: SettingsDep) -> dict[str, Any]:
    """Load current settings (pure read; no side effects).

    Bundle C item 1 (2026-05-25): the first-run wifi prefill side-
    effect that used to live here is now an explicit opt-in via
    `POST /api/settings/wifi-prefill`. The threat: a pre-shipment
    attacker between device-power-on and the operator's home-WiFi
    handshake could set a captive-portal password, then GET
    /api/settings to harvest the operator's home PSK that the Pi
    had already persisted from /etc/wpa_supplicant/wpa_supplicant.
    conf. Making the prefill explicit (the operator must call POST)
    closes that window.
    """
    settings = storage.load()
    return _redact_secrets(settings.model_dump())


@router.post("/wifi-prefill")
async def post_wifi_prefill(storage: SettingsDep) -> dict[str, Any]:
    """Pull the operator's saved home-WiFi creds from
    /etc/wpa_supplicant/wpa_supplicant.conf into settings.

    Explicit replacement for the prior `GET /api/settings` side-
    effect (Bundle C item 1, 2026-05-25). Auth: inherits the
    middleware's bearer-token gate via the `/api/settings/*` path
    (NOT whitelisted in auth_middleware.py). The operator must
    deliberately call this; passive harvesting is closed.

    Responses:
      - 200 + {"prefilled": true, "wifi_station_ssid": "..."} on
        success. Password is NOT echoed -- this endpoint persists
        it but never returns it on the wire.
      - 404 if `read_system_wifi()` returns None (no readable
        wpa_supplicant.conf, no active connection, or unrecognized
        format).
      - 409 if `wifi_station_ssid` is already set -- don't clobber
        operator-configured values.
      - 422 if the prefilled values fail SystemSettings validation
        (charset / length / etc).
    """
    settings = storage.load()
    if (settings.wifi_station_ssid or "").strip():
        raise HTTPException(
            status_code=409,
            detail="wifi_station_ssid is already configured; refusing to clobber",
        )
    # Batch 6.2: read_system_wifi spawns iwgetid (up to 2s) and
    # reads /etc/wpa_supplicant -- both blocking. Offload to a
    # worker thread so the POST doesn't stall the event loop for
    # any concurrent /api/playback/state poll.
    creds = await asyncio.to_thread(read_system_wifi)
    if creds is None:
        raise HTTPException(
            status_code=404,
            detail="no saved wifi credentials found on the device",
        )
    ssid, psk = creds
    try:
        updated = settings.model_copy(
            update={
                "wifi_station_enabled": True,
                "wifi_station_ssid": ssid,
                "wifi_station_password": psk,
            }
        )
        # Round-trip through the validator so field-level rules
        # (length, charset) get enforced before we persist.
        SystemSettings.model_validate(updated.model_dump())
    except Exception as exc:
        log.exception(
            "wifi-prefill: validation failed for SSID %r; leaving settings untouched",
            ssid,
        )
        raise HTTPException(
            status_code=422,
            detail="prefilled credentials failed validation",
        ) from exc
    storage.save(updated)
    log.info(
        "wifi-prefill: applied SSID %r from system wpa_supplicant.conf",
        ssid,
    )
    # Password deliberately NOT in the response -- caller doesn't
    # need it back, and not echoing keeps it out of any caller-side
    # log / network trace.
    return {"prefilled": True, "wifi_station_ssid": ssid}


# 20.4: PUT accepts a raw dict so we can substitute the redaction
# sentinel ("<set>") for each secret field BEFORE Pydantic validates the
# field-specific regex / length rules. The substitution rule:
#
#   payload[secret] == "<set>"   -> use stored value (no change)
#   payload[secret] == any other -> validate + persist as-given
#
# The dispatch's "restrict PUT, force PATCH for secrets" model is a
# stricter cousin of this: the substitution lets the UI keep doing a
# liberal GET-mutate-PUT round-trip (carrying the redacted sentinel
# back without rewriting client-side), while actual rotations land
# through the PATCH endpoints below. The sentinel can never end up
# persisted because it won't pass the wifi_password regex anyway --
# the sentinel branch is the only path the UI exercises in practice.


@router.put("")
async def set_settings(
    payload: dict[str, Any],
    storage: SettingsDep,
    content_storage: ContentDep,
    loop: LoopDep,
) -> dict[str, Any]:
    previous = storage.load()
    # 20.4: replace the sentinel for each secret field with the stored
    # value -- the UI rehydrates redacted GET responses into the form
    # and echoes them back on Save, so the sentinel is what the wire
    # sees most of the time. Any other value (including null/empty
    # for the nullable fields) flows through to Pydantic validation
    # below.
    for key in SECRET_FIELDS:
        if payload.get(key) == SECRET_SENTINEL:
            payload[key] = getattr(previous, key)
    # 2026-07-16 (qarl, JasonsSign1 renamed itself backward to
    # fireplaceSign): an ABSENT `sign_name` means "the operator did not
    # touch the name field", so keep the stored value.
    #
    # This PUT is a full-object replace and the panel autosaves the WHOLE
    # payload on any input event, so a tab loaded before an out-of-band
    # rename holds the OLD name and re-submits it when the operator
    # touches an unrelated field. `previous.sign_name != validated.
    # sign_name` then reads as an intentional rename and fires
    # `hostnamectl set-hostname <old name>` (plus tailnet / AP SSID /
    # mDNS, which all follow it). Nudging a brightness slider on a stale
    # tab renamed the sign backward -- that is the journal's
    # `hostname changed from "JasonsSign1" to "fireplaceSign"`.
    #
    # The server cannot tell a stale echo from a deliberate rename by
    # value alone, so the CLIENT now omits the field unless the operator
    # edited it, and absence is the signal. Same shape as the secret
    # sentinel above: a value the UI is only carrying, not authoring, is
    # replaced by the stored one. Note the asymmetry with every other
    # field here -- absence normally means "take the Pydantic default",
    # which for sign_name would be a rename to the default name.
    #
    # The two halves are COUPLED, not belt-and-braces: an old client still
    # sends the key, so this branch never fires for it and the stale-echo
    # rename still lands -- the client half is what fixes the bug, and this
    # is what makes the new client's omission safe. The skew that bites is
    # the inverse: a cached NEW bundle against a rolled-back backend omits
    # the field and `default_factory=_default_sign_name` mints a random
    # `Sign<3-hex>`. deploy.sh ships both together, so that's rollback-only.
    if "sign_name" not in payload:
        payload["sign_name"] = previous.sign_name
    # Same treatment, same reason (2026-07-16): `tailscale_enabled` gates
    # openmarquee-tailscale.service at every boot, so a stale tab autosaving
    # an unticked box would take a working node off the tailnet — the lane we
    # use to reach signs we cannot physically touch. The client sends it only
    # when the operator actually clicked the toggle. Absence keeps the stored
    # value, so an /api/system/tailscale/up that just persisted True can't be
    # undone by a tab that predates it.
    if "tailscale_enabled" not in payload:
        payload["tailscale_enabled"] = previous.tailscale_enabled
    # Same again (2026-07-16): `wifi_ssid` is the setup-AP SSID and FOLLOWS
    # the sign name -- _reconcile_stored_name_fields rewrites it from the
    # hostname whenever it drifts (qarl's Option A). The field is readonly in
    # the UI now and the panel no longer sends it, which is what stops a
    # stale tab echoing an old SSID.
    #
    # THIS branch exists for the other direction -- the new client's
    # OMISSION. Without it an absent key takes the field's plain default,
    # "openMarquee-SETUP", so a save that never mentioned the network would
    # silently rename the sign's setup AP away from its reconciled value AND
    # hand that wrong name to wifi_networks_actuator as `ap_ssid` below,
    # weakening one of the three signals it uses to recognise (and protect)
    # the setup-AP NM profile. Not sign_name's random-name trap -- the
    # default here is a constant -- but a silent rename either way.
    if "wifi_ssid" not in payload:
        payload["wifi_ssid"] = previous.wifi_ssid
    # 2026-07-03 (qarl handover B1): per-entry SECRET_SENTINEL swap
    # for `wifi_networks[i].password`. The UI submits `<set>` for a
    # network whose PSK the operator hasn't retyped (the response was
    # already masked); we look up the previously-stored PSK by SSID
    # so the round-trip stores the same value as before. A brand-new
    # network entry (SSID not in `previous.wifi_networks`) sending
    # `<set>` is invalid — that means the UI never had a real PSK to
    # display; leave the sentinel in place and let Pydantic reject
    # it with the "expected 8-63 printable ASCII" error.
    incoming_networks = payload.get("wifi_networks")
    if isinstance(incoming_networks, list):
        prev_psk_by_ssid = {entry.ssid: entry.password for entry in previous.wifi_networks}
        for entry in incoming_networks:
            if not isinstance(entry, dict):
                continue
            if entry.get("password") == SECRET_SENTINEL:
                stored = prev_psk_by_ssid.get(entry.get("ssid"))
                if stored is not None:
                    entry["password"] = stored
    try:
        validated = SystemSettings.model_validate(payload)
    except Exception as exc:
        # Re-raise as 422 so the FastAPI top-level handler emits the
        # standard {detail: [...]} shape. We don't reach for
        # RequestValidationError here because the payload type is
        # dict (FastAPI's auto-422 only fires on typed bodies); a
        # plain HTTPException at this layer keeps the wire shape
        # consistent with what existing tests assert.
        #
        # 11.2 / sweep #5 #8: don't reflect Pydantic's raw exception
        # text -- the message includes the failing payload value which
        # can include secrets-passed-through-by-UI. Generic detail; log
        # carries the field-level reason for the operator's audit trail.
        # #379 follow-up: log via _scrubbed_error_summary so the rejected
        # value never lands in journald either.
        log.warning("settings PUT validation failed: %s", _scrubbed_error_summary(exc))
        raise HTTPException(status_code=422, detail="settings validation failed") from exc
    # Compare the dim-affecting fields BEFORE the save so we know whether
    # to fire the text-slide rerender.
    dims_changed = (
        int(previous.display_rotation) != int(validated.display_rotation)
        or int(previous.display_width) != int(validated.display_width)
        or int(previous.display_height) != int(validated.display_height)
    )
    # FYS bug 5: rotation is the only display field the renderer reacts
    # to (its mode comes from the DRM panel, not Settings width/height).
    rotation_changed = int(previous.display_rotation) != int(validated.display_rotation)
    # Wifi-station fields: BEFORE save so we can diff. The actual
    # apply (nmcli connection delete + nmcli device wifi connect on
    # wlan0 + status polling) runs in a background thread so the
    # HTTP response returns immediately. UI polls GET
    # /api/settings/wifi-station-state for live status. (Pre-2026-05
    # this was a wpa_supplicant-wlan0.conf template + systemctl
    # restart; the wifi_station.py history block documents the
    # Pi OS Lite trixie NetworkManager rewrite.)
    wifi_station_changed = wifi_station.has_settings_changed(
        prev_enabled=previous.wifi_station_enabled,
        prev_ssid=previous.wifi_station_ssid,
        prev_password=previous.wifi_station_password,
        new_enabled=validated.wifi_station_enabled,
        new_ssid=validated.wifi_station_ssid,
        new_password=validated.wifi_station_password,
    )
    storage.save(validated)
    # Dim change handling (DELETE-PIL Option α, qarl-direct 2026-05-13):
    # we used to call text_rerender.rerender_text_slides_for_dims here
    # to re-rasterize every stored TextSlide at the new dims. With the
    # PIL rasterizer gone, the device's playback engine cover-fits at
    # panel-native dims when displaying the operator's last in-browser
    # bake — the visible drift is bounded (slight pixel-density change)
    # and corrects fully on the next operator save in the editor.
    _ = dims_changed  # width/height deltas: no renderer action needed
    # FYS bug 5: the renderer reads display rotation at Open time. On a
    # rotation change, restart the renderer so it re-Opens with the new
    # rotation — stop the playback loop first so nothing drives the
    # renderer during the reopen, then restart it (it re-fetches +
    # re-begins the current slide against the freshly reopened
    # sidecar). reopen() is sync (subprocess relaunch + IPC round
    # trips) — run it off the event loop so other requests aren't
    # blocked. Rotation is a set-once-at-install setting, so the
    # one-time playback blip is acceptable.
    if rotation_changed:
        await loop.stop()
        try:
            await asyncio.to_thread(loop.renderer.reopen)
        except Exception:
            log.exception("settings: renderer reopen after rotation change failed")
        await loop.start()
    if wifi_station_changed:
        wifi_station.apply_in_background(
            enabled=validated.wifi_station_enabled,
            ssid=validated.wifi_station_ssid,
            password=validated.wifi_station_password,
        )
        # 2026-07-08 (P0-1): drive the network state machine off the
        # Settings provisioning path too — not just the captive-portal
        # (api_onboarding.submit_credentials) path. Without this, saving
        # station creds via Settings applies the nmcli connect but leaves
        # the supervisor in SETUP, so the AP-teardown + CONNECTING/LINGER
        # confirmation cards never fire on the first session (they only
        # self-correct on the next reboot). Mirror submit_credentials:
        # stamp the target SSID, then fire HAS_STORED_CREDENTIALS
        # (SETUP → CONNECTING; a no-op from other states). Only when creds
        # are actually present + station enabled. Fail-soft — a supervisor
        # hiccup must never 500 the settings PATCH.
        if (
            validated.wifi_station_enabled
            and validated.wifi_station_ssid
            and validated.wifi_station_password
        ):
            try:
                from openmarquee.dependencies import get_network_supervisor
                from openmarquee.network_supervisor import SupervisorEvent, SupervisorState

                supervisor = get_network_supervisor()
                # Only advance from the states where fresh provisioning is
                # meaningful (SETUP → CONNECTING; DEGRADED = a retry) — mirror
                # api_onboarding.submit_credentials. From ONLINE/CONNECTING/
                # LINGER a re-save must NOT re-trigger onboarding; the nmcli
                # reconnect + the supervisor's own observe loop handle those.
                if supervisor.current_state in (
                    SupervisorState.SETUP,
                    SupervisorState.DEGRADED,
                ):
                    supervisor.record_target_ssid(validated.wifi_station_ssid)
                    supervisor.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
            except Exception:
                log.warning(
                    "settings: supervisor provisioning-event dispatch failed; "
                    "station creds applied but SETUP won't advance until next boot",
                    exc_info=True,
                )
    # 2026-07-03 (qarl handover B1): wifi_networks change → run
    # the nmcli reconcile in a background thread. Compare the
    # normalised model dumps so a re-save that didn't actually touch
    # any network doesn't trigger a redundant reconcile.
    if [n.model_dump() for n in previous.wifi_networks] != [
        n.model_dump() for n in validated.wifi_networks
    ]:
        from openmarquee import wifi_networks_actuator

        wifi_networks_actuator.apply_in_background(
            list(validated.wifi_networks), ap_ssid=validated.wifi_ssid
        )
    # 2026-07-03 (qarl handover B1): sign_name change → propagate to
    # hostname / Tailscale / setup-AP SSID / mDNS. All four sub-
    # actuators are fail-soft and run in a background thread so a
    # failed systemctl restart never wedges the PUT.
    if previous.sign_name != validated.sign_name:
        from openmarquee import name_actuator

        name_actuator.apply_in_background(validated.sign_name)
    return _redact_secrets(validated.model_dump())


# --- 20.4 / phase A.4 — PATCH endpoints for the 3 secret fields ---
#
# Each endpoint validates the operator's CURRENT login password against
# the stored AuthState before allowing the rotation. The middleware
# already requires a valid bearer token; the current-password re-auth
# is the second factor for sensitive operations -- a stolen token
# can't rotate the AP password unless it also has the operator's
# current password.
#
# Body shape is uniform across the three endpoints so the UI can share
# a single Change-form handler:
#     {"current_password": "<operator pw>", "new_value": "<new secret>"}
# new_value:
#   - non-empty string: validated by the field's own Pydantic check
#     (regex / length); rejection comes back as 422
#   - empty string / null: clears the field. Only valid for
#     wifi_station_password + tailscale_auth_key (their type is
#     `str | None`); the AP password field rejects empty values
#     because the AP must always carry a passphrase.


class _PatchSecretRequest(BaseModel):
    current_password: str
    new_value: str | None = Field(default=None)


async def _verify_current_password(
    auth: AuthStorage,
    authorization: str | None,
    current_password: str,
) -> None:
    """Re-auth gate. Verifies BOTH the bearer token AND the
    operator's current password against the stored AuthState.

    Raises HTTPException(401) on any failure. The middleware should
    have rejected before we get here on a missing/invalid bearer, but
    belt-and-suspenders: don't trust unauth callers past this point.

    Async because verify_token is async (argon2.verify offloaded to
    a thread per the 2026-05-24 finding 1 Path A fix).
    """
    state = auth.load()
    if state is None:
        raise HTTPException(status_code=404, detail="auth not configured")
    if not await verify_token(_strip_bearer(authorization), state):
        raise HTTPException(status_code=401, detail="invalid token")
    if not verify_password(state.password_hash, current_password):
        raise HTTPException(status_code=401, detail="current password is wrong")


def _patch_secret_field(
    storage: SettingsStorage,
    field: str,
    new_value: str | None,
) -> dict[str, Any]:
    """Update one secret field on the stored SystemSettings, validating
    via the model's own field validators (8-63 passphrase chars, etc.).

    Returns the redacted settings dump so the caller sees its update
    landed.
    """
    current = storage.load()
    try:
        updated = current.model_copy(update={field: new_value})
        # Round-trip through the validator so the field's regex /
        # length rules fire. A bad new_value lands as ValidationError
        # which FastAPI turns into a 422 via the top-level exception
        # handler.
        SystemSettings.model_validate(updated.model_dump())
    except Exception as exc:
        # 11.2 / sweep #5 #8: scrub exception text from the response.
        # new_value is a secret rotation candidate (AP password / station
        # password / Tailscale auth key) and Pydantic's error message
        # quotes the failing input verbatim. Generic detail; log captures
        # the field name + reason (NOT the value).
        # #379 follow-up: _scrubbed_error_summary uses
        # exc.errors(include_input=False) so the journald sink is also
        # safe -- anyone with shell on the Pi can `journalctl` otherwise.
        log.warning(
            "settings PATCH validation failed (field=%s): %s",
            field,
            _scrubbed_error_summary(exc),
        )
        raise HTTPException(status_code=422, detail="secret field validation failed") from exc
    storage.save(updated)
    return _redact_secrets(updated.model_dump())


@router.patch("/wifi-ap-password")
async def patch_wifi_ap_password(
    payload: _PatchSecretRequest,
    storage: SettingsDep,
    auth: AuthDep,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Rotate the captive-portal AP WPA2 passphrase. Cannot be cleared --
    the AP must always carry a passphrase (the wifi_password field is
    a non-None `str` in SystemSettings)."""
    await _verify_current_password(auth, authorization, payload.current_password)
    if not payload.new_value:
        raise HTTPException(status_code=422, detail="wifi_password cannot be empty")
    return _patch_secret_field(storage, "wifi_password", payload.new_value)


@router.patch("/wifi-station-password")
async def patch_wifi_station_password(
    payload: _PatchSecretRequest,
    storage: SettingsDep,
    auth: AuthDep,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Rotate the operator's home-WiFi passphrase. Empty/null clears --
    valid when wifi_station_enabled=false. The model's
    _check_wifi_has_at_least_one_mode_enabled validator catches the
    "station enabled but no creds" combination."""
    await _verify_current_password(auth, authorization, payload.current_password)
    # Empty string → null per the model's _check_station_password
    # validator. Both shapes resolve to "clear the field".
    new_value = payload.new_value or None
    previous = storage.load()
    response = _patch_secret_field(storage, "wifi_station_password", new_value)
    # If station mode is enabled and the password actually changed,
    # re-issue the nmcli connect with the new credentials on wlan0
    # (delete + recreate the wifi-station connection profile under
    # NetworkManager). Background thread so the PATCH returns
    # immediately; UI polls /api/settings/wifi-station-state for
    # live status. (Pre-2026-05 this re-templated a wpa_supplicant
    # conf + restarted wpa_supplicant; the wifi_station.py history
    # block documents the rewrite.)
    if previous.wifi_station_enabled and previous.wifi_station_password != new_value:
        latest = storage.load()
        wifi_station.apply_in_background(
            enabled=latest.wifi_station_enabled,
            ssid=latest.wifi_station_ssid,
            password=latest.wifi_station_password,
        )
        # QA verify-audit 2026-07-15: the ACTUAL Settings UI provisions the
        # home-WiFi passphrase via THIS PATCH endpoint, not the inline-password
        # PUT (api_settings.put). The P0-1 (2026-07-08) state-machine wiring was
        # added only to PUT — so a first-setup / re-provision through the real
        # UI applied the nmcli connect but left the supervisor in SETUP: the
        # AP-teardown + CONNECTING/LINGER confirmation cards never fired on the
        # first session (self-corrects only on the next reboot). Mirror the PUT
        # / submit_credentials path: stamp the target SSID, then fire
        # HAS_STORED_CREDENTIALS (SETUP → CONNECTING; a no-op from ONLINE/
        # CONNECTING/LINGER). Only when creds are actually present + station
        # enabled. Fail-soft — a supervisor hiccup must never 500 the PATCH.
        if (
            latest.wifi_station_enabled
            and latest.wifi_station_ssid
            and latest.wifi_station_password
        ):
            try:
                from openmarquee.dependencies import get_network_supervisor
                from openmarquee.network_supervisor import SupervisorEvent, SupervisorState

                supervisor = get_network_supervisor()
                # Advance only from states where fresh provisioning is
                # meaningful (SETUP → CONNECTING; DEGRADED = a retry). From
                # ONLINE/CONNECTING/LINGER a re-save must NOT re-trigger
                # onboarding — the nmcli reconnect + the supervisor's own
                # observe loop handle those.
                if supervisor.current_state in (
                    SupervisorState.SETUP,
                    SupervisorState.DEGRADED,
                ):
                    supervisor.record_target_ssid(latest.wifi_station_ssid)
                    supervisor.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
            except Exception:
                log.warning(
                    "settings: supervisor provisioning-event dispatch failed "
                    "(PATCH station password); creds applied but SETUP won't "
                    "advance until next boot",
                    exc_info=True,
                )
    return response


@router.patch("/tailscale-auth-key")
async def patch_tailscale_auth_key(
    payload: _PatchSecretRequest,
    storage: SettingsDep,
    auth: AuthDep,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Rotate the Tailscale pre-auth key. Empty/null clears (common --
    a successfully-consumed key has no reason to stay on disk)."""
    await _verify_current_password(auth, authorization, payload.current_password)
    new_value = payload.new_value or None
    return _patch_secret_field(storage, "tailscale_auth_key", new_value)


# ---------------------------------------------------------------------------
# Option D (2026-07-14, qarl's owner decision): reveal a saved WiFi
# network's plaintext PSK, per-network + on-demand. Re-auth-gated on EVERY
# call (fresh bearer + current password; nothing cached). The action is
# audit-logged (action + SSID + outcome only, NEVER the PSK). The read
# crosses the root netctl daemon (the backend is NoNewPrivileges).
# ---------------------------------------------------------------------------


class _RevealPasswordRequest(BaseModel):
    current_password: str


def _audit_reveal(ssid: str, outcome: str) -> None:
    """Emit one audit line for a WiFi-password reveal attempt. Records the
    SSID (not a secret — it's the visible network name) + the outcome
    (revealed / not-stored / denied-unknown-ssid / error). NEVER logs the
    PSK value."""
    _audit_log.info("AUDIT wifi-reveal-password ssid=%r outcome=%s", ssid, outcome)


@router.post("/network/{ssid}/reveal-password")
async def reveal_network_password(
    ssid: str,
    payload: _RevealPasswordRequest,
    storage: SettingsDep,
    auth: AuthDep,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Reveal the plaintext PSK for ONE saved WiFi network.

    Re-auth-gated on EVERY call (verifies the bearer token AND the
    operator's current login password; nothing is session-cached — each
    reveal is a fresh re-auth). The SSID must be one of the operator's
    own saved networks; this both authorises the call and serves as the
    known-profile-set check (the SSID is never used to steer nmcli
    directly — the connection name is resolved from NetworkManager's own
    enumerate). Every attempt is audit-logged (action + SSID + outcome,
    never the PSK).

    Returns ``{ssid, stored, password}``:
      * stored=true, password=<psk> — the device has the PSK.
      * stored=false, password=null — the profile exists but the device
        never captured a secret (the Layer-A ``<hidden>`` case) or it's
        an open network. The UI shows a "not stored on-device" state,
        distinct from "hidden".
    """
    # Audit a failed re-auth too — a secret-reveal endpoint is exactly
    # where a brute-force attempt should leave a trail. The attacker-
    # supplied ssid is not a secret; the outcome is 'denied-auth'.
    try:
        await _verify_current_password(auth, authorization, payload.current_password)
    except HTTPException:
        _audit_reveal(ssid, "denied-auth")
        raise
    settings = storage.load()
    if not any(n.ssid == ssid for n in settings.wifi_networks):
        _audit_reveal(ssid, "denied-unknown-ssid")
        raise HTTPException(status_code=404, detail="no such saved network")
    # Import lazily (matches the actuator's netctl-call pattern) + run the
    # blocking socket round-trip off the event loop.
    from openmarquee.wifi_networks_actuator import (
        RevealSecretError,
        reveal_secret_for_ssid,
    )

    try:
        psk = await asyncio.to_thread(reveal_secret_for_ssid, ssid)
    except RevealSecretError:
        # The typed error carries only nmcli transport text (never the
        # PSK); don't propagate its message to the client.
        _audit_reveal(ssid, "error")
        raise HTTPException(
            status_code=502,
            detail="could not read the secret from NetworkManager",
        ) from None
    except Exception:
        # Fail CLOSED for any unexpected error on the reveal path: audit +
        # a clean 502 rather than a raw 500. No PSK is present on an
        # exception path, so this can't leak — it just keeps the audit
        # trail complete and the response generic.
        _audit_reveal(ssid, "error")
        raise HTTPException(
            status_code=502,
            detail="could not read the secret from NetworkManager",
        ) from None
    if psk is None:
        _audit_reveal(ssid, "not-stored")
        return {"ssid": ssid, "stored": False, "password": None}
    _audit_reveal(ssid, "revealed")
    return {"ssid": ssid, "stored": True, "password": psk}
