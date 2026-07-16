"""Unit tests for openmarquee.name_actuator (Phase B1 —
qarl handover 2026-07-03).

`apply_sign_name` propagates the device name to four consumers via
the netctl socket daemon (2026-07-03 QA FIX 1: NoNewPrivileges on
the backend blocks direct subprocess calls to hostnamectl /
tailscale / systemctl, so the actuator ships payloads over the
already-sanctioned root socket instead):

  1. hostnamectl-set-hostname     (name payload)
  2. tailscale-set-hostname       (name payload)
  3. avahi-write-and-restart      (full rendered conf payload)
  4. hostapd-write-and-restart    (full rendered conf payload —
     REUSES the existing subcommand)

2026-07-07 (qarl Option A): `apply_sign_name` also runs a 5th step —
`_reconcile_stored_name_fields` — syncing the stored settings name
fields (sign_name / tailscale_hostname / wifi_ssid) so they can't drift.

Every sub-actuator must FAIL SOFT so a netctl connect error /
non-zero response leaves the caller unharmed.
"""

from __future__ import annotations

import configparser
from pathlib import Path

from openmarquee import name_actuator


class _NetctlSpy:
    """Record all _netctl_send calls made through the actuator.
    Each call captured as (subcommand, payload) tuple."""

    def __init__(self):
        self.calls: list[tuple[str, bytes]] = []
        self.raise_error: type[Exception] | None = None

    def _send(self, subcommand, payload, *, timeout_s=None, error_cls=RuntimeError):
        self.calls.append((subcommand, payload))
        if self.raise_error is not None:
            raise error_cls("simulated netctl failure")


def _install_netctl_spy(monkeypatch) -> _NetctlSpy:
    """Patch the `_netctl_send` import in `openmarquee.network_supervisor_actuator`
    (name_actuator does inline `from … import _netctl_send` inside each
    sub-actuator, so we patch the source module)."""
    spy = _NetctlSpy()
    monkeypatch.setattr(
        "openmarquee.network_supervisor_actuator._netctl_send",
        spy._send,
    )
    return spy


class TestHostnamectlPath:
    def test_sends_hostnamectl_set_hostname_via_netctl(self, monkeypatch):
        spy = _install_netctl_spy(monkeypatch)
        name_actuator._apply_hostnamectl("JasonsSign1")
        assert spy.calls == [("hostnamectl-set-hostname", b"JasonsSign1\n")]

    def test_hostnamectl_fail_soft_on_netctl_error(self, monkeypatch):
        spy = _install_netctl_spy(monkeypatch)
        spy.raise_error = RuntimeError
        # Must not raise even when the crossing fails.
        name_actuator._apply_hostnamectl("JasonsSign1")
        assert spy.calls == [("hostnamectl-set-hostname", b"JasonsSign1\n")]


class TestTailscalePath:
    def test_sends_tailscale_set_hostname_via_netctl(self, monkeypatch):
        spy = _install_netctl_spy(monkeypatch)
        # 2026-07-07: _apply_tailscale_hostname is now strictly idempotent
        # (reads the current tailnet hostname first). Simulate genuine
        # drift so the set fires.
        monkeypatch.setattr(name_actuator, "_current_tailscale_hostname", lambda: "oldname")
        name_actuator._apply_tailscale_hostname("JasonsSign1")
        assert spy.calls == [("tailscale-set-hostname", b"JasonsSign1\n")]

    def test_tailscale_fail_soft_on_netctl_error(self, monkeypatch):
        spy = _install_netctl_spy(monkeypatch)
        spy.raise_error = RuntimeError
        monkeypatch.setattr(name_actuator, "_current_tailscale_hostname", lambda: "oldname")
        name_actuator._apply_tailscale_hostname("JasonsSign1")
        assert spy.calls == [("tailscale-set-hostname", b"JasonsSign1\n")]

    def test_tailscale_strict_noop_when_already_in_sync(self, monkeypatch):
        """The reconcile calls this every boot; the tailnet name is the
        operator's SSH lane. When it already matches (case-insensitively)
        we must NOT re-set it."""
        spy = _install_netctl_spy(monkeypatch)
        monkeypatch.setattr(name_actuator, "_current_tailscale_hostname", lambda: "jasonssign1")
        name_actuator._apply_tailscale_hostname("JasonsSign1")  # case-insensitive
        assert spy.calls == []

    def test_tailscale_skips_when_hostname_unreadable(self, monkeypatch):
        """tailscaled not up yet at boot -> can't confirm drift -> skip
        (retries on the next rename / boot). Never fire a blind set."""
        spy = _install_netctl_spy(monkeypatch)
        monkeypatch.setattr(name_actuator, "_current_tailscale_hostname", lambda: None)
        name_actuator._apply_tailscale_hostname("JasonsSign1")
        assert spy.calls == []


