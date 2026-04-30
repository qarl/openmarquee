"""API surface tests for /api/settings."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _settings_storage_singleton,
    get_content_storage,
    get_settings_storage,
)
from openmarquee.seed import render_text_slide_png
from openmarquee.settings import SettingsStorage


@pytest.fixture
def storage(tmp_path: Path) -> SettingsStorage:
    return SettingsStorage(tmp_path / "settings.json")


@pytest.fixture
def content_storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def client(
    storage: SettingsStorage, content_storage: ContentStorage
) -> TestClient:
    app.dependency_overrides[get_settings_storage] = lambda: storage
    app.dependency_overrides[get_content_storage] = lambda: content_storage
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _settings_storage_singleton.cache_clear()
        _content_storage_singleton.cache_clear()


def test_get_returns_defaults_when_nothing_persisted(client: TestClient):
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["output_mode"] == "hdmi"
    assert body["display_width"] == 1920
    assert body["display_height"] == 1080
    assert body["brightness"] == 80
    assert body["wifi_password"] == "openmarquee"  # SYSTEM_SPEC §4.1
    assert body["timezone"] is None


def test_put_then_get_round_trip(client: TestClient):
    payload = {
        "schema_version": 1,
        "sign_name": "Coffee Shop",
        "output_mode": "hub75",
        "display_width": 192,
        "display_height": 64,
        "display_rotation": 0,
        "brightness": 40,
        "gamma": 2.4,
        "ws281x_pixel_order": "serpentine",
        "wifi_ap_enabled": True,
        "wifi_ssid": "CoffeeShop",
        "wifi_password": "bean-bean-bean",
        "wifi_station_enabled": False,
        "wifi_station_ssid": None,
        "wifi_station_password": None,
        "timezone": "America/New_York",
        "tailscale_enabled": False,
        "tailscale_auth_key": None,
        "tailscale_hostname": None,
        "flock_sync_enabled": True,
        "ui_first_run_seen": False,
    }
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200
    assert response.json() == payload

    # And reads back verbatim.
    response = client.get("/api/settings")
    assert response.json() == payload


def test_put_rejects_bad_output_mode(client: TestClient):
    payload = {"output_mode": "vga"}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


def test_put_rejects_brightness_out_of_range(client: TestClient):
    payload = {"brightness": 150}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


def test_put_rejects_ssid_over_32_bytes(client: TestClient):
    payload = {"wifi_ssid": "x" * 33}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


def test_put_rejects_too_short_wifi_password(client: TestClient):
    payload = {"wifi_password": "short"}  # 5 chars
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


def test_put_rejects_empty_wifi_password(client: TestClient):
    """Empty passphrase isn't a WPA2 passphrase. UI must send the current
    stored value on Save (GET returns it verbatim) — no "no change" sentinel."""
    payload = {"wifi_password": ""}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


def test_put_accepts_timezone_and_persists_it(client: TestClient):
    payload = {"timezone": "Europe/Paris"}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200
    response = client.get("/api/settings")
    assert response.json()["timezone"] == "Europe/Paris"


def test_put_rejects_garbage_timezone(client: TestClient):
    payload = {"timezone": "DROP TABLE tz;"}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


# --- Display-dim change side-effect (qarl 2026-04-30 ask 1) -----------


def _seed_text_slide(content_storage: ContentStorage, *, width: int, height: int):
    """Helper: seed one text slide at the given dims and return it."""
    from openmarquee.content import TextSlide

    png = render_text_slide_png(
        "Hello there",
        width,
        height,
        fg="#FFFFFF",
        bg="#000000",
    )
    slide = TextSlide(
        name="Hello there",
        text="Hello there",
        text_color="#FFFFFF",
        background_color="#000000",
        font_size_px=int(height * 0.4),
        duration_ms=3000,
    )
    content_storage.save_text_slide(slide, png)
    return slide


def _png_dims(png: bytes) -> tuple[int, int]:
    from io import BytesIO

    from PIL import Image

    return Image.open(BytesIO(png)).size


def test_put_with_no_dim_change_does_not_rerender_text_slides(
    client: TestClient, content_storage: ContentStorage
):
    """Brightness-only change must not touch text-slide PNGs."""
    slide = _seed_text_slide(content_storage, width=1920, height=1080)
    original_png = content_storage.read_asset(slide.id)

    response = client.put("/api/settings", json={"brightness": 60})
    assert response.status_code == 200

    # PNG bytes unchanged.
    assert content_storage.read_asset(slide.id) == original_png


def test_put_rotation_flip_rerenders_text_slides_at_swapped_dims(
    client: TestClient, content_storage: ContentStorage
):
    """Rotating from 0 to 90 swaps width/height — text PNGs re-render
    portrait so the device's renderer doesn't have to letterbox the
    landscape original."""
    slide = _seed_text_slide(content_storage, width=1920, height=1080)
    original_dims = _png_dims(content_storage.read_asset(slide.id))
    assert original_dims == (1920, 1080)

    response = client.put(
        "/api/settings",
        json={
            "display_width": 1920,
            "display_height": 1080,
            "display_rotation": 90,
        },
    )
    assert response.status_code == 200

    new_png = content_storage.read_asset(slide.id)
    assert _png_dims(new_png) == (1080, 1920)


def test_put_resolution_change_rerenders_text_slides(
    client: TestClient, content_storage: ContentStorage
):
    """Switching from 1920×1080 hdmi to 128×64 hub75 re-renders text
    slides at the smaller panel — squish kicks in for any text that
    overflows the new width."""
    slide = _seed_text_slide(content_storage, width=1920, height=1080)
    response = client.put(
        "/api/settings",
        json={
            "output_mode": "hub75",
            "display_width": 128,
            "display_height": 64,
            "display_rotation": 0,
        },
    )
    assert response.status_code == 200

    assert _png_dims(content_storage.read_asset(slide.id)) == (128, 64)


def test_put_dim_change_with_no_text_slides_is_a_clean_noop(
    client: TestClient, content_storage: ContentStorage
):
    """Empty content store + dim change — the rerender background task
    runs to completion against zero items without raising."""
    response = client.put(
        "/api/settings",
        json={
            "display_width": 1920,
            "display_height": 1080,
            "display_rotation": 270,
        },
    )
    assert response.status_code == 200
