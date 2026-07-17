"""Device system settings — model + persistence.

Covers the device-configurable fields from SYSTEM_SPEC §3.4 and §5.4:
sign name, output mode, display geometry, brightness, WiFi AP creds,
gamma, and (reserved) timezone.

This module ships the data model + storage contract only. Several fields
are *persisted now but acted on later*:

- `wifi_password` in production needs a hostapd rewrite + restart — that
  lives in Phase 7. Today we just store. `wifi_ssid` is NO LONGER in that
  bucket and hasn't been since 2026-07-03: `name_actuator._apply_hostapd_ssid`
  rewrites hostapd.conf + restarts it. But it is driven by the SIGN NAME, not
  by this field — the setup-AP SSID follows the sign name (qarl's Option A),
  and `_reconcile_stored_name_fields` rewrites `wifi_ssid` from the hostname
  on drift. So this field is DERIVED state that records what the AP is
  broadcasting; editing it never did anything, which is why the Settings box
  is readonly as of 2026-07-16.
- `output_mode` is HDMI-only on HEAD. Legacy on-disk values from the
  HUB75 / WS2812B / composite era are coerced to "hdmi" on load (see
  `_coerce_legacy_output_mode`); the Rust IPC sidecar owns HDMI scanout
  on production and is the only path the playback loop drives.
- `brightness` is read by the active renderer at playback time.

Storing today means operators can configure their device once, and
each phase's hardware work just reads the already-validated value out
of settings.json instead of inventing its own ad-hoc config path.
"""

import json
import logging
import re
import secrets
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from openmarquee._atomic import atomic_write_text
from openmarquee._storage_recovery import quarantine_corrupt_file

log = logging.getLogger(__name__)

# Bump when the on-disk format changes in a non-backward-compatible way.
# Same migration discipline as `openmarquee.schedule` and
# `openmarquee.content.storage`.
SETTINGS_SCHEMA_VERSION = 1

OutputMode = Literal["hdmi"]

# IEEE 802.11 SSIDs are up to 32 octets. We accept printable ASCII to sidestep
# the hostapd escaping dance — operators who want emoji SSIDs are outside the
# MVP scope.
_SSID_PATTERN = re.compile(r"^[\x20-\x7e]{1,32}$")

# WPA2-PSK passphrases are 8-63 ASCII chars (or a 64-hex-char raw PSK). We
# accept the passphrase form only. The UI preserves the current value on edits
# rather than sending "" as a "no change" sentinel — keeping the on-disk
# representation literal avoids the "empty means ?" ambiguity.
_WIFI_PASSPHRASE_PATTERN = re.compile(r"^[\x20-\x7e]{8,63}$")

# IANA timezones: letters, digits, underscore, slash, hyphen, plus. Not an
# exhaustive check — the device's `zoneinfo` will reject unknown zones at
# actual use time — but it catches obvious garbage at the API boundary.
_IANA_TZ_PATTERN = re.compile(r"^[A-Za-z0-9_+/\-]{1,64}$")

# Tailscale pre-auth keys are `tskey-auth-...`; OAuth client keys are
# `tskey-client-...`. Operators paste one of these; the shape isn't worth
# hard-enforcing (Tailscale may add variants), but the prefix catches
# obvious mistakes (random password pasted into the wrong field).
_TAILSCALE_AUTH_KEY_PATTERN = re.compile(r"^tskey-[a-z]+-[A-Za-z0-9\-]{8,}$")

# RFC 1123 hostname chars, 1-63 per label, no trailing/leading hyphen.
_TAILSCALE_HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$")


def _default_sign_name() -> str:
    """Mint a `Sign<3-hex>` name (e.g. `SignA7F`) on first device boot.

    The factory fires from SystemSettings()'s default when no value is
    supplied. SettingsStorage.load() persists the fresh defaults on
    first access so subsequent reloads return the same name — without
    that save-on-miss, every boot would be a different name.
    """
    import secrets

    return f"Sign{secrets.token_hex(2)[:3].upper()}"


# 2026-07-03 (qarl handover): DNS-safe subset for `sign_name` — the
# device name propagates to hostname (hostnamectl), Tailscale hostname
# (tailscale set --hostname), the setup-AP SSID, and the mDNS
# host-name. All four consumers demand DNS-safe chars, so we validate
# the field at the API boundary rather than letting the actuators
# fail one-by-one on a slash / colon / emoji.
#
# RFC 1123 hostname chars, 1-63 per label, no trailing/leading
# hyphen — same shape as _TAILSCALE_HOSTNAME_PATTERN, hoisted so
# sign_name shares the rule.
_SIGN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$")


