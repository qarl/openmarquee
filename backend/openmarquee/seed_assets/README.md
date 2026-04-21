# seed_assets/

Out-of-band assets that first-boot seed registers as starter content
when present. The directory is tracked via `.gitkeep`; the files
themselves aren't (they're large binaries provisioned by scripts or
the pi-gen image builder).

## Files

| File | Seed behavior | Provisioned by |
| --- | --- | --- |
| `demo.mp4` | Registered as a `VideoSlide` alongside the gradient backgrounds. Skipped silently if missing or not a valid MP4. | `scripts/download-demo-video.sh` (pulls a CC-BY Blender Foundation clip) |

## Customizing

- Swap `demo.mp4` for any short, legally redistributable H.264 MP4.
- Point `OPENMARQUEE_DEMO_VIDEO_PATH` at an arbitrary location via env
  var instead of using the default lookup here.

Everything remains a *best-effort* seed: if the asset isn't there, the
seed flow still succeeds with just the gradient backgrounds.
