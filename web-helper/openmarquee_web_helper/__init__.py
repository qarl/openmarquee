"""openMarquee Web slide screenshot helper.

A small standalone HTTP service that renders a webpage to a PNG via
headless Chromium. The openMarquee sign (a RAM-constrained Raspberry
Pi that cannot run a browser) fetches those screenshots over HTTP.

This package is intentionally self-contained: it must NOT import from
the `openmarquee` backend package. It is deployed to the operator's
own machine, not to the Pi.
"""

__version__ = "0.1.0"
