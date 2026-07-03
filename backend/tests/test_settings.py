"""Unit tests for the SystemSettings model + SettingsStorage."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from openmarquee.settings import (
    SETTINGS_SCHEMA_VERSION,
    SettingsStorage,
    SystemSettings,
    WifiNetworkEntry,
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


def test_output_mode_accepts_hdmi():
    s = SystemSettings(output_mode="hdmi")
    assert s.output_mode == "hdmi"


def test_output_mode_legacy_led_values_coerce_to_hdmi():
    """DELETE-PIL purge collapsed OutputMode to ["hdmi"] only. Legacy
    settings.json files that stored "hub75" / "ws281x" / "composite"
    must coerce silently rather than fail validation."""
    for legacy in ("hub75", "ws281x", "composite"):
        s = SystemSettings.model_validate({"output_mode": legacy})
        assert s.output_mode == "hdmi"


def test_output_mode_rejects_unknown_non_legacy_mode():
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


def test_wifi_password_default_is_not_the_legacy_literal():
    """Bundle C item 5 (2026-05-25): the legacy literal default
    'openmarquee' was a free re-auth target within RF range of any
    dev-Pi that hadn't run the Phase B firstboot rotation. The new
    default_factory generates a per-process random via
    secrets.token_urlsafe(16). Lock the regression so a future
    refactor that re-introduces the literal trips here."""
    assert SystemSettings().wifi_password != "openmarquee"


def test_wifi_password_default_factory_fires_per_instance():
    """default_factory should produce a fresh random per
    SystemSettings instance -- if it ever degrades to a process-
    level cached value (e.g. someone replacing default_factory with
    default=<computed-once>), two instances would share the same
    password + a previously-leaked dev-Pi default would persist
    across reboots."""
    a = SystemSettings().wifi_password
    b = SystemSettings().wifi_password
    assert a != b


def test_wifi_password_default_factory_has_sane_entropy():
    """token_urlsafe(16) produces ~22 base64url chars (16 bytes of
    entropy). Sanity-check the factory wasn't downgraded to a
    smaller token size that would weaken the residual."""
    pw = SystemSettings().wifi_password
    assert len(pw) >= 22


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
    lowercased sign_name (DNS-safe by construction).

    Off-device (no identity.json) path -- see the next test for the
    on-device (identity.json present) anchoring path."""
    from openmarquee.settings import SettingsStorage

    storage = SettingsStorage(tmp_path / "settings.json")
    loaded = storage.load()
    assert loaded.sign_name.startswith("Sign")
    assert loaded.tailscale_hostname == loaded.sign_name.lower()


def test_storage_anchors_to_device_id_when_identity_json_present(tmp_path, monkeypatch):
    """qarl 2026-05-12 (a2): when identity.json holds a MySignXXX
    device_id (set at first boot), the IMMUTABLE infrastructure fields
    anchor to it instead of the random Sign<XXX> mint:
      - sign_name (display label) defaults to device_id but remains
        operator-editable
      - wifi_ssid = device_id (mirrors firstboot's hostapd.conf)
      - tailscale_hostname = device_id.lower()
    Renaming the display label doesn't churn magic-DNS because we
    pin Tailscale's hostname to device_id, not sign_name."""
    from openmarquee.settings import SettingsStorage

    identity_path = tmp_path / "identity.json"
    identity_path.write_text('{"device_id": "MySign7K2"}')
    monkeypatch.setenv("OPENMARQUEE_IDENTITY_PATH", str(identity_path))

    storage = SettingsStorage(tmp_path / "settings.json")
    loaded = storage.load()
    assert loaded.sign_name == "MySign7K2"
    assert loaded.wifi_ssid == "MySign7K2"
    assert loaded.tailscale_hostname == "mysign7k2"


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
    # HTTPS provisioning defaults ON so operators who opt into
    # Tailscale at all get HTTPS + the FQDN-redirect convenience
    # without an extra settings click. No-op when tailscale_enabled
    # itself is False (the bring-up script skips the `tailscale
    # serve` step entirely).
    assert s.tailscale_https_enabled is True


