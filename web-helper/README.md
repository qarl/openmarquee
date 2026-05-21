# openMarquee Web slide helper

This is a small helper program you run on your **own computer** (a laptop,
desktop, or always-on NAS / home server) so that an openMarquee sign can
display a **Web slide** — a live screenshot of a webpage.

**Why it's needed:** an openMarquee sign is a tiny Raspberry Pi. It does
not have enough memory to run a web browser. This helper runs a browser
*for* it: the sign asks the helper for a fresh screenshot of a page, the
helper takes the picture, and the sign shows it. You only need this if you
want to use Web slides — nothing else about the sign depends on it.

You can run the helper two ways. **Docker is the recommended path.** If you
don't use Docker, there's a no-Docker fallback further down.

---

## Security

Please read this before you run the helper.

The helper will take a screenshot of **any** `http`/`https` URL that is
reachable from the machine it runs on — and that includes private and
internal addresses on your own network (for example
`http://192.168.1.20/dashboard`). **This is intentional.** Putting an
internal dashboard — a home-server status page, a smart-home panel, an
office wallboard — on a sign is one of the main reasons Web slides exist,
and that only works if the helper can reach internal addresses.

Two things follow from that:

- **Run the helper only on a network where that capability is
  acceptable.** Anything the helper machine can reach over the network,
  the helper can be told to screenshot.
- **Keep the bearer token private.** The token is the only thing
  protecting the helper. Anyone who has the token *and* network access to
  the helper can make it fetch any URL it can reach. Don't paste it into
  shared chats, screenshots, or public issues; treat it like a password.

---

## Run it with Docker (recommended)

You need [Docker](https://docs.docker.com/get-docker/) installed.

There is no public image to download yet, so you build the image once from
this repository. From the **`code/`** directory of the openMarquee repo:

```sh
docker build -t openmarquee/web-helper web-helper/
```

Then start it:

```sh
docker run -d -p 8888:8888 --name openmarquee-web-helper openmarquee/web-helper
```

That's it — the helper is now running and listening on port **8888**.

### Get the token

Web slides are protected by a secret **token**. The helper prints it when
it starts up. View it with:

```sh
docker logs openmarquee-web-helper
```

Look for a banner like:

```
================================================================
  openMarquee Web slide helper -- bearer token
  Paste this into the sign's Web slide settings:
    abc123...your-token-here...
================================================================
```

Copy that token — you'll paste it into the sign in a moment.

### Keep the token stable across restarts

By default the token is stored *inside* the container. If you ever run
`docker rm`, a new token is generated next time and you'd have to re-paste
it into the sign. Two ways to avoid that:

**Option A — mount a volume** so the token file persists:

```sh
docker run -d -p 8888:8888 --name openmarquee-web-helper \
  -v openmarquee-web-helper-token:/home/pwuser/.openmarquee-web-helper \
  openmarquee/web-helper
```

**Option B — set your own token** with an environment variable (then it's
always the same and you don't depend on a volume):

```sh
docker run -d -p 8888:8888 --name openmarquee-web-helper \
  -e OPENMARQUEE_WEB_HELPER_TOKEN=pick-your-own-long-random-string \
  openmarquee/web-helper
```

### Always-on setup (Compose)

If you want the helper to start automatically and come back after a reboot
(ideal for a NAS), use the included `docker-compose.yml`. From the
`web-helper/` directory:

```sh
docker compose up -d
docker compose logs        # shows the token banner
```

It uses a named volume so the token stays stable, and restarts itself
unless you explicitly stop it.

---

## Run it without Docker (pipx)

If you'd rather not use Docker, you can install the helper directly with
[pipx](https://pipx.pypa.io/). This needs **Python 3.11 or newer**.

From the `code/` directory of the openMarquee repo:

```sh
pipx install ./web-helper
```

The helper drives a real Chromium browser, which is *not* included in the
package. Install it once (a roughly 150 MB download — this is normal and
only happens once):

```sh
playwright install chromium
```

Then start the helper:

```sh
openmarquee-web-helper
```

It serves on `0.0.0.0:8888` and prints the same token banner described
above directly in your terminal. The token is also saved to
`~/.openmarquee-web-helper/token`, so it stays the same on every restart.

---

## Connect your sign to it

1. Find the address of the machine running the helper — its LAN IP
   (e.g. `192.168.1.50`) or, if you use Tailscale, its Tailscale name.
2. On the sign, open the **Settings** page and find the **Web slide**
   helper fields.
3. Enter the helper's address as `http://<that-address>:8888`
   (for example `http://192.168.1.50:8888`).
4. Paste the **token** from the startup banner.
5. Save. The sign can now use Web slides.

---

## Troubleshooting

- **The sign can't reach the helper.** The helper machine and the sign
  must be on the same network (or both on your Tailscale network). Check
  that the address and port are correct, and that no firewall on the
  helper machine is blocking port 8888.
- **Web slides stop updating.** The helper only works while the machine
  running it is powered on and the helper is running. If you put your
  laptop to sleep, Web slides won't refresh until it's back. For
  always-on use, run the helper on a machine that stays on (see the
  Compose setup above).
- **Wrong token.** If you recreated the container without a volume or a
  fixed `OPENMARQUEE_WEB_HELPER_TOKEN`, the token changed — grab the new
  one from `docker logs` and re-paste it into the sign's Settings.

---

## For developers

Endpoints, auth details, and the test suite:

| Method | Path       | Auth          | Purpose                                  |
|--------|------------|---------------|------------------------------------------|
| GET    | `/healthz` | none          | Liveness probe.                          |
| GET    | `/shot`    | bearer token  | Screenshot `url` at a `w`x`h` viewport.  |

`/shot` query params: `url` (http/https only), `w`, `h` (viewport px). On
success it returns `200` with `Content-Type: image/png`.

Bind host/port are configurable via `OPENMARQUEE_WEB_HELPER_HOST`
(default `0.0.0.0`) and `OPENMARQUEE_WEB_HELPER_PORT` (default `8888`).

Run the tests (they mock the screenshot worker, so no browser is needed):

```sh
cd web-helper
pip install -e .[dev]
python -m pytest -q
```
