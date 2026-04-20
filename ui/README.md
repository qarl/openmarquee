# ui

Browser-based web UI served by the backend — the captive-portal interface.

This is where the heavy lifting happens:
- Video decoded and scaled client-side with `ffmpeg.wasm` (~25 MB, shipped from the device so no internet required).
- Text slides rendered directly in the browser to a canvas at the sign's native resolution.
- Playlist management: drag-and-drop reorder, per-item duration, transitions, loop, schedules.

Talks to the backend via a small REST API. Bundler and framework are TBD; goal is something small, vanilla-ish, and easy to serve from the Pi.
