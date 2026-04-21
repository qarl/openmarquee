from fastapi.testclient import TestClient

from openmarquee import __version__
from openmarquee.app import app


def test_index_returns_alive_status():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "alive", "version": __version__}


def test_openapi_schema_available():
    client = TestClient(app)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "OpenMarquee"
    assert schema["info"]["version"] == __version__