def test_tailscale_https_enabled_can_be_disabled():
    s = SystemSettings(tailscale_https_enabled=False)
    assert s.tailscale_https_enabled is False


def test_tailscale_https_enabled_roundtrips_through_json():
    original = SystemSettings(sign_name="SignABC", tailscale_https_enabled=False)
    round_tripped = SystemSettings.model_validate_json(original.model_dump_json())
    assert round_tripped.tailscale_https_enabled is False


def test_legacy_settings_without_https_field_defaults_to_true(tmp_path: Path):
    """A settings.json written before tailscale_https_enabled landed
    must still load AND adopt the True default. Pydantic does this
    naturally for missing fields; this test pins the contract so a
    future refactor can't silently flip the legacy migration to
    `False` and silently break HTTPS on already-deployed Pis.
    """
    legacy = {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "sign_name": "SignABC",
        "wifi_ssid": "openMarqueeABC",
        "wifi_password": "12345678",
        "tailscale_enabled": True,
        "tailscale_hostname": "signabc",
    }
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(legacy))
    s = SettingsStorage(p).load()
    assert s.tailscale_enabled is True
    assert s.tailscale_https_enabled is True


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


# --- legacy / dropped settings keys ---


def test_settings_round_trip_preserves_unknown_top_level_fields(tmp_path: Path):
    """Round-12 forward-compat regression: extra top-level fields on
    the on-disk settings.json (forward-compat fields from a newer
    backend bundle) must SURVIVE the load->mutate->save round-trip.

    Pre-fix, SystemSettings.model_validate ran under default
    extra="ignore", so any unknown key was silently dropped on load;
    the next save then wrote the lossy model back to disk via
    model_dump_json -- data gone forever.

    Operator scenario: downgrade to older backend bundle; settings.json
    still carries a forward-compat field from N+1 (e.g.
    display_color_profile="p3"); operator nudges brightness + Save ->
    field gone; re-upgrade defaults the field and operator re-tunes
    from scratch.

    Test: pre-seed settings.json with both a known field and an
    unknown forward-compat field; load via SettingsStorage (mirroring
    the GET path); model_copy with a brightness change (mirroring
    the UI's GET-mutate-PUT sequence); save back via the storage
    (mirroring the PUT handler's storage.save); re-read raw JSON and
    assert the forward-compat field survived.
    """
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sign_name": "Lobby Sign",
                "output_mode": "hdmi",
                "display_width": 1920,
                "display_height": 1080,
                "brightness": 50,
                # Forward-compat field from a future backend bundle. This
                # bundle doesn't know about it; must NOT silently drop.
                "display_color_profile": "p3",
                # Nested-shaped forward-compat to cover a richer payload
                # (some future field might be an object, not a scalar).
                "_future_calibration": {
                    "white_point": [0.95, 1.0, 1.09],
                    "version": "draft-2026-Q3",
                },
            }
        )
    )

    storage = SettingsStorage(path)
    loaded = storage.load()
    # Mirror the PUT handler's load->mutate->save trio (api_settings.py
    # :294-345 does this via model_validate; same effect for the
    # extras-preservation invariant).
    mutated = loaded.model_copy(update={"brightness": 80})
    storage.save(mutated)

    # Re-read raw JSON (NOT through the model) and assert the
    # forward-compat fields survived end-to-end.
    persisted = json.loads(path.read_text())
    assert persisted["brightness"] == 80, "brightness update must take effect (positive baseline)"
    assert persisted["display_color_profile"] == "p3", "scalar forward-compat field must survive"
    assert persisted["_future_calibration"] == {
        "white_point": [0.95, 1.0, 1.09],
        "version": "draft-2026-Q3",
    }, "object-shaped forward-compat field must survive"


