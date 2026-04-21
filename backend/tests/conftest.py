"""Pytest config shared by every test in the backend suite."""

import os

# Disable the first-boot content seeding for tests. The seed path runs
# inside the FastAPI lifespan, which fires when a TestClient context is
# entered — without this opt-out it would try to populate whatever
# content root the real env var points at (or the default cwd-relative
# fallback) with starter gradient slides, polluting local dev state.
# Seed behavior itself is covered explicitly by `test_seed.py`.
os.environ.setdefault("OPENMARQUEE_DISABLE_SEED", "1")
