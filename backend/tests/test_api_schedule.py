from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.dependencies import (
    _schedule_storage_singleton,
    get_schedule_storage,
)
from openmarquee.schedule import ScheduleStorage


@pytest.fixture
def storage(tmp_path: Path) -> ScheduleStorage:
    return ScheduleStorage(tmp_path / "schedules.json")


@pytest.fixture
def client(storage: ScheduleStorage) -> TestClient:
    app.dependency_overrides[get_schedule_storage] = lambda: storage
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _schedule_storage_singleton.cache_clear()


def test_get_empty_schedule(client: TestClient):
    response = client.get("/api/schedules")
    assert response.status_code == 200
    body = response.json()
    assert body["rules"] == []
    assert body["default_playlist_name"] == "default"


def test_put_then_get_round_trip(client: TestClient):
    payload = {
        "schema_version": 1,
        "rules": [
            {
                "name": "Weekend",
                "days": ["sat", "sun"],
                "start_time": "00:00",
                "end_time": "24:00",  # all-day idiom
                "playlist_name": "weekend_promo",
                "enabled": True,
            }
        ],
        "default_playlist_name": "default",
    }
    response = client.put("/api/schedules", json=payload)
    assert response.status_code == 200
    assert response.json() == payload

    response = client.get("/api/schedules")
    assert response.json() == payload


def test_put_rejects_malformed_time(client: TestClient):
    payload = {
        "rules": [
            {
                "name": "Bad",
                "days": ["mon"],
                "start_time": "8:00",  # missing leading zero
                "end_time": "17:00",
                "playlist_name": "x",
            }
        ],
        "default_playlist_name": "default",
    }
    response = client.put("/api/schedules", json=payload)
    assert response.status_code == 422


def test_put_rejects_unknown_day(client: TestClient):
    payload = {
        "rules": [
            {
                "name": "Bad",
                "days": ["funday"],
                "start_time": "08:00",
                "end_time": "17:00",
                "playlist_name": "x",
            }
        ],
        "default_playlist_name": "default",
    }
    response = client.put("/api/schedules", json=payload)
    assert response.status_code == 422