class WifiNetworkEntry(BaseModel):
    """2026-07-03 (qarl handover): one home wifi network the device
    should try to join. A list of these lives on
    `SystemSettings.wifi_networks` and is reconciled against
    NetworkManager connection profiles.

    Ownership convention (see `wifi_networks_actuator` module
    docstring for full detail): profiles auto-created by the
    reconcile use the connection name `openmarquee-<ssid>`, and
    the ownership predicate is the BROAD `openmarquee-` prefix.
    That means Jason's device's pre-existing `openmarquee-sign-
    wifi` / `openmarquee-mgmt-wifi` / `openmarquee-admin-wifi`
    profiles ARE adopted + managed by the reconcile — deleting
    them requires them to fall out of `wifi_networks` on a PUT.
    Hand-added `nmcli con` profiles WITHOUT the `openmarquee-`
    prefix stay outside the ownership predicate and are never
    deleted by the reconcile.

    Passwords are WRITE-ONLY at the API boundary — the redactor
    replaces every populated `password` with the `<set>` sentinel
    on responses, and the PUT handler swaps `<set>` for the stored
    value before validation. Plaintext PSK never leaves the process.
    """

    model_config = ConfigDict(extra="allow")

    ssid: str = Field(
        ...,
        description="Home WiFi SSID (1-32 printable ASCII chars).",
    )
    password: str | None = Field(
        default=None,
        description=(
            "WPA2 passphrase (8-63 printable ASCII chars). WRITE-ONLY: "
            "GET / PUT responses redact this to `<set>` when populated "
            "or omit it when empty; the PUT handler substitutes `<set>` "
            "back with the stored value before validation."
        ),
    )
    autoconnect: bool = Field(
        default=True,
        description=(
            "Whether NetworkManager should auto-join this network when "
            "in range. Set False for a network the operator wants "
            "manual control over."
        ),
    )
    priority: int = Field(
        default=0,
        ge=-999,
        le=999,
        description=(
            "NetworkManager `connection.autoconnect-priority`. Higher "
            "priority wins when multiple saved networks are in range. "
            "Range clamped so a typo can't render the field invalid."
        ),
    )

    @field_validator("ssid")
    @classmethod
    def _check_ssid(cls, value: str) -> str:
        if not _SSID_PATTERN.match(value):
            raise ValueError(
                f"wifi_networks[].ssid: expected 1-32 printable ASCII chars, got {value!r}"
            )
        return value

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _WIFI_PASSPHRASE_PATTERN.match(value):
            raise ValueError("wifi_networks[].password: expected 8-63 printable ASCII chars")
        return value


