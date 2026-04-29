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


def test_defaults_match_default_output_mode():
    """Default output_mode is HDMI, so default dims must be the HDMI
    native 1920×1080 — not HUB75's 128×96. Bug report: fresh install
    showed output_mode=HDMI but dims=128×96, which rendered HDMI
    playback into a tiny canvas."""
    s = SystemSettings()
    assert s.output_mode == "hdmi"
    assert s.display_width == 1920
    assert s.display_height == 1080
    assert s.schema_version == SETTINGS_SCHEMA_VERSION


def test_ui_first_run_seen_defaults_to_false():
    """A freshly-flashed device has ui_first_run_seen=false so the
    captive-portal UI shows the welcome screen on first visit. The
    welcome's "Make it mine" button flips it to true."""
    s = SystemSettings()
    assert s.ui_first_run_seen is False
    # And the field round-trips through PUT-shaped JSON.
    s2 = SystemSettings.model_validate({**s.model_dump(), "ui_first_run_seen": True})
    assert s2.ui_first_run_seen is True


def test_defaults_roundtrip_through_json():
    # sign_name uses a random default_factory (minted once per device on
    # first boot) so two bare SystemSettings() instances aren't equal —
    # pin it for this round-trip test.
    original = SystemSettings(sign_name="SignABC")
    dumped = original.model_dump_json()
    round_tripped = SystemSettings.model_validate_json(dumped)
    assert round_tripped == original


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


def test_display_rotation_defaults_to_zero():
    assert SystemSettings().display_rotation == 0


def test_display_rotation_accepts_90_180_270():
    for r in (0, 90, 180, 270):
        s = SystemSettings(display_rotation=r)
        assert s.display_rotation == r


def test_display_rotation_rejects_non_cardinal_angles():
    with pytest.raises(ValidationError):
        SystemSettings(display_rotation=45)  # type: ignore[arg-type]


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
    s = SystemSettings(wifi_ssid="openMarquee-A3F7")
    assert s.wifi_ssid == "openMarquee-A3F7"


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


def test_default_sign_name_is_random_sign_prefix():
    import re

    names = {SystemSettings().sign_name for _ in range(8)}
    # All three chars after "Sign" are uppercase hex.
    assert all(re.fullmatch(r"Sign[0-9A-F]{3}", n) for n in names)
    # Non-trivial entropy — 8 draws shouldn't all collapse to one value.
    assert len(names) > 1


def test_storage_persists_minted_default_on_first_load(tmp_path):
    from openmarquee.settings import SettingsStorage

    path = tmp_path / "settings.json"
    storage = SettingsStorage(path)
    minted = storage.load().sign_name
    # Second load gets the SAME name (not a fresh random) because
    # load() saved the minted defaults on first access.
    assert storage.load().sign_name == minted
    assert path.exists()


def test_storage_derives_wifi_ssid_from_sign_name_on_first_load(tmp_path):
    """AP SSID and sign_name share the same 3-char XXX suffix so the
    operator's device name matches the WiFi name they broadcast."""
    from openmarquee.settings import SettingsStorage

    storage = SettingsStorage(tmp_path / "settings.json")
    loaded = storage.load()
    # sign_name minted as "Sign<XXX>"; ssid should be "openMarquee<XXX>".
    assert loaded.sign_name.startswith("Sign")
    suffix = loaded.sign_name[4:]
    assert loaded.wifi_ssid == f"openMarquee{suffix}"


def test_storage_seeds_tailscale_hostname_from_sign_name_on_first_load(tmp_path):
    """Bug B15 (qarl batch 2026-04-29): the Tailscale hostname field
    started empty on a fresh device, so the first-run UI surfaced a
    blank input. SettingsStorage.load() now pre-fills it with the
    lowercased sign_name (DNS-safe by construction)."""
    from openmarquee.settings import SettingsStorage

    storage = SettingsStorage(tmp_path / "settings.json")
    loaded = storage.load()
    assert loaded.sign_name.startswith("Sign")
    assert loaded.tailscale_hostname == loaded.sign_name.lower()


def test_timezone_accepts_well_formed_iana():
    s = SystemSettings(timezone="America/Los_Angeles")
    assert s.timezone == "America/Los_Angeles"


