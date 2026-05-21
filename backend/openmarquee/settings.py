"""Device system settings — model + persistence.

Covers the device-configurable fields from SYSTEM_SPEC §3.4 and §5.4:
sign name, output mode, display geometry, brightness, WiFi AP creds,
gamma, and (reserved) timezone.

This module ships the data model + storage contract only. Several fields
are *persisted now but acted on later*:

- Changing `wifi_ssid` / `wifi_password` in production needs a hostapd
  rewrite + restart — that lives in Phase 7. Today we just store.
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
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from openmarquee._atomic import atomic_write_text
from openmarquee._storage_recovery import quarantine_corrupt_file

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
_TAILSCALE_HOSTNAME_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$"
)


def _default_sign_name() -> str:
    """Mint a `Sign<3-hex>` name (e.g. `SignA7F`) on first device boot.

    The factory fires from SystemSettings()'s default when no value is
    supplied. SettingsStorage.load() persists the fresh defaults on
    first access so subsequent reloads return the same name — without
    that save-on-miss, every boot would be a different name.
    """
    import secrets

    return f"Sign{secrets.token_hex(2)[:3].upper()}"


class SystemSettings(BaseModel):
    """Device-wide configuration. One per device; persisted as a single file."""

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
        description="Display gamma correction. 1.0 is identity (no second gamma applied; assets arrive sRGB-encoded). Operator can dial up if the TV's HDMI pipeline isn't gamma-correct on its own.",
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
    # SYSTEM_SPEC §4.1 specifies "openmarquee" as the default passphrase;
    # Phase 7's first-boot rotation swaps it for a per-device random string.
    wifi_password: str = Field(
        default="openmarquee",
        description="WPA2 passphrase (8-63 printable ASCII chars).",
    )

    # Station mode — join an existing WiFi. Opt-in. Enables Tailscale +
    # anything else that needs internet. Runs concurrently with the AP
    # on the Pi Zero 2 W's single radio (same channel; see SYSTEM_SPEC
    # §4.1). Empty creds are allowed while the toggle is off.
    wifi_station_enabled: bool = Field(
        default=False,
        description="Join the operator's existing WiFi on wlan0.",
    )
    wifi_station_ssid: str | None = Field(
        default=None,
        description="SSID of the home WiFi to join.",
    )
    wifi_station_password: str | None = Field(
        default=None,
        description="Passphrase for the home WiFi (8-63 printable ASCII).",
    )

    timezone: str | None = Field(
        default=None,
        description="IANA timezone (e.g. America/Los_Angeles). Reserved — playback is "
        "naive-local today. Lands fully with the schedule zoned-eval work.",
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
            raise ValueError(
                f"wifi_ssid: expected 1-32 printable ASCII chars, got {value!r}"
            )
        return value

    @field_validator("wifi_password")
    @classmethod
    def _check_wifi_password(cls, value: str) -> str:
        if not _WIFI_PASSPHRASE_PATTERN.match(value):
            raise ValueError(
                "wifi_password: expected empty or 8-63 printable ASCII chars"
            )
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
            raise ValueError(
                "wifi_station_password: expected 8-63 printable ASCII chars"
            )
        return value

    @model_validator(mode="after")
    def _check_wifi_has_at_least_one_mode_enabled(self) -> "SystemSettings":
        """Disabling both AP and station leaves the device network-
        isolated — captive portal dies AND remote management dies. The
        UI gates against this; this validator is the server-side
        belt-and-braces so a manual settings.json edit can't brick a
        deployed device."""
        if not self.wifi_ap_enabled and not self.wifi_station_enabled:
            raise ValueError(
                "at least one of wifi_ap_enabled / wifi_station_enabled "
                "must be true — disabling both network modes would leave "
                "the device unreachable"
            )
        # If station is enabled, it needs credentials to actually connect.
        # Empty creds with the toggle on is a user mistake worth catching.
        if self.wifi_station_enabled:
            if not self.wifi_station_ssid:
                raise ValueError(
                    "wifi_station_enabled=true but wifi_station_ssid is empty"
                )
            if not self.wifi_station_password:
                raise ValueError(
                    "wifi_station_enabled=true but wifi_station_password is empty"
                )
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
            return SystemSettings.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            quarantine_corrupt_file(self.path, exc)
            fresh = SystemSettings()
            self.save(fresh)
            return fresh

    def save(self, settings: SystemSettings) -> None:
        """Replace the on-disk settings with `settings`. Atomic via rename."""
        type(self)._stats["save_calls"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 11.2: atomic_write_text sets 0600 + cleans up orphan .tmp.
        # settings.json carries the AP password, station password,
        # and (future) Tailscale auth key -- the 0600 mode is
        # specifically why this helper exists.
        atomic_write_text(self.path, settings.model_dump_json(indent=2))
