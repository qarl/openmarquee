"""Tests for the on-device web-slide renderer (Web slide C2).

`openmarquee.web_render` turns an operator-supplied URL into a
panel-sized PNG entirely on the Pi:
`WeasyPrint(url) -> PDF -> pypdfium2 -> PNG`.

`weasyprint` and `pypdfium2` are imported LAZILY inside `render_web_png`
(deferred imports), so this whole test module imports and runs on a
host WITHOUT either library installed. The render-path tests inject
fakes into `sys.modules` so the deferred `import weasyprint` /
`import pypdfium2` pick up the stubs instead of the (absent) real
libraries — and the assertions then check the wiring (the `@page`
injection, `write_pdf`, page-1 rasterization, the typed error).
"""

import sys
import types

import pytest

from openmarquee import web_render
from openmarquee.web_render import (
    WEB_RENDER_FETCH_TIMEOUT_S,
    WebRenderError,
    _build_page_css,
    main,
    render_web_png,
)

# Guard: the deferred-import promise is the whole point of this module.
# If a future change pulls weasyprint/pypdfium2 to module scope, this
# import-time check fails loudly here rather than silently on the Pi.
assert "weasyprint" not in sys.modules, (
    "weasyprint must not be imported by importing openmarquee.web_render"
)
assert "pypdfium2" not in sys.modules, (
    "pypdfium2 must not be imported by importing openmarquee.web_render"
)


# --- a real, tiny PNG so the fakes can hand back genuine bytes ---------
# 1x1 PNG — `_FakePilImage.save` writes this verbatim so the render
# tests assert on a body that actually starts with the PNG signature.
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000"
    "00907753de0000000c4944415408d76360606000000000040001"
    "5c0c02b00000000049454e44ae426082"
)


# ---------------------------------------------------------------------
# Fakes for the deferred weasyprint / pypdfium2 imports.
# ---------------------------------------------------------------------
class _FakePilImage:
    """Stand-in for the PIL image pypdfium2's bitmap.to_pil() returns.

    `save(buf, format=...)` writes a real 1x1 PNG so the produced bytes
    are genuinely PNG. `resize` records the requested size and returns
    a same-shaped fake so the exact-size guard is exercised.
    """

    def __init__(self, size=(1360, 768)):
        self.size = size
        self.resized_to = None

    def resize(self, size):
        self.resized_to = size
        return _FakePilImage(size=size)

    def save(self, buf, format=None):  # noqa: A002 — mirror PIL's kwarg
        buf.write(_PNG_1x1)


def _make_weasyprint_fake(*, write_pdf_result=b"%PDF-1.7 fake",
                          write_pdf_exc=None, calls=None):
    """Build a fake `weasyprint` module.

    `calls` (a dict) records what the render passed: the @page CSS
    string, the HTML kwargs, and the `stylesheets=` arg of write_pdf —
    so the wiring can be asserted. `write_pdf_exc`, if set, is raised
    from `write_pdf` to drive the failure path.
    """
    mod = types.ModuleType("weasyprint")

    class _FakeCSS:
        def __init__(self, string=None):
            self.string = string
            if calls is not None:
                calls["css_string"] = string

    class _FakeHTML:
        def __init__(self, url=None, url_fetcher=None):
            if calls is not None:
                calls["html_url"] = url
                calls["url_fetcher"] = url_fetcher

        def write_pdf(self, stylesheets=None):
            if calls is not None:
                calls["write_pdf_stylesheets"] = stylesheets
            if write_pdf_exc is not None:
                raise write_pdf_exc
            return write_pdf_result

    def _default_url_fetcher(url, timeout=10, ssl_context=None):
        return {"string": b"", "mime_type": "text/plain"}

    mod.CSS = _FakeCSS
    mod.HTML = _FakeHTML
    mod.default_url_fetcher = _default_url_fetcher
    return mod


def _make_pypdfium2_fake(*, page_count=1, render_exc=None, calls=None,
                         pil_size=(1360, 768)):
    """Build a fake `pypdfium2` module.

    `page_count` controls `len(PdfDocument)`; `render_exc`, if set, is
    raised from `page.render` to drive the rasterize-failure path.
    `calls` records the render `scale` so the px-exact scaling can be
    asserted.
    """
    mod = types.ModuleType("pypdfium2")

    class _FakeBitmap:
        def to_pil(self):
            return _FakePilImage(size=pil_size)

        def close(self):
            pass

    class _FakePage:
        def render(self, scale=None):
            if calls is not None:
                calls["render_scale"] = scale
            if render_exc is not None:
                raise render_exc
            return _FakeBitmap()

        def close(self):
            pass

    class _FakePdfDocument:
        def __init__(self, data):
            if calls is not None:
                calls["pdf_data"] = data
            self._pages = [_FakePage() for _ in range(page_count)]

        def __len__(self):
            return len(self._pages)

        def __getitem__(self, idx):
            return self._pages[idx]

        def close(self):
            if calls is not None:
                calls["pdf_closed"] = True

    mod.PdfDocument = _FakePdfDocument
    return mod


