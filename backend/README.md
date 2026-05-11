# backend

FastAPI application that runs on the device.

Responsibilities:
- Serve the web UI (`../ui/`) over HTTP on the captive-portal AP.
- Handle content uploads (MP4 from `ffmpeg.wasm` in the browser, image PNG/JPG, text-slide PNG renderings), playlist and schedule CRUD, brightness and system settings.
- Drive the playback engine — HDMI via `ffmpeg`/`vlc` → framebuffer, or HUB75 via `hzeller/rpi-rgb-led-matrix` bindings.

State lives on the SD card as files. Playlists and schedules are JSON. No database.
