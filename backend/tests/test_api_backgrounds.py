"""API tests for /api/backgrounds/*."""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _playlist_storage_singleton,
    _settings_storage_singleton,
    get_content_storage,
    get_playlist_storage,
    get_settings_storage,
)
from openmarquee.playlist import PlaylistStorage
from openmarquee.settings import SettingsStorage, SystemSettings


def _real_image_bytes(w: int = 1024, h: int = 1024) -> bytes:
    img = Image.new("RGB", (w, h), (120, 200, 30))
    buf = io.BytesIO()
    # Pollinations returns JPEG; the downscale path handles either format.
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def playlist_storage(tmp_path: Path) -> PlaylistStorage:
    return PlaylistStorage(tmp_path / "playlist.json")


@pytest.fixture
def settings_storage(tmp_path: Path) -> SettingsStorage:
    store = SettingsStorage(tmp_path / "settings.json")
    store.save(SystemSettings(display_width=128, display_height=96))
    return store


@pytest.fixture
def client(
    storage: ContentStorage,
    playlist_storage: PlaylistStorage,
    settings_storage: SettingsStorage,
) -> TestClient:
    app.dependency_overrides[get_content_storage] = lambda: storage
    app.dependency_overrides[get_playlist_storage] = lambda: playlist_storage
    app.dependency_overrides[get_settings_storage] = lambda: settings_storage
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _content_storage_singleton.cache_clear()
        _playlist_storage_singleton.cache_clear()
        _settings_storage_singleton.cache_clear()


def _stub_provider_generate(monkeypatch, bytes_: bytes = None):
    """Patch every registered provider's generate() to return `bytes_`."""
    image = bytes_ if bytes_ is not None else _real_image_bytes()
    from openmarquee import backgrounds

    for provider in backgrounds.PROVIDERS.values():
        monkeypatch.setattr(provider, "generate", lambda prompt, _b=image: _b)


# --- GET /providers ---


def test_list_providers_returns_default_and_available(client: TestClient):
    response = client.get("/api/backgrounds/providers")
    assert response.status_code == 200
    body = response.json()
    assert "pollinations" in body["available"]
    assert body["default"] == "pollinations"


# --- POST /generate ---


def test_generate_saves_image_and_appends_to_playlist(
    client: TestClient,
    storage: ContentStorage,
    playlist_storage: PlaylistStorage,
    monkeypatch,
):
    _stub_provider_generate(monkeypatch)
    response = client.post(
        "/api/backgrounds/generate",
        json={"prompt": "minimal gradient, signage-friendly"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "image"
    assert body["name"].endswith(" — Background")

    from uuid import UUID
    item_id = UUID(body["id"])
    asset = storage.read_asset(item_id)
    # Provider bytes are stored verbatim now — no device-side resample.
    # The stub's _real_image_bytes() returns a 1024×1024 JPEG, so that's
    # what should be on disk; playback cover-fits to panel dims later.
    img = Image.open(io.BytesIO(asset))
    assert img.size == (1024, 1024)
    assert playlist_storage.load().item_ids == [item_id]


def test_generate_rejects_empty_prompt(client: TestClient, monkeypatch):
    _stub_provider_generate(monkeypatch)
    response = client.post("/api/backgrounds/generate", json={"prompt": ""})
    assert response.status_code == 422


def test_generate_accepts_prompt_at_500_char_limit(client: TestClient, monkeypatch):
    """500 is the max_length cap; values up to and including 500 must
    pass validation. Regression on the off-by-one — admin asked for
    'sane max ≈ 500 SD-prompt-budget' (QA explore-bg-gen 2026-04-26)."""
    _stub_provider_generate(monkeypatch)
    response = client.post(
        "/api/backgrounds/generate",
        json={"prompt": "x" * 500},
    )
    assert response.status_code == 200, response.text


def test_generate_rejects_prompt_over_500_chars(client: TestClient, monkeypatch):
    """Anything past 500 should 422 with a structured detail instead
    of round-tripping to Pollinations. Pollinations would fail upstream
    anyway; defensive validation catches the typo / pasted-novel at
    the API edge so the operator gets a clean error fast."""
    _stub_provider_generate(monkeypatch)
    response = client.post(
        "/api/backgrounds/generate",
        json={"prompt": "x" * 501},
    )
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], list)
    # Pydantic emits `string_too_long` with `max_length=500` in ctx for
    # this constraint — pin both so a future cap change is forced to
    # update the test.
    err = body["detail"][0]
    assert err["type"] == "string_too_long"
    # Subset check: pydantic v2 emits {"max_length": N} today; future
    # versions may add ctx keys (actual_length, etc.). Don't pin the
    # full dict.
    assert err["ctx"]["max_length"] == 500
    # The over-cap input itself MUST NOT echo back in `detail[0].input`
    # — the global RequestValidationError handler in app.py strips it.
    # Without that, a 5000-char prompt would round-trip back to the
    # client as a 5KB response body.
    assert "input" not in err


