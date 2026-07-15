import pytest
from fastapi.testclient import TestClient

from openmarquee import __version__
from openmarquee.app import app


@pytest.fixture
def client():
    """`with TestClient(app)` so app lifespan (startup/shutdown) runs — otherwise
    background tasks registered in the lifespan silently don't get cleaned up."""
    with TestClient(app) as test_client:
        yield test_client


def test_healthz_returns_alive_status(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "alive", "version": __version__}


def test_root_serves_ui_html(client):
    """The UI is the device's permanent interface — / must return index.html."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "openMarquee" in response.text


def test_static_mount_does_not_shadow_api_routes(client):
    """Regression guard: the `/` static mount is registered last, and
    /healthz, /api/*, and /openapi.json must still win over the fallback."""
    assert client.get("/healthz").json()["status"] == "alive"
    assert client.get("/api/content").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_openapi_schema_available(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "openMarquee"
    assert schema["info"]["version"] == __version__


def test_request_validation_422_strips_echoed_input(client):
    """Global RequestValidationError handler in app.py drops the
    verbatim input echo from every endpoint's body-validation 422.
    Pins the cross-cutting behavior at an endpoint OTHER than
    /api/backgrounds/generate (which has its own boundary test) so a
    future refactor that scopes the handler too narrowly fails here.

    Routes through /api/content/text-slides which fails at request-
    validation when png_base64 is missing — distinct from the
    in-route _validation_error_422 helper that fires after the body
    parses cleanly but TextSlide() construction raises.
    """
    response = client.post(
        "/api/content/text-slides",
        json={"name": "x", "text": "x"},  # missing png_base64
    )
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], list)
    # All errors must be JSON-safe AND must NOT echo input bytes.
    for err in body["detail"]:
        assert "input" not in err


def test_boot_card_ssid_sourced_from_live_association_not_target():
    """Regression (2026-07-15, bug 2): the BOOT identity card's Wi-Fi
    SSID must come from the LIVE wlan0 association (active_wlan0_ssid),
    NOT the persisted submit-time target (supervisor.last_sta_ssid) —
    which is written before the join is confirmed and never refreshed,
    so it can name a stale / not-yet-joined network or a connection
    profile name. Anti-revert guard on the app.py boot-card wiring.
    """
    import inspect

    from openmarquee import app as app_mod
    from openmarquee.app import _boot_card_params

    src = inspect.getsource(app_mod)
    assert "active_wlan0_ssid" in src, "boot card must read the live SSID"
    # 2026-07-15 (bug 1): the BOOT card is published early (before the
    # playback loop) from `_boot_ssid = active_wlan0_ssid()`, then fed to
    # the card builder as `ssid=_boot_ssid` (2026-07-15 A1 refactor: the
    # dict literal moved into _boot_card_params for testability).
    assert "ssid=_boot_ssid" in src, "boot card ssid must be the live value"
    assert "ssid=supervisor.last_sta_ssid" not in src, (
        "boot card must NOT be fed the persisted target SSID"
    )
    # And the builder plumbs that live value straight through to the params
    # (runtime, not just source-shape).
    params = _boot_card_params(url="http://openmarquee.local", ssid="LiveNet", ip="1.2.3.4")
    assert params["ssid"] == "LiveNet", "boot card ssid param must be the value passed in"


def test_boot_card_includes_setup_countdown_when_mid_gesture(tmp_path, monkeypatch):
    """Recovery A1 end-to-end (count file → boot_hint param): when the
    on-disk power-cycle counter says the operator is 1 or 2 cycles into the
    3× gesture, _boot_card_params folds the "Restart N× more for Setup Mode"
    line into the BOOT card the lifespan ships to the renderer; a normal boot
    (count 0) omits it. Pairs with the renderer's own boot_hint_appears_when_set
    test (param → CardShape::BootHint) so the full count-file → param → shape
    chain is covered."""
    from openmarquee.app import _boot_card_params

    count_file = tmp_path / "boot-cycle-count"
    monkeypatch.setenv("OPENMARQUEE_BOOT_CYCLE_COUNT_FILE", str(count_file))

    # 1 cycle done → 2 to go.
    count_file.write_text("1\n")
    params = _boot_card_params(url="http://x", ssid=None, ip=None)
    assert params["boot_hint"] == "Restart 2× more for Setup Mode"

    # 2 cycles done → 1 to go.
    count_file.write_text("2\n")
    params = _boot_card_params(url="http://x", ssid=None, ip=None)
    assert params["boot_hint"] == "Restart 1× more for Setup Mode"

    # Normal boot (count 0) → no countdown key at all.
    count_file.write_text("0\n")
    params = _boot_card_params(url="http://x", ssid=None, ip=None)
    assert "boot_hint" not in params
