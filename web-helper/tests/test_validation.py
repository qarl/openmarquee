"""Unit tests for the pure `validate_web_url` function."""

import pytest

from openmarquee_web_helper.validation import InvalidWebURL, validate_web_url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com/page?q=1",
        "HTTP://EXAMPLE.COM",  # scheme compare is case-insensitive
        "https://192.168.1.10:8080/dashboard",
    ],
)
def test_validate_web_url_accepts_http_and_https(url):
    """http/https URLs with a host pass through unchanged (trimmed)."""
    assert validate_web_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "data:text/html,<h1>hi</h1>",
        "chrome://settings",
        "/etc/passwd",  # bare path, no scheme
        "example.com",  # bare host, no scheme
        "",  # empty
        "   ",  # whitespace only
        "http://",  # scheme but no host
    ],
)
def test_validate_web_url_rejects_non_web(url):
    """file:, ftp:, data:, bare paths, empty input -> InvalidWebURL."""
    with pytest.raises(InvalidWebURL):
        validate_web_url(url)
