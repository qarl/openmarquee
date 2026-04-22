"""Pytest config shared by every test in the backend suite."""

import os

# Disable the first-boot content seeding for tests. The seed path runs
# inside the FastAPI lifespan, which fires when a TestClient context is
# entered — without this opt-out it would try to populate whatever
# content root the real env var points at (or the default cwd-relative
# fallback) with starter gradient slides, polluting local dev state.
# Seed behavior itself is covered explicitly by `test_seed.py`.
os.environ.setdefault("OPENMARQUEE_DISABLE_SEED", "1")

# Disable the lifespan auto-start of the playback loop. Production runs
# the loop continuously ("hardware always running"); test fixtures
# injecting their own loops via dependency_overrides don't need the
# real singleton leaking an asyncio task across the whole suite.
# Individual tests that need playback running call start() explicitly.
os.environ.setdefault("OPENMARQUEE_DISABLE_AUTOSTART", "1")