def test_timezone_normalizes_empty_string_to_none():
    s = SystemSettings(timezone="")
    assert s.timezone is None


def test_timezone_rejects_garbage():
    with pytest.raises(ValidationError):
        SystemSettings(timezone="not a tz; DROP TABLE")


# --- WiFi AP / station toggles ---


def test_wifi_ap_default_on_station_default_off():
    s = SystemSettings()
    assert s.wifi_ap_enabled is True
    assert s.wifi_station_enabled is False


def test_wifi_station_enabled_requires_ssid():
    with pytest.raises(ValidationError) as exc:
        SystemSettings(
            wifi_station_enabled=True,
            wifi_station_password="correct-horse-battery",
        )
    assert "wifi_station_ssid" in str(exc.value)


def test_wifi_station_enabled_requires_password():
    with pytest.raises(ValidationError) as exc:
        SystemSettings(
            wifi_station_enabled=True,
            wifi_station_ssid="home",
        )
    assert "wifi_station_password" in str(exc.value)


def test_wifi_station_accepts_creds_when_enabled():
    s = SystemSettings(
        wifi_station_enabled=True,
        wifi_station_ssid="home-net",
        wifi_station_password="correct-horse-battery",
    )
    assert s.wifi_station_enabled is True
    assert s.wifi_station_ssid == "home-net"


def test_wifi_station_ssid_empty_coerces_to_none():
    s = SystemSettings(wifi_station_ssid="")
    assert s.wifi_station_ssid is None


def test_disabling_both_wifi_modes_is_rejected():
    with pytest.raises(ValidationError) as exc:
        SystemSettings(wifi_ap_enabled=False, wifi_station_enabled=False)
    assert "at least one" in str(exc.value).lower()


def test_ap_off_station_on_is_fine():
    s = SystemSettings(
        wifi_ap_enabled=False,
        wifi_station_enabled=True,
        wifi_station_ssid="home",
        wifi_station_password="correct-horse-battery",
    )
    assert s.wifi_ap_enabled is False
    assert s.wifi_station_enabled is True


# --- Tailscale ---


def test_tailscale_defaults_off():
    s = SystemSettings()
    assert s.tailscale_enabled is False
    assert s.tailscale_auth_key is None
    assert s.tailscale_hostname is None


def test_tailscale_accepts_pre_authorized_auth_key():
    s = SystemSettings(tailscale_auth_key="tskey-auth-abc123XYZ-long-enough")
    assert s.tailscale_auth_key == "tskey-auth-abc123XYZ-long-enough"


def test_tailscale_accepts_client_auth_key():
    s = SystemSettings(tailscale_auth_key="tskey-client-123-456-aaa-bbb")
    assert s.tailscale_auth_key.startswith("tskey-client")


def test_tailscale_rejects_pasted_non_tskey_string():
    with pytest.raises(ValidationError):
        SystemSettings(tailscale_auth_key="password123")


def test_tailscale_auth_key_empty_string_coerces_to_none():
    s = SystemSettings(tailscale_auth_key="")
    assert s.tailscale_auth_key is None


def test_tailscale_hostname_accepts_rfc_safe_name():
    s = SystemSettings(tailscale_hostname="lobby-sign-01")
    assert s.tailscale_hostname == "lobby-sign-01"


def test_tailscale_hostname_rejects_leading_hyphen():
    with pytest.raises(ValidationError):
        SystemSettings(tailscale_hostname="-bad")


def test_tailscale_hostname_rejects_spaces():
    with pytest.raises(ValidationError):
        SystemSettings(tailscale_hostname="lobby sign")


# --- SettingsStorage ---


def test_storage_load_returns_defaults_when_file_absent(tmp_path: Path):
    # sign_name is minted randomly per device; wifi_ssid + tailscale_hostname
    # both derive from it (different format each) — compare everything else.
    storage = SettingsStorage(tmp_path / "settings.json")
    loaded = storage.load()
    skip = {"sign_name", "wifi_ssid", "tailscale_hostname"}
    assert loaded.model_dump(exclude=skip) == SystemSettings(
        sign_name="ignored"
    ).model_dump(exclude=skip)


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
