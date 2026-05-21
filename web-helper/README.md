# openMarquee Web slide helper

A small standalone HTTP service that screenshots a webpage to a PNG, so
an openMarquee sign can show a "Web slide" without running a browser
itself.

The openMarquee sign is a RAM-constrained Raspberry Pi that cannot run
Chromium. The **operator** runs this helper on their own capable
machine; the sign fetches rendered page screenshots from it over HTTP.

## Endpoints

| Method | Path       | Auth          | Purpose                                   |
|--------|------------|---------------|-------------------------------------------|
| GET    | `/healthz` | none          | Liveness probe.                           |
| GET    | `/shot`    | bearer token  | Screenshot `url` at a `w`x`h` viewport.   |

`/shot` query params: `url` (http/https only), `w`, `h` (viewport px).
On success it returns `200` with `Content-Type: image/png`.

## Auth

`/shot` requires `Authorization: Bearer <token>`. The token is resolved
at startup from, in order: the `OPENMARQUEE_WEB_HELPER_TOKEN` env var, a
persisted token file at `~/.openmarquee-web-helper/token`, or a freshly
generated random token (then saved to that file, mode 0600). The active
token is printed to stdout at startup -- copy it into the sign's Web
slide settings.

## Run

```sh
pip install -e .[dev]
playwright install chromium      # one-time browser download
openmarquee-web-helper           # serves on 0.0.0.0:8888 by default
```

## Tests

```sh
cd web-helper
python -m pytest -q
```

The test suite mocks the screenshot worker, so it runs with no browser
installed.

> Docker packaging, the operator-facing setup guide, and pipx
> distribution docs are tracked separately (commit H2).
