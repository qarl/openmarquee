"""Direct unit tests for auth_middleware's pure-function private helpers.

Surveyed in 557975c (backend+rust improvement-targets) target #2:
these 4 helpers gate which paths bypass auth (`_is_whitelisted`),
which routes accept the query-string token fallback for `<img>` /
`<video>` tags (`_is_media_route`), and how the bearer token is
extracted from inbound requests (`_token_from_query`,
`_bearer_from_headers`). They had ZERO direct test coverage --
behavior was indirectly exercised by test_auth.py + test_api_settings
.py + test_csp_middleware.py end-to-end, but no helper-level
contract pinning existed.

A regression that silently widens `_is_whitelisted` (e.g. an
accidental trailing-glob entry) opens an auth bypass that the
end-to-end tests may not catch if they don't happen to exercise
the precise added path. Same shape for `_is_media_route` widening
the query-string-auth surface beyond binary blobs. These tests
lock the contracts case-by-case.

Same precedent as Bundle B2's test_rate_limit.py (round-11 code2):
security-relevant pure helpers earn their own coverage file even
when indirectly covered by integration tests.
"""

from __future__ import annotations

import pytest

from openmarquee.auth_middleware import (
    _bearer_from_headers,
    _is_media_route,
    _is_whitelisted,
    _token_from_query,
)

# --- _is_whitelisted ------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        # Exact-match entries from _WHITELIST_EXACT — sample one of
        # each "category" to lock the set membership rather than every
        # path (over-specifying would force test churn on every legit
        # whitelist addition).
        "/healthz",
        "/welcome.html",
        "/login",
        "/api/auth/status",
        "/api/auth/login",
        "/api/flock/manifest",
        "/api/playback/current-thumbnail",
        "/api/system/csp-report",
    ],
)
def test_is_whitelisted_matches_exact_paths(path: str):
    assert _is_whitelisted(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/static/anything.css",
        "/static/",  # bare prefix entry; should match itself
        "/dist/main.js",
        "/dist/chunk-abc.js",
        "/fonts/Inter.woff2",
        "/api/flock/asset/some-uuid/asset.png",
    ],
)
def test_is_whitelisted_matches_prefix_paths(path: str):
    assert _is_whitelisted(path) is True


@pytest.mark.parametrize(
    "path",
    [
        # Auth-gated API routes that MUST NOT silently slip through.
        "/api/content",
        "/api/content/some-uuid",
        "/api/content/some-uuid/asset",  # media route uses token-fallback, NOT whitelist
        "/api/settings",
        "/api/playback/state",
        "/api/auth/change-password",  # require-current-password endpoint
        "/api/live/start",
        # Near-miss against exact entries -- substring of a whitelisted
        # path must NOT match unless explicitly listed.
        "/healthz/extra",
        "/healthzfake",
        "/welcomex",
        "/api/auth/loginx",
        # Near-miss against prefix entries -- "/static" without the
        # trailing slash must NOT match "/static/" (otherwise a
        # "/staticfoo" route would silently bypass auth).
        "/staticfoo",
        "/distfoo",
        "/fontsfoo",
        # Empty path + root-only-with-suffix
        "",
        # Case sensitivity: the whitelist is case-sensitive. A
        # mixed-case spoof MUST NOT match (browsers + the routing
        # layer preserve case; spoofing /HealthZ to dodge an audit
        # tool shouldn't succeed at the auth gate).
        "/Healthz",
        "/HEALTHZ",
        "/Welcome.html",
        "/Static/main.css",
    ],
)
def test_is_whitelisted_rejects_non_whitelisted_paths(path: str):
    assert _is_whitelisted(path) is False


# --- _is_media_route -----------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/content/abc-uuid/asset",
        "/api/content/00000000-0000-0000-0000-000000000000/asset",
        "/api/content/some-id/video",
    ],
)
def test_is_media_route_matches_canonical_binary_suffixes(path: str):
    assert _is_media_route(path) is True


