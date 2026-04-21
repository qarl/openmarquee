"""Unit tests for the SystemSettings model + SettingsStorage."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from openmarquee.settings import (
    SETTINGS_SCHEMA_VERSION,
    SettingsStorage,
    SystemSettings,
)


# --- Defaults ---


def test_defaults_match_system_spec_display_size():
    s = SystemSettings()
    assert s.display_width == 128
    assert s.display_height == 96
    assert s.output_mode == "hdmi"
    assert s.schema_version == SETTINGS_SCHEMA_VERSION


def test_defaults_roundtrip_through_json():
    dumped = SystemSettings().model_dump_json()
    round_tripped = SystemSettings.model_validate_json(dumped)
    assert round_tripped == SystemSettings()


# --- Field validation ---


def test_output_mode_accepts_all_four_spec_modes():
    for mode in ("hdmi", "hub75", "ws281x", "composite"):
        s = SystemSettings(output_mode=mode)
        assert s.output_mode == mode


def test_output_mode_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        SystemSettings(output_mode="vga")  # type: ignore[arg-type]


def test_brightness_clamps_at_0_and_100_inclusive():
    assert SystemSettings(brightness=0).brightness == 0
    assert SystemSettings(brightness=100).brightness == 100
    with pytest.raises(ValidationError):
        SystemSettings(brightness=-1)
    with pytest.raises(ValidationError):
        SystemSettings(brightness=101)


def test_gamma_accepts_reasonable_range():
    assert SystemSettings(gamma=1.0).gamma == 1.0
    assert SystemSettings(gamma=2.2).gamma == 2.2
    with pytest.raises(ValidationError):
        SystemSettings(gamma=0.0)
    with pytest.raises(ValidationError):
        SystemSettings(gamma=4.0)


def test_display_dimensions_reject_zero_and_negative():
    with pytest.raises(ValidationError):
        SystemSettings(display_width=0)
    with pytest.raises(ValidationError):
        SystemSettings(display_height=-10)


def test_display_dimensions_reject_absurdly_large():
    with pytest.raises(ValidationError):
        SystemSettings(display_width=99999)


def test_ssid_rejects_empty():
    with pytest.raises(ValidationError):
        SystemSettings(wifi_ssid="")


def test_ssid_rejects_over_32_chars():
    with pytest.raises(ValidationError):
        SystemSettings(wifi_ssid="x" * 33)


def test_ssid_rejects_non_ascii():
    with pytest.raises(ValidationError):
        SystemSettings(wifi_ssid="Café")


def test_ssid_accepts_hyphens_and_digits():
    s = SystemSettings(wifi_ssid="OpenMarquee-A3F7")
    assert s.wifi_ssid == "OpenMarquee-A3F7"


def test_wifi_password_default_matches_system_spec():
    """SYSTEM_SPEC §4.1 pins the shipped default to 'openmarquee'."""
    assert SystemSettings().wifi_password == "openmarquee"


def test_wifi_password_rejects_empty():
    with pytest.raises(ValidationError):
        SystemSettings(wifi_password="")


def test_wifi_password_rejects_too_short():
    with pytest.raises(ValidationError):
        SystemSettings(wifi_password="short")  # 5 chars, under WPA2's 8


def test_wifi_password_rejects_too_long():
    with pytest.raises(ValidationError):
        SystemSettings(wifi_password="x" * 64)  # 64 chars, over WPA2's 63


def test_wifi_password_accepts_valid_length():
    s = SystemSettings(wifi_password="correct-horse-battery")
    assert s.wifi_password == "correct-horse-battery"


def test_sign_name_rejects_empty():
    with pytest.raises(ValidationError):
        SystemSettings(sign_name="")


def test_timezone_accepts_well_formed_iana():
    s = SystemSettings(timezone="America/Los_Angeles")
    assert s.timezone == "America/Los_Angeles"


def test_timezone_normalizes_empty_string_to_none():
    s = SystemSettings(timezone="")
    assert s.timezone is None


def test_timezone_rejects_garbage():
    with pytest.raises(ValidationError):
        SystemSettings(timezone="not a tz; DROP TABLE")


# --- SettingsStorage ---


def test_storage_load_returns_defaults_when_file_absent(tmp_path: Path):
    storage = SettingsStorage(tmp_path / "settings.json")
    assert storage.load() == SystemSettings()


def test_storage_save_then_load_roundtrip(tmp_path: Path):
    storage = SettingsStorage(tmp_path / "settings.json")
    settings = SystemSettings(
        sign_name="Lobby Sign",
        output_mode="hub75",
        display_width=192,
        display_height=64,
        brightness=40,
        wifi_ssid="Lobby-WiFi",
        wifi_password="correct-horse-battery",
        timezone="America/New_York",
    )
    storage.save(settings)
    assert storage.load() == settings


def test_storage_save_is_atomic_no_half_written_files(tmp_path: Path):
    """After a successful save, no .tmp sibling should remain."""
    path = tmp_path / "settings.json"
    storage = SettingsStorage(path)
    storage.save(SystemSettings())
    assert path.exists()
    assert not path.with_name(path.name + ".tmp").exists()


def test_storage_creates_parent_dir(tmp_path: Path):
    path = tmp_path / "nested" / "dirs" / "settings.json"
    storage = SettingsStorage(path)
    storage.save(SystemSettings())
    assert path.exists()


def test_storage_accepts_unknown_schema_version_today(tmp_path: Path):
    """Today `load()` does NOT enforce that on-disk schema_version matches
    SETTINGS_SCHEMA_VERSION — matches the behavior of `ScheduleStorage`.
    Before bumping to v2, replace this test with one that either
    (a) rejects unknown versions, or (b) exercises a migrator. Leaving
    this test here so the silent-accept behavior is explicit, not accidental."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema_version": 999, "output_mode": "hdmi"}))
    storage = SettingsStorage(path)
    loaded = storage.load()
    assert loaded.schema_version == 999
