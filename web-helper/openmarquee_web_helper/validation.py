"""URL scheme validation for the Web slide helper.

The helper screenshots whatever URL it is handed. Even though it runs
on the operator's trusted machine, it must not become a general
local-file / arbitrary-protocol read primitive -- so the `url` param
is restricted to `http` and `https` only.
"""

from urllib.parse import urlparse

# Only real, network-fetched web pages. `file:`, `ftp:`, `data:`,
# `chrome:`, a bare path, etc. are all rejected.
ALLOWED_SCHEMES = frozenset({"http", "https"})


class InvalidWebURL(ValueError):
    """Raised when a `url` param is not an acceptable http/https URL."""


def validate_web_url(url: str) -> str:
    """Validate and normalize a Web slide URL.

    Returns the URL unchanged on success. Raises `InvalidWebURL` with a
    human-readable message on any of:
      - empty / missing
      - a scheme other than http/https (file:, ftp:, data:, ...)
      - a bare path with no scheme at all
      - no host component

    This is a pure function with no I/O so it can be unit-tested
    directly.
    """
    if not url or not url.strip():
        raise InvalidWebURL("url is required")

    url = url.strip()

    # Reject control characters (notably a NUL byte) up front: urlparse
    # keeps them, and a smuggled \x00 has no legitimate place in a URL.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        raise InvalidWebURL("url contains control characters")

    parsed = urlparse(url)

    if not parsed.scheme:
        raise InvalidWebURL(
            f"url must be an absolute http/https URL, got a bare path: {url!r}"
        )

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidWebURL(
            f"url scheme {parsed.scheme!r} is not allowed; "
            "only http and https are permitted"
        )

    if not parsed.netloc:
        raise InvalidWebURL(f"url has no host: {url!r}")

    return url