class SystemSettings(BaseModel):
    """Device-wide configuration. One per device; persisted as a single file."""

    # Round-12 forward-compat fix (2026-05-25): preserve unknown
    # top-level fields across the storage round-trip.
    #
    # PUT /api/settings reads JSON, redaction-substitutes the three
    # secret fields, then SystemSettings.model_validate(payload)
    # (api_settings.py:305). The UI's Save button does GET -> mutate
    # -> PUT; without extra="allow" the validate step silently
    # dropped any field this build didn't know about. SettingsStorage.
    # save then wrote the lossy model back to disk via
    # model_dump_json -- data gone forever.
    #
    # Operator scenario: downgrade to older bundle; settings.json
    # carries a forward-compat field from N+1 (e.g.
    # display_color_profile="p3"); operator nudges brightness + Save;
    # the field is gone; on re-upgrade it defaults and operator
    # re-tunes from scratch.
    #
    # In Pydantic v2 with extra="allow":
    #   - validate_python / model_validate preserve extras into
    #     model_extra
    #   - model_copy(update={...}) preserves extras
    #   - model_dump / model_dump_json emit extras alongside known
    #     fields
    # So load -> mutate -> save round-trips cleanly with no other
    # code changes (the PUT handler at api_settings.py:287-345 needs
    # zero changes; same shape as the r11 ContentItem fix).
    #
    # The existing _coerce_legacy_output_mode model_validator
    # (mode="before") below still removes the 3 retired keys
    # (ws281x_pixel_order, web_helper_url, web_helper_token) BEFORE
    # validation runs -- so those don't end up in model_extra and
    # don't get re-emitted by save. Forward-compat keys pass through
    # the strip + land in model_extra + get persisted. The strip was
    # documentary-only before (extra="ignore" would drop them anyway);
    # post-r12 it is load-bearing.
    #
    # Same shape as the Schedule + Playlist + ContentItem forward-
    # compat fixes; this closes the SystemSettings leg. Flock is the
    # last one in the series (r13).
    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(default=SETTINGS_SCHEMA_VERSION)

    sign_name: str = Field(
        default_factory=_default_sign_name,
        min_length=1,
        max_length=64,
        description="Operator-facing label for this device (shown in UI + welcome screen).",
    )

    flock_sync_enabled: bool = Field(
        default=True,
        description=(
            "Global kill switch for flock participation. When false, this "
            "device stops pushing local changes, stops pulling from peers, "
            "and drops inbound /api/flock/notify posts. Peer tiles keep "
            "their per-peer sync flag but no bytes flow."
        ),
    )

    ui_first_run_seen: bool = Field(
        default=False,
        description=(
            "Whether the operator has dismissed the captive-portal "
            "first-run welcome screen. Flipped to true the first time "
            "they tap 'Make it mine' so subsequent visits go straight "
            "to the editor. Independent from the seed marker (which "
            "tracks content seeding, not UI dismissals)."
        ),
    )

    output_mode: OutputMode = Field(
        default="hdmi",
        description="Which renderer the playback engine drives.",
    )

    # Defaults match the default `output_mode` (HDMI → 1920×1080). A
    # fresh install shouldn't render HDMI-mode playback into a HUB75-sized
    # canvas — the UI has output-mode → default-dims snapping for mode
    # changes, but the initial paint before any change needs the backend
    # default to already be mode-consistent.
    display_width: int = Field(
        default=1920,
        ge=1,
        le=4096,
        description="Display width in pixels. Default matches HDMI (1920).",
    )
    display_height: int = Field(
        default=1080,
        ge=1,
        le=4096,
        description="Display height in pixels. Default matches HDMI (1080).",
    )
    # Physical panel dims above describe the hardware's native orientation;
    # `display_rotation` is how the operator has physically mounted it.
    # The renderer rotates the engine's logical frames before pushing to
    # hardware, and the editor's preview canvases swap aspect when rotation
    # is 90° or 270° so operators see what the installed sign actually
    # shows.
    display_rotation: Literal[0, 90, 180, 270] = Field(
        default=0,
        description="Clockwise rotation applied to rendered frames on the way "
        "to hardware. 0 = native landscape; 90/270 = portrait mounting.",
    )

    brightness: int = Field(
        default=80,
        ge=0,
        le=100,
        description="Renderer-applied brightness, 0-100. Meaning is renderer-specific.",
    )

    gamma: float = Field(
        default=1.0,
        ge=0.1,
        le=3.0,
        description=(
            "Display gamma correction. 1.0 is identity (no second gamma "
            "applied; assets arrive sRGB-encoded). Operator can dial up "
            "if the TV's HDMI pipeline isn't gamma-correct on its own."
        ),
    )

    # Captive-portal access point — this is how phones connect during
    # setup. Default on; disabling requires station mode to be on
    # instead so the device isn't network-isolated.
    wifi_ap_enabled: bool = Field(
        default=True,
        description="Broadcast the openMarquee captive-portal WiFi network.",
    )
    wifi_ssid: str = Field(
        default="openMarquee-SETUP",
        description="Access-point SSID (1-32 printable ASCII chars).",
    )
    # Per-process random AP passphrase. SYSTEM_SPEC §4.1 originally
    # specified the literal "openmarquee" as the default + had Phase 7's
    # first-boot rotation swap it for a per-device random string. Bundle
    # C item 5 (2026-05-25): a dev-Pi that hasn't run the firstboot
    # rotation was a free re-auth target within RF range of the literal
    # default. token_urlsafe(16) closes the residual so the default is
    # NEVER the literal on any boot path. Production firstboot still
    # overwrites this with its own per-device persisted random; this
    # factory only changes the PRE-firstboot exposure window.
    wifi_password: str = Field(
        default_factory=lambda: secrets.token_urlsafe(16),
        description="WPA2 passphrase (8-63 printable ASCII chars).",
    )

    # Station mode — join an existing WiFi. Opt-in. Enables Tailscale +
    # anything else that needs internet. Runs concurrently with the AP
    # on the Pi Zero 2 W's single radio (same channel; see SYSTEM_SPEC
    # §4.1). Empty creds are allowed while the toggle is off.
    #
    # LEGACY single-network fields — retained for read-only backward
    # compatibility during rolling deploys + persisted-settings.json
    # migration. New code paths (API + UI) key off `wifi_networks`
    # below. On load, `_migrate_legacy_wifi_station_fields` converts
    # any populated single-network shape into a one-entry
    # `wifi_networks` list; the resulting SystemSettings then clears
    # the legacy trio so a save-back round-trip persists only the
    # canonical list. See `_migrate_legacy_wifi_station_fields`
    # docstring for the full migration rationale.
    wifi_station_enabled: bool = Field(
        default=False,
        description="DEPRECATED (2026-07-03): use `wifi_networks` (a non-empty "
        "list implies the same 'STA mode wants to run' semantic).",
    )
    wifi_station_ssid: str | None = Field(
        default=None,
        description="DEPRECATED (2026-07-03): use `wifi_networks[i].ssid`.",
    )
    wifi_station_password: str | None = Field(
        default=None,
        description="DEPRECATED (2026-07-03): use `wifi_networks[i].password`.",
    )

    # 2026-07-03 (qarl handover): multi-network home wifi. Each entry
    # in this list becomes an NM connection profile on the device.
    # `_wifi_station_actuator.apply_networks` reconciles the list
    # against nmcli's `openmarquee-managed-<i>` connection names
    # (add / update / delete) so the on-disk NM state matches the
    # settings exactly + doesn't clobber the setup-AP profile or
    # Tailscale.
    #
    # Passwords are WRITE-ONLY at the API boundary: `_redact_secrets`
    # masks each `.password` on every GET / PUT response, and the
    # PUT handler substitutes the `<set>` sentinel back with the
    # stored value before validation so the UI can round-trip a
    # network without ever seeing plaintext PSK.
    wifi_networks: list[WifiNetworkEntry] = Field(
        default_factory=list,
        description=(
            "Home WiFi networks the device should try to join, in "
            "priority order. Each entry becomes an NM connection "
            "profile; passwords are write-only (masked in every API "
            "response)."
        ),
    )
    # 2026-07-03 (qarl handover B1): one-shot boot flag set to True
    # by SettingsStorage.load() after it runs the NM-import path
    # (see `wifi_networks_actuator.import_existing_wifi_profiles`).
    # This idempotency guard means the import fires AT MOST once
    # per device — an operator who intentionally deletes every
    # network via the dashboard won't get them silently re-imported
    # on the next boot from stale nmcli state.
    wifi_networks_seeded_from_nm: bool = Field(
        default=False,
        description=(
            "One-shot flag: True once the boot-time NM-import path "
            "has run for this device (whether it found profiles or "
            "not). Prevents re-import after the operator intentionally "
            "clears the list via the dashboard."
        ),
    )

    timezone: str | None = Field(
        default=None,
        description="IANA timezone (e.g. America/Los_Angeles). Drives both the "
        "on-screen clock and schedule evaluation (a 09:00-17:00 rule fires by "
        "this wall clock, not the device process clock). Unset → naive local.",
    )

    # --- Tailscale (optional remote management) ---
    #
    # Enabling provisions the device onto the operator's tailnet so they
    # can reach the captive-portal UI from outside the AP (useful for
    # managing a sign remotely). Requires the device to have internet at
    # install time — either via secondary WiFi or Ethernet. All three
    # fields are stored now; the oneshot systemd unit that reads them +
    # runs `tailscale up` lands with Phase 7.
    tailscale_enabled: bool = Field(
        default=False,
        description="Bring up the Tailscale daemon on boot.",
    )
    tailscale_auth_key: str | None = Field(
        default=None,
        description=(
            "Pre-authorized Tailscale auth key (tskey-auth-… or "
            "tskey-client-…). Used once at device bring-up; can be cleared "
            "after the node authenticates."
        ),
    )
    tailscale_hostname: str | None = Field(
        default=None,
        description=(
            "DNS-safe hostname the device registers under on the tailnet. "
            "Defaults to the operating-system hostname when unset."
        ),
    )
    tailscale_https_enabled: bool = Field(
        default=True,
        description=(
            "Provision HTTPS via `tailscale serve` when tailscale_enabled. "
            "Tailscale-issued Let's Encrypt cert covers the FQDN "
            "(`<hostname>.<tailnet>.ts.net`) only; short MagicDNS names + "
            "LAN IPs continue working on plain HTTP. When True, the backend "
            "also 301-redirects non-FQDN/non-LAN requests to the canonical "
            "HTTPS FQDN so operator bookmarks resolve to a secure context."
        ),
    )
    network_fallback_mutex_mode: bool = Field(
        default=False,
        description=(
            "P1.1 onboarding-rework (2026-06-10): fallback flag for "
            "the network-supervisor. When False (default), the "
            "supervisor runs the spec's preferred concurrent regime "
            "(AP + STA share the radio's single channel; AP follows "
            "STA channel; LINGER grace window). When True, the "
            "supervisor enforces the comitup pattern: AP and STA are "
            "mutually exclusive (AP off when STA up). QA can flip "
            "this at runtime without reinstall if firmware bugs "
            "surface in concurrent mode. See "
            "docs/onboarding-rework-plan.md §C.2 + spec §"
            '"Fallback position".'
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_wifi_station_fields(cls, data: object) -> object:
        """2026-07-03 (qarl handover): migrate the single-network
        `wifi_station_ssid` / `wifi_station_password` shape into a
        one-entry `wifi_networks` list. Fires BEFORE field validation
        so a settings.json persisted pre-multi-wifi loads cleanly on
        upgrade + a save-back round-trip persists only the canonical
        list shape.

        Idempotent: a payload that already carries `wifi_networks` is
        left untouched, and if the migration WOULD produce a duplicate
        entry (same SSID as an existing wifi_networks[i]) it's a
        no-op. The legacy fields stay in the payload so back-compat
        readers see them; a downstream `model_validator(mode="after")`
        could zero them, but the low-risk path is to preserve for
        now + let a follow-up PR retire the fields once the UI has
        rolled out.

        No-ops if `data` isn't a dict (a bare dict is the only
        settings-load shape; anything else is Pydantic's problem).
        """
        if not isinstance(data, dict):
            return data
        legacy_ssid = data.get("wifi_station_ssid")
        legacy_password = data.get("wifi_station_password")
        existing_networks = data.get("wifi_networks") or []
        if not legacy_ssid or not legacy_password:
            return data
        # Idempotent: if the legacy SSID already appears in the list
        # (as a dict OR a fully-validated WifiNetworkEntry), skip.
        for entry in existing_networks:
            entry_ssid = (
                entry.get("ssid") if isinstance(entry, dict) else getattr(entry, "ssid", None)
            )
            if entry_ssid == legacy_ssid:
                return data
        migrated = [
            {
                "ssid": legacy_ssid,
                "password": legacy_password,
                "autoconnect": True,
                "priority": 0,
            },
            *existing_networks,
        ]
        # Return a shallow copy with the merged list so we don't
        # mutate the caller's payload (matters when settings storage
        # keeps its own reference for a subsequent save).
        return {**data, "wifi_networks": migrated}

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_output_mode(cls, data: object) -> object:
        """Coerce legacy on-disk output_mode values to "hdmi".

        DELETE-PIL purge collapsed OutputMode to ["hdmi"] only. A
        settings.json saved before this purge may carry output_mode
        in {"hub75", "ws281x", "composite"}; without coercion,
        Pydantic would reject the file at load and the device would
        boot into a fresh-default state (losing wifi/Tailscale/etc.).
        Coerce silently -- LED hardware is offline regardless of the
        stored value; the Rust IPC sidecar drives HDMI on production.

        Other unknown values (e.g. "vga", "displayport") still raise
        ValidationError -- this migration is scoped to the specific
        legacy set, not a universal coerce-all.
        """
        _LEGACY_LED_OUTPUT_MODES = {"hub75", "ws281x", "composite"}
        if isinstance(data, dict):
            mode = data.get("output_mode")
            if mode in _LEGACY_LED_OUTPUT_MODES:
                data = {**data, "output_mode": "hdmi"}
            # Strip dropped settings keys a legacy settings.json may
            # still carry: ws281x_pixel_order (DELETE-PIL purge), and
            # web_helper_url / web_helper_token (retired when the Web
            # slide switched to on-device rendering). Pydantic's default
            # extra="ignore" would silently drop them anyway, but we are
            # explicit here so the migrations stay grep-able.
            dropped = {
                "ws281x_pixel_order",
                "web_helper_url",
                "web_helper_token",
            }
            if data.keys() & dropped:
                data = {k: v for k, v in data.items() if k not in dropped}
        return data

    @field_validator("wifi_ssid")
    @classmethod
    def _check_ssid(cls, value: str) -> str:
        if not _SSID_PATTERN.match(value):
            raise ValueError(f"wifi_ssid: expected 1-32 printable ASCII chars, got {value!r}")
        return value

    @field_validator("sign_name")
    @classmethod
    def _check_sign_name(cls, value: str) -> str:
        """2026-07-03 (qarl handover): sign_name propagates to hostname
        (hostnamectl), Tailscale hostname (tailscale set --hostname),
        the setup-AP SSID, and the mDNS host-name. All four consumers
        need DNS-safe characters.

        This validator NORMALIZES rather than rejects so a legacy
        settings.json seeded with `"Lobby Sign"` or `"Coffee Shop"`
        loads cleanly and the propagation actuators see a DNS-safe
        value. Rules:
          * Whitespace runs collapse to a single `-`.
          * Any char outside `[A-Za-z0-9-]` is dropped.
          * Leading / trailing `-` stripped (RFC 1123 label rule).
          * Empty result → ValueError (something has to remain).
          * Result over 63 chars → truncated.
        A perfectly DNS-safe input is returned unchanged.
        """
        # Preserve the empty-string reject — the min_length=1 field
        # constraint would already fire, but keeping this branch
        # first makes the error message specific.
        if not value:
            raise ValueError("sign_name: must be non-empty")
        # Collapse whitespace to hyphen so "Lobby Sign" → "Lobby-Sign".
        normalized = re.sub(r"\s+", "-", value)
        # Drop everything outside the RFC 1123 label alphabet.
        normalized = re.sub(r"[^A-Za-z0-9\-]", "", normalized)
        # Strip leading + trailing hyphens (RFC 1123 label rule).
        normalized = normalized.strip("-")
        # Truncate to 63 chars (DNS label limit).
        normalized = normalized[:63]
        if not normalized:
            raise ValueError(
                "sign_name: no DNS-safe characters left after "
                "normalisation. Use letters/digits/hyphen "
                f"(input was {value!r})."
            )
        if not _SIGN_NAME_PATTERN.match(normalized):
            # Defense-in-depth: the normalisation above should always
            # produce a matching string, but a bug in the regex here
            # is easier to catch than a downstream actuator failure.
            raise ValueError(
                f"sign_name normalisation produced {normalized!r} which "
                "still doesn't match the DNS-safe pattern"
            )
        return normalized

    @field_validator("wifi_password")
    @classmethod
    def _check_wifi_password(cls, value: str) -> str:
        if not _WIFI_PASSPHRASE_PATTERN.match(value):
            raise ValueError("wifi_password: expected empty or 8-63 printable ASCII chars")
        return value

    @field_validator("wifi_station_ssid")
    @classmethod
    def _check_station_ssid(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _SSID_PATTERN.match(value):
            raise ValueError(
                f"wifi_station_ssid: expected 1-32 printable ASCII chars, got {value!r}"
            )
        return value

    @field_validator("wifi_station_password")
    @classmethod
    def _check_station_password(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _WIFI_PASSPHRASE_PATTERN.match(value):
            raise ValueError("wifi_station_password: expected 8-63 printable ASCII chars")
        return value

    @model_validator(mode="after")
    def _check_wifi_has_at_least_one_mode_enabled(self) -> "SystemSettings":
        """Disabling both AP and station leaves the device network-
        isolated — captive portal dies AND remote management dies. The
        UI gates against this; this validator is the server-side
        belt-and-braces so a manual settings.json edit can't brick a
        deployed device.

        2026-07-03 (qarl handover): "STA enabled" now means
        `wifi_networks` is non-empty OR the legacy
        `wifi_station_enabled` toggle is still on (back-compat during
        rolling deploys). The legacy sub-clause preserving
        wifi_station_ssid/password remains — a settings.json still
        writing the legacy fields shouldn't produce an unreachable
        device just because it hasn't migrated to the list yet.
        """
        sta_active = bool(self.wifi_networks) or self.wifi_station_enabled
        if not self.wifi_ap_enabled and not sta_active:
            raise ValueError(
                "at least one of wifi_ap_enabled or wifi_networks / "
                "wifi_station_enabled must be true — disabling every "
                "network path would leave the device unreachable"
            )
        # Legacy sub-clause: if wifi_station_enabled is on (a payload
        # that predates multi-wifi rollout), it still needs creds.
        if self.wifi_station_enabled and not self.wifi_networks:
            if not self.wifi_station_ssid:
                raise ValueError("wifi_station_enabled=true but wifi_station_ssid is empty")
            if not self.wifi_station_password:
                raise ValueError("wifi_station_enabled=true but wifi_station_password is empty")
        return self

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _IANA_TZ_PATTERN.match(value):
            raise ValueError(f"timezone: not a well-formed IANA name: {value!r}")
        return value

    @field_validator("tailscale_auth_key")
    @classmethod
    def _check_tailscale_auth_key(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _TAILSCALE_AUTH_KEY_PATTERN.match(value):
            raise ValueError(
                "tailscale_auth_key: expected a tskey-auth-… / tskey-client-… "
                "string from the Tailscale admin console"
            )
        return value

    @field_validator("tailscale_hostname")
    @classmethod
    def _check_tailscale_hostname(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _TAILSCALE_HOSTNAME_PATTERN.match(value):
            raise ValueError(
                "tailscale_hostname: expected DNS-safe 1-63 chars "
                "(letters, digits, hyphens; no leading/trailing hyphen)"
            )
        return value


class SettingsStorage:
    """Persists `SystemSettings` as a single JSON file with atomic writes.

    Mirrors `ScheduleStorage` — same on-disk shape (Pydantic model dump),
    same write discipline (sibling .tmp + rename), same "refuse to read
    if the envelope's schema_version doesn't match what code expects"
    semantics baked in via Pydantic validation.
    """

    # Perf counters (Batch 6.1). See ContentStorage._stats comment.
    _stats: dict[str, int] = {"load_calls": 0, "save_calls": 0}

    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def stats_snapshot(cls) -> dict[str, int]:
        return dict(cls._stats)

    @staticmethod
    def _wifi_nmcli_available() -> bool:
        """Return True when nmcli is on PATH — used to gate the
        one-shot boot-time import path. Extracted so tests can
        monkeypatch it without touching subprocess."""
        import shutil

        return shutil.which("nmcli") is not None

    def load(self) -> SystemSettings:
        type(self)._stats["load_calls"] += 1
        """Load settings from disk. On first access (no file yet) we mint
        fresh defaults AND persist them — otherwise sign_name's random
        factory would hand out a different name on every reload and the
        UI + the flock peer list would flap.

        Derives wifi_ssid from sign_name's XXX suffix so the AP and the
        device name share an identifier ("SignA7F" + "openMarqueeA7F").
        """
        if not self.path.exists():
            fresh = SystemSettings()
            updates: dict[str, str] = {}
            # qarl 2026-05-12 (a2): the factory-stamped MySignXXX
            # device_id (set by openmarquee-firstboot.sh) is the
            # IMMUTABLE infrastructure identifier. Anchor sign_name +
            # wifi_ssid + tailscale_hostname against it at first-load:
            #   - sign_name defaults to device_id but stays operator-
            #     editable (it's the display label, not the
            #     infrastructure ID)
            #   - wifi_ssid = device_id (mirrors the firstboot-written
            #     hostapd.conf, single source of truth)
            #   - tailscale_hostname = device_id (lowercased; device_id
            #     is [A-Z0-9] so lowercase is DNS-safe). Pinning to
            #     device_id, not sign_name, means renaming the
            #     display label doesn't churn magic-DNS.
            # If identity.json isn't present (off-device dev), fall
            # back to the legacy Sign<3-hex> path so existing dev
            # flows keep working.
            from openmarquee import identity as _identity

            device_id = _identity.read_device_id()
            if device_id is not None:
                updates["sign_name"] = device_id
                updates["wifi_ssid"] = device_id
                # TODO (arc 4 -- Tailscale URL-auth): tailscale_hostname
                # is currently a first-load mirror of device_id.lower()
                # but stays operator-editable in the wire shape. Arc 4
                # locks this down: tailscale_hostname becomes a
                # derived-read-only field anchored to device_id so
                # magic-DNS never churns.
                updates["tailscale_hostname"] = device_id.lower()
            else:
                if fresh.sign_name.startswith("Sign") and len(fresh.sign_name) > 4:
                    suffix = fresh.sign_name[4:]
                    updates["wifi_ssid"] = f"openMarquee{suffix}"
                # Bug B15 (qarl batch 2026-04-29): Tailscale hostname
                # starts empty, so the first-run UI surfaces a blank
                # field. Pre-fill with lowercased sign_name (DNS-safe
                # by Sign<3-hex> construction).
                updates["tailscale_hostname"] = fresh.sign_name.lower()
            if updates:
                fresh = fresh.model_copy(update=updates)
            self.save(fresh)
            return fresh
        # 19.2 / sweep #10 #4: on parse / schema failure quarantine
        # the bad file + return defaults; the next save() rewrites
        # the original path. settings.json carries the AP password
        # and Tailscale auth key -- the quarantine .corrupt-<ISO>
        # file inherits the 0600 mode (preserved by rename) so the
        # secrets don't leak through the recovery path.
        try:
            data = json.loads(self.path.read_text())
            settings = SystemSettings.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            quarantine_corrupt_file(self.path, exc)
            fresh = SystemSettings()
            self.save(fresh)
            return fresh
        # 2026-07-03 (qarl handover B1): one-shot import-from-NM path.
        # On the Jason device the 3 existing wifi profiles
        # (openmarquee-sign-wifi, openmarquee-mgmt-wifi,
        # openmarquee-admin-wifi) live in NM but not in
        # settings.wifi_networks. `import_existing_wifi_profiles`
        # walks nmcli's connection list + adopts them (reading their
        # PSKs) so the dashboard's future "Wi-Fi networks" section
        # shows the 3 already-programmed networks. Guarded by
        # `wifi_networks_seeded_from_nm` so an operator who
        # intentionally deletes every network via the dashboard
        # doesn't get them silently re-imported on the next boot.
        # Fails soft: nmcli missing / non-zero produces an empty
        # import list + still flips the flag so we don't retry
        # indefinitely.
        if not settings.wifi_networks_seeded_from_nm:
            settings = self._seed_wifi_networks_from_nm(settings)
        return settings

    def _seed_wifi_networks_from_nm(self, settings: SystemSettings) -> SystemSettings:
        """One-shot: adopt NM's currently-configured wifi profiles
        into settings.wifi_networks. Sets `wifi_networks_seeded_
        from_nm=True` ONLY when the nmcli probe actually succeeded
        so the boot-time overhead is bounded to once but a
        transient failure retries next boot.

        Dev hosts without nmcli LEAVE THE FLAG FALSE + DON'T
        PERSIST so an eventual deploy to a real device still runs
        the import cleanly — and so unit tests don't get an
        implicit save-mutation of every load. Extracted as a
        method so tests can monkeypatch either
        `_wifi_nmcli_available` or `import_existing_wifi_profiles`.

        2026-07-03 (QA HARDEN B): `import_existing_wifi_profiles`
        now returns `(probe_ok, entries)`. Only flip the seeded
        flag when `probe_ok=True` — a transient nmcli error means
        we don't know whether there are inactive fallback profiles
        (openmarquee-qarl-wifi, openmarquee-admin-wifi) that
        aren't in settings.wifi_networks yet, and flipping the
        flag would let a subsequent PUT-triggered reconcile delete
        them (they'd be openmarquee-owned + inactive + missing
        from the settings list → deletable per the reconcile
        algorithm). Retrying next boot is cheap; losing the
        fallbacks is not.
        """
        if not self._wifi_nmcli_available():
            return settings  # dev host — no flip, no save
        from openmarquee.wifi_networks_actuator import import_existing_wifi_profiles

        probe_ok = False
        imported: list[dict[str, object]] = []
        try:
            probe_ok, imported = import_existing_wifi_profiles(ap_ssid=settings.wifi_ssid)
        except Exception:  # noqa: BLE001 — fail-soft on the whole hook
            log.exception("wifi-networks-import: unexpected error; skipping seed")
        # De-duplicate against any already-present entries (idempotency
        # belt-and-suspenders — if the migration validator already
        # added an entry for the same SSID, the import won't clobber).
        existing_ssids = {n.ssid for n in settings.wifi_networks}
        merged = list(settings.wifi_networks)
        for entry_dict in imported:
            if entry_dict["ssid"] in existing_ssids:
                continue
            try:
                merged.append(WifiNetworkEntry.model_validate(entry_dict))
            except ValidationError as exc:
                log.warning(
                    "wifi-networks-import: skipping malformed profile ssid=%r: %r",
                    entry_dict.get("ssid"),
                    exc,
                )
                continue
        if not probe_ok:
            # 2026-07-03 (QA HARDEN B): probe failed — do NOT flip
            # the seeded flag so we retry on the next boot. Also
            # don't persist a partial-merge, since a later successful
            # probe should re-seed atomically.
            log.info(
                "wifi-networks-import: nmcli probe failed; leaving "
                "wifi_networks_seeded_from_nm=False so next boot retries",
            )
            return settings
        updated = settings.model_copy(
            update={"wifi_networks": merged, "wifi_networks_seeded_from_nm": True}
        )
        # Persist so subsequent boots don't re-run the import.
        try:
            self.save(updated)
        except OSError:
            log.exception("wifi-networks-import: failed to persist seeded settings")
        return updated

    def save(self, settings: SystemSettings) -> None:
        """Replace the on-disk settings with `settings`. Atomic via rename."""
        type(self)._stats["save_calls"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 11.2: atomic_write_text sets 0600 + cleans up orphan .tmp.
        # settings.json carries the AP password, station password,
        # and (future) Tailscale auth key -- the 0600 mode is
        # specifically why this helper exists.
        atomic_write_text(self.path, settings.model_dump_json(indent=2))
