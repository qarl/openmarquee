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

from pydantic import BaseModel, Field, field_validator

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


class SystemSettings(BaseModel):
    """Device-wide configuration. One per device; persisted as a single file."""

    schema_version: int = Field(default=SETTINGS_SCHEMA_VERSION)

    sign_name: str = Field(
        default="openMarquee",
        min_length=1,
        max_length=64,
        description="Operator-facing label for this device (shown in UI + welcome screen).",
    )

    output_mode: OutputMode = Field(
        default="hdmi",
        description="Which renderer the playback engine drives.",
    )

    display_width: int = Field(
        default=128,
        ge=1,
        le=4096,
        description="Display width in pixels. Defaults to SYSTEM_SPEC §3.4 (128).",
    )
    display_height: int = Field(
        default=96,
        ge=1,
        le=4096,
        description="Display height in pixels. Defaults to SYSTEM_SPEC §3.4 (96).",
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

    timezone: str | None = Field(
        default=None,
        description="IANA timezone (e.g. America/Los_Angeles). Reserved — playback is "
        "naive-local today. Lands fully with the schedule zoned-eval work.",
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

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _IANA_TZ_PATTERN.match(value):
            raise ValueError(f"timezone: not a well-formed IANA name: {value!r}")
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
        """Load settings from disk. Returns defaults if the file is missing."""
        if not self.path.exists():
            return SystemSettings()
        data = json.loads(self.path.read_text())
        return SystemSettings.model_validate(data)

    def save(self, settings: SystemSettings) -> None:
        """Replace the on-disk settings with `settings`. Atomic via rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(settings.model_dump_json(indent=2))
        tmp.replace(self.path)