def test_legacy_settings_with_web_helper_keys_loads(tmp_path: Path):
    """A settings.json written while the Web slide used an external
    render helper carries web_helper_url / web_helper_token. After the
    switch to on-device rendering those fields are gone — the file must
    still load (the dropped keys are stripped) and re-dump without
    them."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sign_name": "Lobby Sign",
                "output_mode": "hdmi",
                "display_width": 1920,
                "display_height": 1080,
                "web_helper_url": "http://192.168.1.50:8888",
                "web_helper_token": "shared-secret-abc",
            }
        )
    )
    loaded = SettingsStorage(path).load()
    # 2026-07-03 (qarl handover): sign_name normalises whitespace to
    # `-` on load so a legacy "Lobby Sign" value stays load-clean AND
    # becomes DNS-safe automatically for the propagation actuators
    # (hostnamectl / Tailscale / setup-AP SSID / mDNS host-name).
    assert loaded.sign_name == "Lobby-Sign"
    dumped = loaded.model_dump()
    assert "web_helper_url" not in dumped
    assert "web_helper_token" not in dumped


# --- SettingsStorage ---


def test_storage_load_returns_defaults_when_file_absent(tmp_path: Path):
    # sign_name is minted randomly per device; wifi_ssid + tailscale_hostname
    # both derive from it (different format each); wifi_password is a
    # per-instance random token_urlsafe(16) (Bundle C item 5 2026-05-25,
    # previously the literal "openmarquee"). Compare everything else.
    storage = SettingsStorage(tmp_path / "settings.json")
    loaded = storage.load()
    skip = {"sign_name", "wifi_ssid", "tailscale_hostname", "wifi_password"}
    assert loaded.model_dump(exclude=skip) == SystemSettings(sign_name="ignored").model_dump(
        exclude=skip
    )


def test_storage_save_then_load_roundtrip(tmp_path: Path):
    storage = SettingsStorage(tmp_path / "settings.json")
    settings = SystemSettings(
        sign_name="Lobby Sign",
        output_mode="hdmi",
        display_width=1920,
        display_height=1080,
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


# --- Multi-network WiFi (2026-07-03 qarl handover) ----------------------


class TestWifiNetworkEntry:
    """`WifiNetworkEntry` is the per-network shape inside
    `SystemSettings.wifi_networks`. Same charset rules as the legacy
    `wifi_station_ssid/password` fields but with sensible defaults
    for `autoconnect` and `priority` so a minimal add-network form
    (ssid + password) validates cleanly."""

    def test_minimal_entry_has_sane_defaults(self):
        entry = WifiNetworkEntry(ssid="HomeWifi", password="open-sesame")
        assert entry.autoconnect is True  # default: auto-join in range
        assert entry.priority == 0  # neutral priority

    def test_ssid_charset_matches_legacy_pattern(self):
        with pytest.raises(ValidationError, match="ssid"):
            WifiNetworkEntry(ssid="", password="open-sesame")
        # 33 chars exceeds the SSID limit.
        with pytest.raises(ValidationError, match="ssid"):
            WifiNetworkEntry(ssid="A" * 33, password="open-sesame")

    def test_password_charset_matches_legacy_pattern(self):
        # 7 chars is too short for WPA2 (min 8).
        with pytest.raises(ValidationError, match="password"):
            WifiNetworkEntry(ssid="HomeWifi", password="short")
        # None + empty string both allowed (write-only sentinel path).
        assert WifiNetworkEntry(ssid="HomeWifi", password=None).password is None
        assert WifiNetworkEntry(ssid="HomeWifi", password="").password is None

    def test_priority_clamped_to_reasonable_range(self):
        with pytest.raises(ValidationError):
            WifiNetworkEntry(ssid="HomeWifi", password="open-sesame", priority=10_000)


class TestMultiWifiMigration:
    """Legacy settings.json (single `wifi_station_*` fields, no
    `wifi_networks`) migrates cleanly into the list-based shape via
    the `_migrate_legacy_wifi_station_fields` before-validator."""

    def test_legacy_single_wifi_migrates_to_one_entry_list(self):
        legacy = {
            "wifi_station_enabled": True,
            "wifi_station_ssid": "HomeWifi",
            "wifi_station_password": "open-sesame",
        }
        loaded = SystemSettings.model_validate(legacy)
        assert len(loaded.wifi_networks) == 1
        assert loaded.wifi_networks[0].ssid == "HomeWifi"
        assert loaded.wifi_networks[0].password == "open-sesame"
        assert loaded.wifi_networks[0].autoconnect is True
        assert loaded.wifi_networks[0].priority == 0

    def test_legacy_without_creds_is_a_no_op(self):
        """Legacy shape with wifi_station_enabled but blank creds:
        migration adds NOTHING (the payload can't fill a real
        connection profile), and downstream validation surfaces the
        legacy 'missing ssid/password' error."""
        with pytest.raises(ValidationError, match="wifi_station_"):
            SystemSettings.model_validate(
                {
                    "wifi_station_enabled": True,
                    "wifi_station_ssid": None,
                    "wifi_station_password": None,
                }
            )

    def test_migration_is_idempotent_when_list_already_populated(self):
        """A payload that already carries the migrated list AND still
        has the legacy fields (a half-in-flight rolling deploy):
        skip re-adding a duplicate entry, honor the list as-is."""
        payload = {
            "wifi_station_enabled": True,
            "wifi_station_ssid": "HomeWifi",
            "wifi_station_password": "open-sesame",
            "wifi_networks": [
                {"ssid": "HomeWifi", "password": "open-sesame"},
            ],
        }
        loaded = SystemSettings.model_validate(payload)
        assert len(loaded.wifi_networks) == 1

    def test_migration_prepends_when_list_has_a_different_network(self):
        payload = {
            "wifi_station_enabled": True,
            "wifi_station_ssid": "HomeWifi",
            "wifi_station_password": "open-sesame",
            "wifi_networks": [
                {"ssid": "GuestNetwork", "password": "guest-pass"},
            ],
        }
        loaded = SystemSettings.model_validate(payload)
        ssids = [n.ssid for n in loaded.wifi_networks]
        assert "HomeWifi" in ssids
        assert "GuestNetwork" in ssids

    def test_wifi_networks_non_empty_satisfies_at_least_one_mode(self):
        """The cross-field validator now accepts a non-empty
        wifi_networks as "STA mode active" — an AP-off + no legacy
        wifi_station_enabled configuration is valid IFF wifi_networks
        is non-empty."""
        settings = SystemSettings(
            wifi_ap_enabled=False,
            wifi_networks=[WifiNetworkEntry(ssid="HomeWifi", password="open-sesame")],
        )
        assert settings.wifi_ap_enabled is False
        assert len(settings.wifi_networks) == 1

    def test_empty_wifi_networks_and_no_ap_still_rejected(self):
        with pytest.raises(ValidationError, match="wifi_ap_enabled"):
            SystemSettings(wifi_ap_enabled=False)


class TestSignNameNormalisation:
    """`sign_name` propagates to hostname / Tailscale / setup-AP SSID
    / mDNS host-name. The field validator normalises whitespace to
    hyphen + drops non-safe chars rather than rejecting, so a legacy
    "Lobby Sign" seed keeps loading cleanly."""

    def test_spaces_collapse_to_hyphen(self):
        s = SystemSettings(sign_name="Lobby Sign")
        assert s.sign_name == "Lobby-Sign"

    def test_multiple_spaces_collapse_to_single_hyphen(self):
        s = SystemSettings(sign_name="A   B   C")
        assert s.sign_name == "A-B-C"

    def test_unsafe_punctuation_is_dropped(self):
        # Apostrophe + exclamation are neither DNS-safe nor useful,
        # so they get dropped rather than raising. The interior
        # whitespace becomes `-` (whitespace-collapse runs before
        # punctuation-drop) so `"Jason's Sign!"` → `"Jasons-Sign"`.
        s = SystemSettings(sign_name="Jason's Sign!")
        assert s.sign_name == "Jasons-Sign"

    def test_leading_and_trailing_hyphens_stripped(self):
        s = SystemSettings(sign_name="- MySign -")
        assert s.sign_name == "MySign"

    def test_perfectly_safe_input_passes_through_unchanged(self):
        s = SystemSettings(sign_name="JasonsSign1")
        assert s.sign_name == "JasonsSign1"

    def test_empty_after_normalisation_raises(self):
        # A pure-punctuation input has NO safe characters left after
        # normalisation — surface a specific error instead of
        # bricking downstream actuators.
        with pytest.raises(ValidationError, match="DNS-safe"):
            SystemSettings(sign_name="!!!")


class TestSeedWifiNetworksFromNmHardenB:
    """2026-07-03 (QA HARDEN B): the seed-from-NM path must ONLY
    flip `wifi_networks_seeded_from_nm=True` when the nmcli probe
    ACTUALLY succeeded. A transient nmcli error must leave the flag
    False so the next boot re-runs the import — otherwise a
    subsequent PUT triggers a reconcile that deletes the inactive
    fallback profiles (qarl / admin) that lived in NM but aren't
    in settings.wifi_networks yet.
    """

    def _run_seed(self, tmp_path: Path, monkeypatch, *, probe_ok: bool, imported: list):
        """Wire the seed path with a scripted import_existing_wifi_profiles
        response + a stub-True `_wifi_nmcli_available`, then call
        load() (which triggers the seed on the second-and-later
        load, not the first-mint one). Returns (was_flipped,
        was_persisted_during_seed, resulting_settings)."""
        monkeypatch.setattr(
            "openmarquee.settings.SettingsStorage._wifi_nmcli_available",
            lambda _self: True,
        )
        monkeypatch.setattr(
            "openmarquee.wifi_networks_actuator.import_existing_wifi_profiles",
            lambda *_a, **_k: (probe_ok, imported),
        )
        storage = SettingsStorage(tmp_path / "settings.json")
        # First load mints defaults + persists; seed hook does NOT run
        # on the first-mint branch. Discard it, then spy save() before
        # the second load which exercises the seed path.
        storage.load()
        saved_calls: list[SystemSettings] = []
        original_save = storage.save

        def _spy_save(s):
            saved_calls.append(s)
            original_save(s)

        monkeypatch.setattr(storage, "save", _spy_save)
        loaded = storage.load()
        return loaded.wifi_networks_seeded_from_nm, bool(saved_calls), loaded

    def test_flag_flips_when_probe_succeeds_with_profiles(self, tmp_path, monkeypatch):
        """Happy path: nmcli responded, returned entries → flag flips
        + settings persisted."""
        entries = [
            {"ssid": "NEBULA", "password": "psk-nebula-1", "autoconnect": True, "priority": 0}
        ]
        flipped, persisted, loaded = self._run_seed(
            tmp_path, monkeypatch, probe_ok=True, imported=entries
        )
        assert flipped is True
        assert persisted is True
        assert any(n.ssid == "NEBULA" for n in loaded.wifi_networks)

    def test_flag_flips_when_probe_succeeds_with_zero_profiles(self, tmp_path, monkeypatch):
        """Boundary: nmcli responded with genuinely zero wifi profiles.
        Flag flips (nothing more to import; the retry-next-boot loop
        would be wasted work)."""
        flipped, persisted, _ = self._run_seed(tmp_path, monkeypatch, probe_ok=True, imported=[])
        assert flipped is True
        assert persisted is True

    def test_flag_STAYS_false_when_probe_fails(self, tmp_path, monkeypatch):
        """Regression: on a transient nmcli error the import path
        returns (False, []). The seed hook MUST NOT flip the flag,
        MUST NOT persist. Next boot re-runs the import so the
        inactive fallback profiles aren't lost to a later reconcile."""
        flipped, persisted, _ = self._run_seed(tmp_path, monkeypatch, probe_ok=False, imported=[])
        assert flipped is False
        assert persisted is False