@pytest.fixture
def fake_libs(monkeypatch):
    """Install fake `weasyprint` + `pypdfium2` modules so the deferred
    imports inside `render_web_png` resolve to the stubs.

    Yields a callable `install(weasyprint=..., pypdfium2=...)`; each
    test builds the fakes it needs (with the right success/failure
    behavior) and installs them. `monkeypatch` undoes the `sys.modules`
    entries at teardown, restoring the deferred-import-absent baseline.
    """

    def install(weasyprint=None, pypdfium2=None):
        monkeypatch.setitem(
            sys.modules, "weasyprint",
            weasyprint if weasyprint is not None else _make_weasyprint_fake(),
        )
        monkeypatch.setitem(
            sys.modules, "pypdfium2",
            pypdfium2 if pypdfium2 is not None else _make_pypdfium2_fake(),
        )

    yield install


# ---------------------------------------------------------------------
# _build_page_css — the @page injection helper.
# ---------------------------------------------------------------------
def test_build_page_css_exact_dimensions():
    """The @page CSS carries the EXACT panel pixel dimensions and a
    zero margin — without this WeasyPrint defaults to A4."""
    assert _build_page_css(1360, 768) == (
        "@page { size: 1360px 768px; margin: 0; }"
    )


def test_build_page_css_other_panel_size():
    """A second panel size to pin the formatting, not just one value."""
    assert _build_page_css(1920, 1080) == (
        "@page { size: 1920px 1080px; margin: 0; }"
    )


# ---------------------------------------------------------------------
# render_web_png — the success path + its wiring.
# ---------------------------------------------------------------------
def test_render_web_png_returns_png_bytes(fake_libs):
    """A normal render returns real PNG bytes (PNG signature)."""
    fake_libs()
    png = render_web_png("https://status.example.com", 1360, 768)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_web_png_injects_page_css(fake_libs):
    """The render builds the @page CSS with the panel dims and passes
    it to write_pdf via `stylesheets=`."""
    calls = {}
    fake_libs(
        weasyprint=_make_weasyprint_fake(calls=calls),
        pypdfium2=_make_pypdfium2_fake(),
    )
    render_web_png("https://status.example.com", 1360, 768)

    assert calls["css_string"] == "@page { size: 1360px 768px; margin: 0; }"
    # The CSS object built from that string is the one handed to
    # write_pdf — i.e. the @page rule actually reaches WeasyPrint.
    stylesheets = calls["write_pdf_stylesheets"]
    assert len(stylesheets) == 1
    assert stylesheets[0].string == calls["css_string"]
    assert calls["html_url"] == "https://status.example.com"


def test_render_web_png_url_fetcher_has_timeout(fake_libs):
    """The render hands WeasyPrint a custom url_fetcher; calling it
    applies the per-fetch timeout (so one slow asset can't hang)."""
    calls = {}
    captured = {}

    fake_wp = _make_weasyprint_fake(calls=calls)

    # Wrap default_url_fetcher to record the timeout the wrapper passes.
    def _recording_default(url, timeout=10, ssl_context=None):
        captured["timeout"] = timeout
        return {"string": b"", "mime_type": "text/plain"}

    fake_wp.default_url_fetcher = _recording_default
    fake_libs(weasyprint=fake_wp, pypdfium2=_make_pypdfium2_fake())

    render_web_png("https://status.example.com", 1360, 768)

    fetcher = calls["url_fetcher"]
    assert callable(fetcher)
    fetcher("https://status.example.com/style.css")
    assert captured["timeout"] == WEB_RENDER_FETCH_TIMEOUT_S


def test_render_web_png_rasterizes_first_page_at_exact_scale(fake_libs):
    """pypdfium2 renders page 1 at scale 96/72, the value that maps the
    px-sized @page back to exactly the panel pixel count."""
    calls = {}
    fake_libs(
        weasyprint=_make_weasyprint_fake(write_pdf_result=b"%PDF-1.7 x"),
        pypdfium2=_make_pypdfium2_fake(calls=calls),
    )
    render_web_png("https://status.example.com", 1360, 768)

    # The PDF bytes WeasyPrint produced are the ones loaded by pdfium.
    assert calls["pdf_data"] == b"%PDF-1.7 x"
    # 96/72 — px-exact scaling (see _PDFIUM_RENDER_SCALE).
    assert calls["render_scale"] == pytest.approx(96.0 / 72.0)
    # The native PDF document is closed explicitly (no GC reliance).
    assert calls["pdf_closed"] is True


def test_render_web_png_resizes_to_exact_panel_when_off_by_a_pixel(fake_libs):
    """If pdfium's rounding lands the bitmap off by a pixel, the render
    forces the exact panel dimensions before encoding the PNG."""
    fake_libs(
        weasyprint=_make_weasyprint_fake(),
        # pdfium hands back a 1361x768 bitmap — one px wide.
        pypdfium2=_make_pypdfium2_fake(pil_size=(1361, 768)),
    )
    # Still succeeds — the size guard resizes it to the panel.
    png = render_web_png("https://status.example.com", 1360, 768)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