class TestAvahiPath:
    """The avahi sub-actuator reads the existing conf, rewrites the
    host-name line locally, and ships the rewritten conf as payload."""

    def test_no_op_when_conf_missing(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        monkeypatch.setattr(
            "openmarquee.name_actuator._AVAHI_CONF",
            tmp_path / "avahi-daemon.conf",  # doesn't exist
        )
        name_actuator._apply_avahi_hostname("JasonsSign1")
        assert spy.calls == []

    def test_rewrites_and_ships_full_conf(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\n#host-name=openmarquee\ndomain-name=local\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        name_actuator._apply_avahi_hostname("JasonsSign1")
        assert len(spy.calls) == 1
        subcommand, payload = spy.calls[0]
        assert subcommand == "avahi-write-and-restart"
        text = payload.decode("utf-8")
        assert "host-name=JasonsSign1" in text
        assert "[server]" in text
        assert "domain-name=local" in text
        assert "#host-name=" not in text

    def test_no_ship_when_value_unchanged(self, monkeypatch, tmp_path):
        """When the conf already has the target host-name, don't
        make a netctl round-trip (avoids a spurious avahi restart)."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\nhost-name=JasonsSign1\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        name_actuator._apply_avahi_hostname("JasonsSign1")
        assert spy.calls == []

    def test_avahi_fail_soft_on_netctl_error(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        spy.raise_error = RuntimeError
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\nhost-name=old\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        # Must not raise.
        name_actuator._apply_avahi_hostname("JasonsSign1")


def _parse_shipped(payload: bytes) -> configparser.ConfigParser:
    """Parse a payload the actuator shipped to netctl EXACTLY as
    avahi-daemon would: any assignment outside a group raises, which is
    the daemon's "Assignment outside group" refusal."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read_string(payload.decode("utf-8"))
    return parser


class TestAvahiConfIsAlwaysWellFormed:
    """2026-07-16 (QA, JasonsSign1): avahi-daemon refused to start —
    "Assignment outside group in <host-name=jasonssign1>" — so the sign
    had no mDNS at all. The actuator had written a conf whose entire
    content was a bare `host-name=` line with no [server] group.

    These pin the payload the actuator SHIPS, parsed the way the daemon
    reads it. Each drives the real write path via the netctl spy.
    """

    def test_empty_conf_does_not_produce_a_bare_hostname_line(self, monkeypatch, tmp_path):
        """THE BUG, exactly. An empty conf hit `conf_text.rstrip() +
        "\\nhost-name=..."`, and `"".rstrip()` is `""` — so the append WAS
        the whole file: `"\\nhost-name=jasonssign1\\n"`. host-name on line
        2, no group anywhere. Fails on the pre-fix code."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("")  # exists, but empty
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)

        name_actuator._apply_avahi_hostname("jasonssign1")

        assert len(spy.calls) == 1, "an empty conf must still be repaired"
        _subcommand, payload = spy.calls[0]
        parser = _parse_shipped(payload)  # raises on "assignment outside group"
        assert parser.get("server", "host-name") == "jasonssign1"

    def test_conf_with_groups_but_no_hostname_puts_key_in_server(self, monkeypatch, tmp_path):
        """Pre-fix, the append landed host-name at EOF — inside whatever
        group happened to be LAST. That parses fine (so a
        parses-cleanly assertion would MISS it) but configures the wrong
        group: [reflector].host-name is inert, and <name>.local never
        resolves. Assert the key's GROUP, not just the file's validity."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\nallow-interfaces=wlan0\n\n[reflector]\nenable-reflector=no\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)

        name_actuator._apply_avahi_hostname("jasonssign1")

        _subcommand, payload = spy.calls[0]
        parser = _parse_shipped(payload)
        assert parser.get("server", "host-name") == "jasonssign1"
        assert not parser.has_option("reflector", "host-name"), (
            "host-name must land in [server], not the file's last group"
        )
        # The rest of the conf survives the edit.
        assert parser.get("server", "allow-interfaces") == "wlan0"
        assert parser.get("reflector", "enable-reflector") == "no"

    def test_recovers_from_an_already_clobbered_conf(self, monkeypatch, tmp_path):
        """The self-perpetuating case: once the bare-line conf was on
        disk, the old regex MATCHED it and substituted in place, so the
        broken shape survived every subsequent rename. QA's hand-fix
        would be re-corrupted on the next PUT. This is what closes the
        loop — the actuator must REPAIR a clobbered conf, not preserve
        its shape."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("\nhost-name=jasonssign1\n")  # the clobbered shape
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)

        name_actuator._apply_avahi_hostname("fireplacesign")

        _subcommand, payload = spy.calls[0]
        parser = _parse_shipped(payload)
        assert parser.get("server", "host-name") == "fireplacesign"

    def test_template_header_comment_is_not_mistaken_for_config(self, monkeypatch, tmp_path):
        """The packaged template's commentary contains the literal text
        `#   1. host-name=openmarquee — announces ...`. Only the real key
        inside [server] may be rewritten; mangling the prose would be a
        silent docs regression that no parse check would catch."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text(
            "# host-name=openmarquee — announces openmarquee.local\n"
            "\n"
            "[server]\n"
            "host-name=openmarquee\n"
            "allow-interfaces=wlan0\n"
        )
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)

        name_actuator._apply_avahi_hostname("jasonssign1")

        _subcommand, payload = spy.calls[0]
        text = payload.decode("utf-8")
        assert "# host-name=openmarquee — announces openmarquee.local" in text, (
            "the header commentary must survive verbatim"
        )
        assert _parse_shipped(payload).get("server", "host-name") == "jasonssign1"

    def test_malformed_result_is_never_shipped(self, monkeypatch, tmp_path):
        """The structural gate. netctl writes our payload VERBATIM and
        restarts avahi-daemon — it does not validate — so a bad payload
        takes mDNS down until someone SSHes in. If the renderer is ever
        broken again, the actuator must refuse to write rather than ship
        it. Simulates a renderer regression directly."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\nhost-name=old\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        # A renderer that reintroduces exactly the 2026-07-16 bug.
        monkeypatch.setattr(
            name_actuator,
            "_substitute_hostname_line",
            lambda _text, name: f"\nhost-name={name}\n",
        )

        name_actuator._apply_avahi_hostname("jasonssign1")

        assert spy.calls == [], (
            "a conf avahi-daemon can't parse must never cross the netctl "
            "boundary; the existing working conf stays in place"
        )

    def test_wrong_case_server_group_is_regenerated_not_stranded(self, monkeypatch, tmp_path):
        """The renderer matches [server] case-insensitively; configparser
        does not. Without an escape hatch the renderer would happily edit
        `[Server]` and the gate would then refuse forever -- the renderer
        is deterministic, so EVERY later reconcile refuses identically and
        the sign is stranded with no mDNS. The unsound-original path must
        regenerate instead."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[Server]\nhost-name=old\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)

        name_actuator._apply_avahi_hostname("jasonssign1")

        assert len(spy.calls) == 1, "must not strand the sign"
        _subcommand, payload = spy.calls[0]
        assert _parse_shipped(payload).get("server", "host-name") == "jasonssign1"

    def test_duplicate_hostname_keys_collapse_to_one(self, monkeypatch, tmp_path):
        """A stray duplicate host-name in [server] made the gate reject our
        own output (configparser resolves duplicates to the LAST value), so
        the rename would no-op forever. Rewrite the first key and drop the
        rest."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\nhost-name=old\nallow-interfaces=wlan0\nhost-name=stale\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)

        name_actuator._apply_avahi_hostname("jasonssign1")

        _subcommand, payload = spy.calls[0]
        text = payload.decode("utf-8")
        assert text.count("host-name=") == 1, f"expected exactly one key, got:\n{text}"
        assert _parse_shipped(payload).get("server", "host-name") == "jasonssign1"
        assert _parse_shipped(payload).get("server", "allow-interfaces") == "wlan0"

    def test_sound_conf_is_protected_when_renderer_misbehaves(self, monkeypatch, tmp_path):
        """The escape hatch must NOT become a licence to overwrite. When
        the on-disk conf is sound (parses + has [server]), avahi is
        plausibly serving mDNS from it right now -- a bad payload would
        take a WORKING sign down, so refuse and no-op the rename."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\nhost-name=old\nallow-interfaces=wlan0\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        monkeypatch.setattr(
            name_actuator,
            "_substitute_hostname_line",
            lambda _text, name: f"\nhost-name={name}\n",
        )

        name_actuator._apply_avahi_hostname("jasonssign1")

        assert spy.calls == [], "a working conf must never be gambled away"

    def test_unsound_conf_is_repaired_even_when_renderer_misbehaves(self, monkeypatch, tmp_path):
        """The other side of the split. When the on-disk conf is ALSO
        unusable there is no working mDNS to protect, and refusing would
        strand the sign permanently. Regenerate from the baseline."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("\nhost-name=jasonssign1\n")  # the clobbered shape
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        monkeypatch.setattr(
            name_actuator,
            "_substitute_hostname_line",
            lambda _text, name: f"\nhost-name={name}\n",
        )

        name_actuator._apply_avahi_hostname("fireplacesign")

        assert len(spy.calls) == 1, "an unusable conf must be repaired, not preserved"
        _subcommand, payload = spy.calls[0]
        assert _parse_shipped(payload).get("server", "host-name") == "fireplacesign"

    def test_repair_at_the_same_name_still_ships(self, monkeypatch, tmp_path):
        """JasonsSign1's ACTUAL recovery: the clobbered conf already names
        the sign correctly, so the repair must not be swallowed by the
        `rewritten == original` skip-the-round-trip early-return. The name
        is unchanged; the SHAPE is what's broken."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("\nhost-name=jasonssign1\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)

        name_actuator._apply_avahi_hostname("jasonssign1")  # SAME name

        assert len(spy.calls) == 1, "the shape repair must ship even at the same name"
        _subcommand, payload = spy.calls[0]
        assert _parse_shipped(payload).get("server", "host-name") == "jasonssign1"

    def test_packaged_template_round_trips_byte_identical(self):
        """NO SPURIOUS RESTARTS. The reconciler runs _apply_avahi_hostname
        on EVERY backend startup. If rendering an already-correct conf
        changed so much as a newline, `rewritten == original` would be
        False and every boot would rewrite the conf + restart
        avahi-daemon, dropping mDNS briefly on each deploy/restart."""
        template = Path(__file__).resolve().parents[2] / "system" / "avahi" / "avahi-daemon.conf"
        text = template.read_text()
        assert name_actuator._substitute_hostname_line(text, "openmarquee") == text, (
            "rendering an in-sync conf must be byte-identical or every boot restarts avahi-daemon"
        )

    def test_fallback_matches_packaged_template_server_keys(self):
        """DRIFT GUARD. The fallback conf is a hand-written copy of the
        packaged template's [server] group. If someone adds a key to
        system/avahi/avahi-daemon.conf, a sign that falls back would
        silently lose it. Compares against the REAL template file, so
        the two cannot drift apart unnoticed."""
        template = Path(__file__).resolve().parents[2] / "system" / "avahi" / "avahi-daemon.conf"
        assert template.is_file(), f"packaged template missing at {template}"

        packaged = configparser.ConfigParser(strict=False, interpolation=None)
        packaged.read_string(template.read_text())
        fallback = configparser.ConfigParser(strict=False, interpolation=None)
        fallback.read_string(name_actuator._AVAHI_FALLBACK_CONF.format(name="x"))

        # items(), not keys: a key-only compare passes when the template
        # changes allow-interfaces=wlan0 -> wlan0,eth0, and a fallen-back
        # sign would silently lose the new value. host-name is excluded --
        # it's the field this actuator exists to vary.
        def _server_items(parser):
            return {k: v for k, v in parser["server"].items() if k != "host-name"}

        assert _server_items(fallback) == _server_items(packaged), (
            "fallback [server] drifted from the packaged template"
        )


