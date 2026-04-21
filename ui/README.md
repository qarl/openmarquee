# ui

Browser-based web UI served by the backend — the captive-portal interface.

This is where the heavy lifting happens:
- Video decoded and scaled client-side with `ffmpeg.wasm` (~25 MB, shipped from the device so no internet required).
- Text slides rendered directly in the browser to a canvas at the sign's native resolution.
- Playlist management: drag-and-drop reorder, per-item duration, transitions, loop, schedules.

Talks to the backend via a small REST API. Bundler is **esbuild**, test runner is **vitest**. No framework.

## Local dev

On a normal filesystem: `npm install`, then `npm test` or `npm run dev`.

If your project tree lives on an rclone mount that doesn't preserve POSIX exec bits (qarl's setup), run `bash ../scripts/setup.sh` instead — it installs Node deps to `~/tmp/openmarquee-deps/ui/` and symlinks `node_modules` back into this directory. Re-run after any change to `package.json`.