def test_generate_accepts_optional_name_override(client: TestClient, monkeypatch):
    _stub_provider_generate(monkeypatch)
    response = client.post(
        "/api/backgrounds/generate",
        json={"prompt": "x", "name": "My Custom Background"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "My Custom Background"


def test_generate_maps_provider_error_to_502(client: TestClient, monkeypatch):
    from openmarquee import backgrounds

    def boom(prompt):
        raise backgrounds.BackgroundGenError("rate limited by upstream")

    for provider in backgrounds.PROVIDERS.values():
        monkeypatch.setattr(provider, "generate", boom)

    response = client.post("/api/backgrounds/generate", json={"prompt": "x"})
    assert response.status_code == 502
    # 11.2: detail is opaque now (the provider's response body could leak
    # upstream HTML / tokens). Full message goes to the server log.
    assert response.json()["detail"] == "background provider failed"


def test_generate_rejects_unknown_provider(client: TestClient):
    response = client.post(
        "/api/backgrounds/generate",
        json={"prompt": "x", "provider": "dall-e"},
    )
    assert response.status_code == 400
    # 11.2: detail no longer echoes the unknown provider name.
    assert response.json()["detail"] == "unknown background provider"


def test_generate_no_longer_requires_api_key_env(
    client: TestClient, monkeypatch
):
    """Pollinations (the default provider) needs no API key — the 503 path
    the OpenAI-based prototype used doesn't exist anymore."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _stub_provider_generate(monkeypatch)
    response = client.post("/api/backgrounds/generate", json={"prompt": "x"})
    assert response.status_code == 200


# --- Sweep #3 #10: _name_from_prompt edge cases ---


def test_name_from_prompt_whitespace_only_falls_back_to_generic():
    """A prompt that's just whitespace/newlines after .strip() ends
    up empty; the ternary uses the generic placeholder."""
    from openmarquee.api_backgrounds import _name_from_prompt

    assert _name_from_prompt("   \n  ") == "Generated — Background"
    assert _name_from_prompt("") == "Generated — Background"
    assert _name_from_prompt("\t\t") == "Generated — Background"


def test_name_from_prompt_long_emoji_no_decode_error():
    """A long all-emoji prompt should slice + format without raising
    a UnicodeDecodeError or splitting a codepoint mid-byte."""
    from openmarquee.api_backgrounds import _name_from_prompt

    name = _name_from_prompt("🎉" * 100)
    # Result is well-formed UTF-8 (no surrogate halves).
    assert isinstance(name, str)
    name.encode("utf-8")  # no crash
    assert name.endswith(" — Background")


def test_name_from_prompt_truncates_at_60_chars_no_codepoint_split():
    """Python str slicing is character-based, not byte-based, so
    [:60] of an emoji string takes 60 codepoints -- no half-byte
    surrogate split. The output should still decode cleanly."""
    from openmarquee.api_backgrounds import _name_from_prompt

    name = _name_from_prompt("🚀" * 100)
    name.encode("utf-8")  # round-trip
    # First line, 60 codepoints + " — Background" suffix.
    prefix, _, _ = name.partition(" — Background")
    assert len(prefix) == 60


def test_name_from_prompt_first_line_only():
    """Multi-line prompts: the name is the first stripped line
    (operators paste long prompts; the name should stay short)."""
    from openmarquee.api_backgrounds import _name_from_prompt

    name = _name_from_prompt("first line\nsecond line longer\nmore")
    assert name == "first line — Background"
