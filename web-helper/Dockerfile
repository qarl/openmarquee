# openMarquee Web slide helper -- container image.
#
# This packages the standalone screenshot helper so an operator can run it
# with a single `docker run` (see README.md). The helper itself is just a
# FastAPI app; the heavy part is a headless Chromium, so we build on the
# official Microsoft Playwright Python image, which already ships:
#   * Chromium + every OS library it needs (fonts, GTK, NSS, ...)
#   * the matching `playwright` Python package and browser binaries
# That makes the image build fast and avoids a separate ~150 MB
# `playwright install` download at build time.
#
# Base tag: v1.52.0-noble. The pyproject pin is loose (`playwright>=1.40`),
# so we pick a recent stable Playwright release. `-noble` = Ubuntu 24.04 LTS.
# If you bump this tag, keep it on a real published Playwright Python image
# (https://mcr.microsoft.com/artifact/mar/playwright/python/tags).
FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

# Don't buffer stdout -- the startup token banner must appear in
# `docker logs` immediately so the operator can copy it.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy the package (build context is the `web-helper/` directory -- see
# .dockerignore for what is excluded). pyproject.toml + the package dir
# are all `pip install .` needs.
COPY pyproject.toml README.md ./
COPY openmarquee_web_helper ./openmarquee_web_helper

# Install the helper itself. fastapi + uvicorn come from pyproject; the
# `playwright` Python package is already present in the base image. The
# base image's Playwright version matches the FROM tag, so its bundled
# Chromium is the one Playwright will drive -- no `playwright install`
# needed. (If you bump the FROM tag to a line newer than what pip would
# resolve, add `RUN playwright install --with-deps chromium` as a
# belt-and-suspenders step.)
RUN pip install --no-cache-dir .

# Pre-create the token state dir owned by pwuser. The helper writes its
# generated token to ~/.openmarquee-web-helper/token; doing this here means
# a named-volume mounted at that path inherits pwuser ownership (Docker
# seeds a fresh named volume from the image's directory). Without this, a
# volume mount would be root-owned and the non-root process could not
# write the token file.
RUN mkdir -p /home/pwuser/.openmarquee-web-helper \
    && chown -R pwuser:pwuser /home/pwuser/.openmarquee-web-helper

# The Playwright base image ships an unprivileged `pwuser`. Running as a
# non-root user is good practice; switch to it for the actual service.
# The persisted token lives under this user's home (~/.openmarquee-web-helper)
# -- mount a volume there if you want the token to survive `docker rm`
# (see README.md). $HOME for pwuser on this image is /home/pwuser.
USER pwuser

# The helper binds 0.0.0.0:8888 by default (OPENMARQUEE_WEB_HELPER_PORT).
EXPOSE 8888

# Run the console script defined in pyproject.toml ([project.scripts]).
# It launches uvicorn, which prints the bearer-token banner at startup.
# Override the token with `docker run -e OPENMARQUEE_WEB_HELPER_TOKEN=...`;
# otherwise a random token is generated and shown in `docker logs`.
CMD ["openmarquee-web-helper"]
