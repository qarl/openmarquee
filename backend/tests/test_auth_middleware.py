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
    _wrap_send_no_store_no_referrer,
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
        # 2026-07-12: dashboard tiles use the JPEG /thumbnail endpoint
        # (2026-07-02 OOM handover). It's a binary-blob GET like /asset,
        # so query-token auth must reach it — without this the dashboard
        # <img> tiles 401'd → dark placeholders.
        "/api/content/some-id/thumbnail",
        "/api/content/00000000-0000-0000-0000-000000000000/thumbnail",
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
        "/api/content/some-uuid/thumbnail.jpg",  # ".jpg" suffix, not "/thumbnail"
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


# --- _wrap_send_no_store_no_referrer --------------------------------
#
# Round-15 test-gap coverage (closes the test-gap audit series). This
# response-hardening wrap is installed when query-string token auth
# was used (auth_middleware.py:235 -- the ?token=... fallback for
# <img>/<video> src). Its docstring documents that it MUST strip any
# pre-existing Cache-Control / Referrer-Policy headers from the
# inner handler BEFORE appending the canonical safe values, so a
# misconfigured intermediary that picks first-or-last can't honor a
# permissive directive over no-store.
#
# Pre-r15 coverage: existing tests only hit routes that don't set
# these headers themselves; the strip-then-append branch was never
# exercised. A refactor that silently switched to
# "append-without-stripping" would ship a token-bearing URL with two
# contradictory Cache-Control directives -- an intermediary picking
# the permissive one (RFC 7234 §5.2 says compliant caches honor the
# most-restrictive, but misconfigured ones in the wild don't always)
# caches the bearer. The token has been a real argon2-verified secret
# since 2026-05-24 (audit Path A), so the leak is no longer inert.


@pytest.mark.asyncio
async def test_wrap_strips_pre_existing_cache_control():
    """Inner handler emits Cache-Control: public, max-age=3600 on its
    response (e.g. a future media-route adding caching). The wrap
    must REPLACE that with the single canonical no-store, not append
    a second header line."""
    captured: list[dict] = []

    async def inner_send(msg: dict) -> None:
        captured.append(msg)

    wrapped = _wrap_send_no_store_no_referrer(inner_send)
    await wrapped(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"cache-control", b"public, max-age=3600"),
                (b"content-type", b"image/png"),
            ],
        }
    )
    start = captured[0]
    cc_values = [v for k, v in start["headers"] if k.lower() == b"cache-control"]
    assert cc_values == [b"no-store"], (
        f"expected exactly one Cache-Control: no-store, got {cc_values!r}"
    )


@pytest.mark.asyncio
async def test_wrap_strips_pre_existing_referrer_policy():
    """Inner handler emits Referrer-Policy: unsafe-url. The wrap must
    REPLACE that with the single canonical no-referrer, not append a
    second header line."""
    captured: list[dict] = []

    async def inner_send(msg: dict) -> None:
        captured.append(msg)

    wrapped = _wrap_send_no_store_no_referrer(inner_send)
    await wrapped(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"referrer-policy", b"unsafe-url"),
                (b"content-type", b"image/png"),
            ],
        }
    )
    start = captured[0]
    rp_values = [v for k, v in start["headers"] if k.lower() == b"referrer-policy"]
    assert rp_values == [b"no-referrer"], (
        f"expected exactly one Referrer-Policy: no-referrer, got {rp_values!r}"
    )


