"""Console-script entry point: run the Web slide helper under uvicorn.

Installed as the `openmarquee-web-helper` command (see pyproject.toml).

Bind host/port are configurable via env vars so the operator can expose
the helper on the LAN for the sign to reach:
  OPENMARQUEE_WEB_HELPER_HOST  (default 0.0.0.0)
  OPENMARQUEE_WEB_HELPER_PORT  (default 8888)
"""

import os


def main() -> None:
    """Launch the FastAPI app with uvicorn."""
    import uvicorn

    host = os.environ.get("OPENMARQUEE_WEB_HELPER_HOST", "0.0.0.0")
    port = int(os.environ.get("OPENMARQUEE_WEB_HELPER_PORT", "8888"))

    # Pass the import string (not the app object) so uvicorn owns the
    # lifespan -- that is where the token banner is printed.
    uvicorn.run("openmarquee_web_helper.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
