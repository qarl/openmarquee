from fastapi.testclient import TestClient

from openmarquee import __version__
from openmarquee.app import app


def test_healthz_returns_alive_status():
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "alive", "version": __version__}


def test_root_serves_ui_html():
    """The UI is the device's permanent interface — / must return index.html."""
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "OpenMarquee" in response.text


def test_static_mount_does_not_shadow_api_routes():
    """Regression guard: the `/` static mount is registered last, and
    /healthz, /api/*, and /openapi.json must still win over the fallback."""
    client = TestClient(app)
    assert client.get("/healthz").json()["status"] == "alive"
    assert client.get("/api/content").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_openapi_schema_available():
    client = TestClient(app)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "OpenMarquee"
    assert schema["info"]["version"] == __version__