@pytest.mark.asyncio
async def test_wrap_strips_both_pre_existing_headers():
    """When BOTH permissive headers are present on the inner response,
    both get stripped and both get replaced with the canonical
    safe values. Exactly one of each survives."""
    captured: list[dict] = []

    async def inner_send(msg: dict) -> None:
        captured.append(msg)

    wrapped = _wrap_send_no_store_no_referrer(inner_send)
    await wrapped(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"cache-control", b"public, max-age=86400"),
                (b"referrer-policy", b"unsafe-url"),
                (b"content-type", b"image/png"),
            ],
        }
    )
    start = captured[0]
    cc_values = [v for k, v in start["headers"] if k.lower() == b"cache-control"]
    rp_values = [v for k, v in start["headers"] if k.lower() == b"referrer-policy"]
    assert cc_values == [b"no-store"]
    assert rp_values == [b"no-referrer"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header_name",
    [
        b"Cache-Control",  # title-case
        b"CACHE-CONTROL",  # upper-case
        b"cAcHe-CoNtRoL",  # mixed-case
        b"cache-control",  # already-lower baseline
    ],
)
async def test_wrap_strips_case_variants_of_cache_control(header_name: bytes):
    """RFC 7230 §3.2 says header names are case-insensitive. ASGI
    convention is lowercased bytes, but a handler that emits an
    upper/mixed-case Cache-Control still needs to be stripped --
    otherwise a refactor that switches to an exact `== b"cache-
    control"` comparison would let a Title-Case pre-existing header
    slip through alongside the appended no-store."""
    captured: list[dict] = []

    async def inner_send(msg: dict) -> None:
        captured.append(msg)

    wrapped = _wrap_send_no_store_no_referrer(inner_send)
    await wrapped(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(header_name, b"public, max-age=3600")],
        }
    )
    start = captured[0]
    # Count Cache-Control occurrences case-insensitively (matching the
    # impl's case-folded compare). Should be exactly ONE, with the
    # canonical lowercase name and the no-store value.
    cc_entries = [(k, v) for k, v in start["headers"] if k.lower() == b"cache-control"]
    assert cc_entries == [(b"cache-control", b"no-store")], (
        f"variant {header_name!r}: expected one canonical no-store, got {cc_entries!r}"
    )


@pytest.mark.asyncio
async def test_wrap_passes_through_non_start_messages_unchanged():
    """The wrap is documented to only mutate http.response.start --
    body messages (chunks, trailers) must pass through byte-identical
    so the inner handler's payload reaches the client unchanged."""
    captured: list[dict] = []

    async def inner_send(msg: dict) -> None:
        captured.append(msg)

    wrapped = _wrap_send_no_store_no_referrer(inner_send)
    body_msg = {
        "type": "http.response.body",
        "body": b"<binary blob>",
        "more_body": False,
    }
    await wrapped(body_msg)
    assert captured == [body_msg], "http.response.body must pass through unchanged (byte-identical)"

    # A future ASGI message type (e.g. http.response.trailers) should
    # also pass through unchanged -- the wrap's mutation is scoped
    # specifically to the start message.
    trailers_msg = {
        "type": "http.response.trailers",
        "headers": [(b"x-trace-id", b"abc123")],
    }
    await wrapped(trailers_msg)
    assert captured[-1] == trailers_msg


@pytest.mark.asyncio
async def test_wrap_preserves_other_inner_handler_headers():
    """The wrap MUST only strip Cache-Control + Referrer-Policy. Every
    other header from the inner handler (content-type, etag,
    set-cookie, custom headers) survives alongside the appended
    canonical no-store + no-referrer values."""
    captured: list[dict] = []

    async def inner_send(msg: dict) -> None:
        captured.append(msg)

    wrapped = _wrap_send_no_store_no_referrer(inner_send)
    await wrapped(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"cache-control", b"public"),  # will be stripped
                (b"content-type", b"image/png"),  # survives
                (b"etag", b'"abc123"'),  # survives
                (b"x-custom-header", b"keep-me"),  # survives
                (b"referrer-policy", b"unsafe-url"),  # will be stripped
            ],
        }
    )
    start = captured[0]
    surviving = {(k, v) for k, v in start["headers"]}
    # Non-stripped headers must all be present.
    assert (b"content-type", b"image/png") in surviving
    assert (b"etag", b'"abc123"') in surviving
    assert (b"x-custom-header", b"keep-me") in surviving
    # Stripped + replaced headers carry the canonical values, not the
    # inner handler's permissive ones.
    assert (b"cache-control", b"no-store") in surviving
    assert (b"referrer-policy", b"no-referrer") in surviving
    # Sanity: no leftover permissive values.
    cc_values = [v for k, v in start["headers"] if k.lower() == b"cache-control"]
    rp_values = [v for k, v in start["headers"] if k.lower() == b"referrer-policy"]
    assert cc_values == [b"no-store"]
    assert rp_values == [b"no-referrer"]
