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

    src = inspect.getsource(app_mod)
    assert "active_wlan0_ssid" in src, "boot card must read the live SSID"
    # 2026-07-15 (bug 1): the BOOT card is published early (before the
    # playback loop) from `_boot_ssid = active_wlan0_ssid()`.
    assert '"ssid": _boot_ssid' in src, "boot card ssid must be the live value"
    assert '"ssid": supervisor.last_sta_ssid' not in src, (
        "boot card must NOT be fed the persisted target SSID"
    )
