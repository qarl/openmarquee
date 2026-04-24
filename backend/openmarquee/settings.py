"""Device system settings — model + persistence.

Covers the device-configurable fields from SYSTEM_SPEC §3.4 and §5.4:
sign name, output mode, display geometry, brightness, WiFi AP creds,
gamma, and (reserved) timezone.

This module ships the data model + storage contract only. Several fields
are *persisted now but acted on later*:

- Changing `wifi_ssid` / `wifi_password` in production needs a hostapd
  rewrite + restart — that lives in Phase 7. Today we just store.
- `output_mode` picks which renderer the playback loop uses. Until
  Phase 6 (HDMI), Phase 8 (HUB75), and Phase 10 (WS2812B / composite)
  land, the playback loop is always wired to MockRenderer and the stored
  mode is advisory.
- `brightness` is renderer-specific (HUB75 has scan-rate brightness,
  HDMI has framebuffer gamma, WS2812B has per-pixel scaling). The stored
  value is read by the active renderer at playback time.

Storing today means operators can configure their device once, and
each phase's hardware work just reads the already-validated value out
of settings.json instead of inventing its own ad-hoc config path.
"""

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Bump when the on-disk format changes in a non-backward-compatible way.
# Same migration discipline as `openmarquee.schedule` and
# `openmarquee.content.storage`.
SETTINGS_SCHEMA_VERSION = 1

OutputMode = Literal["hdmi", "hub75", "ws281x", "composite"]

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
        default=2.2,
        ge=0.1,
        le=3.0,
        description="Display gamma correction. 2.2 is the sRGB default.",
    )

    # WS2812B strip / matrix wiring. Physical order of LEDs in a matrix
    # built from a strip rarely matches raster order; the operator picks
    # the wiring style here. Only meaningful when output_mode == "ws281x".
    # Mirror of the pixel_map arg on WS2812BRenderer.
    ws281x_pixel_order: Literal["row_major", "serpentine"] = Field(
        default="row_major",
        description=(
            "Physical LED order for the WS2812B strip / matrix. "
            "row_major = raster order; serpentine = rows alternate direction."
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

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> SystemSettings:
        """Load settings from disk. On first access (no file yet) we mint
        fresh defaults AND persist them — otherwise sign_name's random
        factory would hand out a different name on every reload and the
        UI + the flock peer list would flap.

        Derives wifi_ssid from sign_name's XXX suffix so the AP and the
        device name share an identifier ("SignA7F" + "openMarqueeA7F").
        """
        if not self.path.exists():
            fresh = SystemSettings()
            if fresh.sign_name.startswith("Sign") and len(fresh.sign_name) > 4:
                suffix = fresh.sign_name[4:]
                fresh = fresh.model_copy(
                    update={"wifi_ssid": f"openMarquee{suffix}"}
                )
            self.save(fresh)
            return fresh
        data = json.loads(self.path.read_text())
        return SystemSettings.model_validate(data)

    def save(self, settings: SystemSettings) -> None:
        """Replace the on-disk settings with `settings`. Atomic via rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(settings.model_dump_json(indent=2))
        tmp.replace(self.path)