# ---------------------------------------------------------------------
# render_web_png — failure paths. Each raises the typed error; none hang.
# ---------------------------------------------------------------------
def test_render_web_png_weasyprint_failure_raises(fake_libs):
    """A WeasyPrint error (e.g. a network failure) surfaces as a clear
    WebRenderError, not the raw library exception."""
    fake_libs(
        weasyprint=_make_weasyprint_fake(
            write_pdf_exc=RuntimeError("connection refused")
        ),
        pypdfium2=_make_pypdfium2_fake(),
    )
    with pytest.raises(WebRenderError, match="WeasyPrint failed"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_empty_pdf_raises(fake_libs):
    """An empty PDF result is a failure, not a silent empty render."""
    fake_libs(
        weasyprint=_make_weasyprint_fake(write_pdf_result=b""),
        pypdfium2=_make_pypdfium2_fake(),
    )
    with pytest.raises(WebRenderError, match="empty PDF"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_pdf_with_no_pages_raises(fake_libs):
    """A PDF that loaded but has zero pages is a failure."""
    fake_libs(
        weasyprint=_make_weasyprint_fake(),
        pypdfium2=_make_pypdfium2_fake(page_count=0),
    )
    with pytest.raises(WebRenderError, match="no pages"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_pdfium_failure_raises(fake_libs):
    """A pypdfium2 rasterize error surfaces as a typed WebRenderError."""
    fake_libs(
        weasyprint=_make_weasyprint_fake(),
        pypdfium2=_make_pypdfium2_fake(
            render_exc=RuntimeError("bad bitmap")
        ),
    )
    with pytest.raises(WebRenderError, match="pypdfium2 failed"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_rejects_file_url_before_imports():
    """A `file://` URL is rejected by validate_web_url BEFORE the heavy
    imports — no fake_libs fixture, so if validation didn't run first
    the deferred `import weasyprint` would ImportError instead."""
    with pytest.raises(ValueError, match="scheme"):
        render_web_png("file:///etc/passwd", 1360, 768)


def test_render_web_png_rejects_nonpositive_dimensions(fake_libs):
    """A zero/negative panel dimension is a ValueError, caught before
    rendering."""
    fake_libs()
    with pytest.raises(ValueError, match="positive"):
        render_web_png("https://status.example.com", 0, 768)


# ---------------------------------------------------------------------
# main() — the subprocess entry point + its argv contract.
# ---------------------------------------------------------------------
def test_main_good_args_writes_png(fake_libs, tmp_path, capsys):
    """Good argv: the render runs and the PNG lands at <out_path>,
    exit 0, nothing on stderr."""
    fake_libs()
    out = tmp_path / "shot.png"
    rc = main(["https://status.example.com", "1360", "768", str(out)])

    assert rc == 0
    assert out.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert capsys.readouterr().err == ""


def test_main_wrong_arg_count_exits_2(capsys):
    """Too few argv -> exit 2 + a usage line on stderr."""
    rc = main(["https://status.example.com", "1360"])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_main_non_integer_dimensions_exit_2(capsys):
    """A non-integer dimension -> exit 2 + a clear stderr message."""
    rc = main(["https://status.example.com", "wide", "768", "/tmp/x.png"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "integers" in err


def test_main_nonpositive_dimensions_exit_2(capsys):
    """A zero/negative dimension -> exit 2 + a clear stderr message."""
    rc = main(["https://status.example.com", "1360", "-1", "/tmp/x.png"])
    assert rc == 2
    assert "positive" in capsys.readouterr().err


def test_main_file_url_rejected_exit_2(capsys):
    """A `file://` URL is rejected — exit 2 (bad input), a clear
    'invalid URL' message, and crucially the render is never attempted
    (no fake_libs, so a deferred import would have ImportError'd)."""
    rc = main(["file:///etc/passwd", "1360", "768", "/tmp/x.png"])
    assert rc == 2
    assert "invalid URL" in capsys.readouterr().err


def test_main_render_failure_exits_1(fake_libs, tmp_path, capsys):
    """A render failure (WeasyPrint error) -> exit 1 + the reason on
    stderr; no PNG is written."""
    fake_libs(
        weasyprint=_make_weasyprint_fake(
            write_pdf_exc=RuntimeError("connection refused")
        ),
        pypdfium2=_make_pypdfium2_fake(),
    )
    out = tmp_path / "shot.png"
    rc = main(["https://status.example.com", "1360", "768", str(out)])

    assert rc == 1
    assert "web-render:" in capsys.readouterr().err
    assert not out.exists()


def test_main_unwritable_output_path_exits_1(fake_libs, capsys):
    """A render that succeeds but can't write the PNG -> exit 1 + a
    clear stderr message."""
    fake_libs()
    # A path whose parent directory does not exist -> OSError on open.
    rc = main([
        "https://status.example.com", "1360", "768",
        "/nonexistent-dir-xyz/shot.png",
    ])
    assert rc == 1
    assert "failed to write PNG" in capsys.readouterr().err