class TestHostapdPath:
    """The hostapd sub-actuator reads the existing conf, rewrites the
    ssid= line locally, then ships via the existing (pre-Phase-B1)
    hostapd-write-and-restart netctl subcommand."""

    def test_no_op_when_conf_missing(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        monkeypatch.setattr(
            "openmarquee.name_actuator._HOSTAPD_CONF",
            tmp_path / "hostapd.conf",  # doesn't exist
        )
        name_actuator._apply_hostapd_ssid("JasonsSign1")
        assert spy.calls == []

    def test_rewrites_and_ships_via_existing_subcommand(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=openMarquee-SETUP\nchannel=6\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        name_actuator._apply_hostapd_ssid("JasonsSign1")
        assert len(spy.calls) == 1
        subcommand, payload = spy.calls[0]
        # 2026-07-03 (QA FIX 1): reuse the EXISTING subcommand — do
        # NOT add a fresh "hostapd-set-ssid" one.
        assert subcommand == "hostapd-write-and-restart"
        text = payload.decode("utf-8")
        assert "ssid=JasonsSign1" in text
        assert "interface=ap0" in text
        assert "channel=6" in text

    def test_no_ship_when_ssid_unchanged(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=JasonsSign1\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        name_actuator._apply_hostapd_ssid("JasonsSign1")
        assert spy.calls == []

    def test_ap_ssid_clamped_to_32_octets(self, monkeypatch, tmp_path):
        """2026-07-03 (QA HARDEN A): hostapd's `ssid=` line is capped at
        32 octets per 802.11. sign_name can be up to 63 chars (RFC 1123
        hostname). A longer sign_name must be TRUNCATED before the AP
        SSID rewrite so hostapd doesn't fail-to-restart and take the
        recovery-AP down.

        The hostname / Tailscale / mDNS consumers keep the full name;
        only the AP SSID clamp is scoped to the hostapd sub-actuator."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=openMarquee-SETUP\nchannel=6\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        # 40 chars, well over the 32-octet cap.
        long_name = "A-Really-Long-Sign-Name-That-Overflows-XY"
        assert len(long_name) == 41
        name_actuator._apply_hostapd_ssid(long_name)
        assert len(spy.calls) == 1
        _, payload = spy.calls[0]
        text = payload.decode("utf-8")
        # The AP SSID line MUST be present, truncated to 32 chars.
        expected_clamped = long_name[:32]
        assert len(expected_clamped) == 32
        assert f"ssid={expected_clamped}\n" in text
        # And the FULL untruncated name must NOT appear anywhere.
        assert long_name not in text

    def test_exactly_32_chars_not_clamped(self, monkeypatch, tmp_path):
        """Boundary check: a 32-octet sign_name is exactly at the cap
        and MUST NOT be truncated."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=old\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        boundary_name = "X" * 32
        name_actuator._apply_hostapd_ssid(boundary_name)
        _, payload = spy.calls[0]
        assert f"ssid={boundary_name}\n" in payload.decode("utf-8")


class TestApplySignName:
    """The top-level orchestrator (UI-rename path) drives all the live
    sub-actuators AND reconciles the stored name fields, regardless of
    individual failures."""

    def test_all_sub_actuators_and_stored_reconcile_run(self, monkeypatch):
        called: list[str] = []
        monkeypatch.setattr(
            "openmarquee.name_actuator._apply_hostnamectl",
            lambda name: called.append(f"hostnamectl:{name}"),
        )
        monkeypatch.setattr(
            "openmarquee.name_actuator._apply_tailscale_hostname",
            lambda name: called.append(f"tailscale:{name}"),
        )
        monkeypatch.setattr(
            "openmarquee.name_actuator._apply_avahi_hostname",
            lambda name: called.append(f"avahi:{name}"),
        )
        monkeypatch.setattr(
            "openmarquee.name_actuator._apply_hostapd_ssid",
            lambda name: called.append(f"hostapd:{name}"),
        )
        # 2026-07-07 (qarl Option A): a UI rename must ALSO clear the stored
        # tailscale_hostname + sync wifi_ssid so they can't drift.
        monkeypatch.setattr(
            "openmarquee.name_actuator._reconcile_stored_name_fields",
            lambda name: called.append(f"stored:{name}"),
        )
        name_actuator.apply_sign_name("JasonsSign1")
        assert called == [
            "hostnamectl:JasonsSign1",
            "tailscale:JasonsSign1",
            "avahi:JasonsSign1",
            "hostapd:JasonsSign1",
            "stored:JasonsSign1",
        ]


class TestReconcileNamesFromHostname:
    """2026-07-07 (one-name-everywhere, qarl-approved): boot reconcile of
    EVERY name surface from the CURRENT hostname, so an out-of-band
    rename (hostnamectl direct, bypassing the settings-PUT flow — as on
    fireplaceSign -> JasonsSign1) doesn't leave any surface stale."""

    def _stub_sub_actuators(self, monkeypatch, sink):
        # Skip hostnamectl entirely (hostname IS the source); record the
        # rest so we can assert what the reconcile drives.
        for surface, fn in (
            ("tailscale", "_apply_tailscale_hostname"),
            ("avahi", "_apply_avahi_hostname"),
            ("hostapd", "_apply_hostapd_ssid"),
            ("settings", "_reconcile_stored_name_fields"),
        ):
            monkeypatch.setattr(name_actuator, fn, lambda n, _s=surface: sink.append((_s, n)))
        monkeypatch.setattr(
            name_actuator,
            "_apply_hostnamectl",
            lambda n: sink.append(("hostnamectl", n)),
        )

    def test_calls_every_surface_with_leaf_hostname_skipping_hostnamectl(self, monkeypatch):
        called: list[tuple[str, str]] = []
        self._stub_sub_actuators(monkeypatch, called)
        monkeypatch.setattr(name_actuator.socket, "gethostname", lambda: "JasonsSign1.local")
        name_actuator.reconcile_names_from_hostname_at_boot()
        # Tailscale + avahi + hostapd + settings, all from the leaf name;
        # hostnamectl NOT called (the hostname is the source of truth).
        assert called == [
            ("tailscale", "JasonsSign1"),
            ("avahi", "JasonsSign1"),
            ("hostapd", "JasonsSign1"),
            ("settings", "JasonsSign1"),
        ]

    def test_noop_on_empty_hostname(self, monkeypatch):
        called: list[tuple[str, str]] = []
        self._stub_sub_actuators(monkeypatch, called)
        monkeypatch.setattr(name_actuator.socket, "gethostname", lambda: "")
        name_actuator.reconcile_names_from_hostname_at_boot()
        assert called == []

    def test_reconciles_drifted_hostapd_ssid_end_to_end(self, monkeypatch, tmp_path):
        # The original #4 regression, now via the generalized entrypoint:
        # hostapd stuck on the OLD name -> rewritten to the hostname.
        # Other surfaces stubbed so we isolate the hostapd netctl call.
        spy = _install_netctl_spy(monkeypatch)
        monkeypatch.setattr(name_actuator, "_apply_tailscale_hostname", lambda n: None)
        monkeypatch.setattr(name_actuator, "_apply_avahi_hostname", lambda n: None)
        monkeypatch.setattr(name_actuator, "_reconcile_stored_name_fields", lambda n: None)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=fireplaceSign\nchannel=6\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        monkeypatch.setattr(name_actuator.socket, "gethostname", lambda: "JasonsSign1")
        name_actuator.reconcile_names_from_hostname_at_boot()
        assert len(spy.calls) == 1
        subcommand, payload = spy.calls[0]
        assert subcommand == "hostapd-write-and-restart"
        assert "ssid=JasonsSign1" in payload.decode("utf-8")

    def test_hostapd_idempotent_when_already_matching(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        monkeypatch.setattr(name_actuator, "_apply_tailscale_hostname", lambda n: None)
        monkeypatch.setattr(name_actuator, "_apply_avahi_hostname", lambda n: None)
        monkeypatch.setattr(name_actuator, "_reconcile_stored_name_fields", lambda n: None)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=JasonsSign1\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        monkeypatch.setattr(name_actuator.socket, "gethostname", lambda: "JasonsSign1")
        name_actuator.reconcile_names_from_hostname_at_boot()
        assert spy.calls == []


class TestStoredNameFieldsReconcile:
    """The stored-settings surface (qarl 2026-07-07, Option A): sync
    sign_name TO the hostname, CLEAR tailscale_hostname (so Tailscale
    follows the OS hostname), and SYNC wifi_ssid — no-op when already in
    sync. Uses a REAL SystemSettings so the model_validate-based reconcile
    (which normalises + rejects exactly like production) is genuinely
    exercised — a fake with a permissive model_copy would hide the churn
    + quarantine bugs this path guards against."""

    def _install_storage(
        self, monkeypatch, sign_name, tailscale_hostname=None, wifi_ssid="openMarquee-SETUP"
    ):
        from openmarquee.settings import SystemSettings

        saved: list = []
        loaded = SystemSettings(
            sign_name=sign_name,
            tailscale_hostname=tailscale_hostname,
            wifi_ssid=wifi_ssid,
        )

        class _Storage:
            def load(self):
                return loaded

            def save(self, settings):
                saved.append(settings)

        monkeypatch.setattr("openmarquee.dependencies.get_settings_storage", lambda: _Storage())
        return saved

    def test_reconciles_all_three_fields_on_out_of_band_rename(self, monkeypatch):
        # fireplaceSign → JasonsSign1: sign_name follows, the stale
        # tailscale_hostname is CLEARED (empty ⇒ Tailscale uses the OS
        # hostname), wifi_ssid follows.
        saved = self._install_storage(
            monkeypatch,
            sign_name="fireplaceSign",
            tailscale_hostname="fireplacesign",
            wifi_ssid="fireplaceSign",
        )
        name_actuator._reconcile_stored_name_fields("JasonsSign1")
        assert len(saved) == 1
        assert saved[0].sign_name == "JasonsSign1"
        assert saved[0].tailscale_hostname is None
        assert saved[0].wifi_ssid == "JasonsSign1"

    def test_noop_when_all_fields_already_in_sync(self, monkeypatch):
        saved = self._install_storage(
            monkeypatch,
            sign_name="JasonsSign1",
            tailscale_hostname=None,
            wifi_ssid="JasonsSign1",
        )
        name_actuator._reconcile_stored_name_fields("JasonsSign1")
        assert saved == []

    def test_clears_stale_tailscale_hostname_even_when_sign_name_in_sync(self, monkeypatch):
        # THE live-sign case (JasonsSign1): #7 already synced sign_name on
        # a prior boot, but tailscale_hostname is still the stale
        # `fireplacesign` that openmarquee-tailscale.sh would re-pin. The
        # reconcile must still fire and CLEAR it.
        saved = self._install_storage(
            monkeypatch,
            sign_name="JasonsSign1",
            tailscale_hostname="fireplacesign",
            wifi_ssid="JasonsSign1",
        )
        name_actuator._reconcile_stored_name_fields("JasonsSign1")
        assert len(saved) == 1
        assert saved[0].tailscale_hostname is None

    def test_syncs_stale_wifi_ssid(self, monkeypatch):
        saved = self._install_storage(
            monkeypatch,
            sign_name="JasonsSign1",
            tailscale_hostname=None,
            wifi_ssid="fireplaceSign",
        )
        name_actuator._reconcile_stored_name_fields("JasonsSign1")
        assert len(saved) == 1
        assert saved[0].wifi_ssid == "JasonsSign1"

    def test_persists_normalised_sign_name_not_raw(self, monkeypatch):
        # sign_name stores the validated/normalised form (`Jason_Sign` →
        # `JasonSign`) so the next reconcile is a no-op.
        saved = self._install_storage(monkeypatch, sign_name="fireplaceSign")
        name_actuator._reconcile_stored_name_fields("Jason_Sign")
        assert len(saved) == 1
        assert saved[0].sign_name == "JasonSign"

    def test_caps_wifi_ssid_at_32_bytes(self, monkeypatch):
        # A hostname longer than the 32-byte SSID max is truncated for
        # wifi_ssid (rather than failing the whole reconcile).
        long_name = "a" * 40
        saved = self._install_storage(monkeypatch, sign_name="fireplaceSign")
        name_actuator._reconcile_stored_name_fields(long_name)
        assert len(saved) == 1
        assert saved[0].wifi_ssid == "a" * 32

    def test_skips_invalid_name_without_quarantining_settings(self, monkeypatch):
        # Safety guard: `---` normalises to empty → the sign_name validator
        # raises → we SKIP. Persisting it would fail re-validation on the
        # next load and quarantine settings.json to FACTORY DEFAULTS
        # (wiping the AP passphrase, Tailscale key, wifi_networks).
        saved = self._install_storage(
            monkeypatch, sign_name="JasonsSign1", tailscale_hostname="fireplacesign"
        )
        name_actuator._reconcile_stored_name_fields("---")
        assert saved == []