@pytest.mark.parametrize(
    "path",
    [
        # Under the /api/content/ prefix but NOT a binary-blob suffix.
        # Token-via-query MUST NOT carve out metadata / list routes --
        # those still require the Authorization header per the
        # _MEDIA_ROUTE_SUFFIXES allowlist comment.
        "/api/content/some-uuid",  # metadata GET
        "/api/content/some-uuid/",
        "/api/content/some-uuid/metadata",
        "/api/content/some-uuid/asset.png",  # ".png" suffix, not "/asset"
        "/api/content/some-uuid/video.mp4",  # ".mp4" suffix, not "/video"
        "/api/content",  # list route
        "/api/content/",
        # Not under the /api/content/ prefix at all.
        "/api/settings/asset",
        "/api/auth/login/asset",
        "/static/asset",
        "/asset",
        "",
        # Near-miss prefix: "/api/contentX/.../asset" must NOT widen
        # the carve-out to a sibling route.
        "/api/contentX/some-uuid/asset",
    ],
)
def test_is_media_route_rejects_non_binary_blob_paths(path: str):
    assert _is_media_route(path) is False


# --- _token_from_query ---------------------------------------------


@pytest.mark.parametrize(
    "query_string,expected",
    [
        (b"token=abc", "abc"),
        (b"foo=1&token=abc&bar=2", "abc"),
        (b"token=abc%2Fdef", "abc/def"),  # URL-decoded by parse_qs
        # parse_qs decodes plus-as-space too; combined with .strip()
        # the leading/trailing space inside the value is stripped.
        (b"token=abc+", "abc"),
    ],
)
def test_token_from_query_parses_token_value(query_string: bytes, expected: str):
    assert _token_from_query(query_string) == expected


@pytest.mark.parametrize(
    "query_string",
    [
        b"",
        b"foo=1&bar=2",  # no `token` key
        b"token=",  # blank value; keep_blank_values=False drops it
        b"\xff\xfe",  # non-ASCII bytes -- decode("ascii") raises
        b"\xff\xfetoken=abc",  # leading non-ASCII before a valid pair
    ],
)
def test_token_from_query_returns_empty_for_missing_or_malformed(query_string: bytes):
    assert _token_from_query(query_string) == ""


# --- _bearer_from_headers ------------------------------------------


@pytest.mark.parametrize(
    "headers,expected",
    [
        ([(b"authorization", b"Bearer abc")], "abc"),
        # Lowercase scheme -- RFC 7235 §2.1 says scheme name is case-
        # insensitive; clients ship both.
        ([(b"authorization", b"bearer abc")], "abc"),
        ([(b"authorization", b"BEARER abc")], "abc"),
        # Header-name is also case-insensitive per the ASGI / HTTP
        # spec; the helper lowercases on comparison.
        ([(b"Authorization", b"Bearer abc")], "abc"),
        ([(b"AUTHORIZATION", b"Bearer abc")], "abc"),
        # Trailing whitespace in token -- .strip() canonicalizes.
        ([(b"authorization", b"Bearer  abc  ")], "abc"),
        # Multiple headers -- first authorization wins.
        (
            [
                (b"x-other", b"junk"),
                (b"authorization", b"Bearer abc"),
                (b"authorization", b"Bearer def"),
            ],
            "abc",
        ),
    ],
)
def test_bearer_from_headers_extracts_token(headers: list[tuple[bytes, bytes]], expected: str):
    assert _bearer_from_headers(headers) == expected


@pytest.mark.parametrize(
    "headers",
    [
        # No Authorization header at all.
        [],
        [(b"x-other", b"junk")],
        # Wrong scheme -- "Basic" / "Token" / etc must NOT yield a
        # value (would be a scheme-confusion vulnerability).
        [(b"authorization", b"Basic abc")],
        [(b"authorization", b"Token abc")],
        [(b"authorization", b"Bearer-Custom abc")],
        # Scheme without a value -- "Bearer" alone, single-token
        # without space. len(parts) == 1, return "".
        [(b"authorization", b"Bearer")],
        # Empty token after scheme + space -- strip() yields "".
        [(b"authorization", b"Bearer ")],
        [(b"authorization", b"Bearer    ")],
        # Just a raw token without "Bearer " prefix -- must NOT yield
        # the value (operator-experience question, but the security
        # posture is "no scheme = no auth").
        [(b"authorization", b"abc")],
        # Non-ASCII bytes in the value -- decode("ascii") raises;
        # the helper's UnicodeDecodeError catch returns "".
        [(b"authorization", b"Bearer \xff\xfe")],
        [(b"authorization", b"\xff\xfe")],
    ],
)
def test_bearer_from_headers_rejects_missing_or_malformed(
    headers: list[tuple[bytes, bytes]],
):
    assert _bearer_from_headers(headers) == ""
