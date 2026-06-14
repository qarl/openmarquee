# Transition golden fixtures + assertion spec (QA, 2026-06-13)

Deterministic decoder-friendly test videos (H.264 Main / bframes=0 / yuv420p /
untagged / 1280x720 / ~2s @24fps) for exact transition-output verification.

- golden-red.mp4   solid #FF0000
- golden-blue.mp4  solid #0000FF
- golden-quad.mp4  TL red · TR green · BL blue · BR yellow

## Coders: wrap each as a content item (type video) under a test content-root,
## then drive the renderer capture harness and assert:

1. BLACK-BG REGRESSION (the bug that shipped): render fade red->blue at
   t={0.25,0.5,0.75} via `--capture-sb-mid --fade-from <red> --fade-to <blue>
   --transition fade --capture-sb-t <t> --content-root <root>`. Assert the
   background region is a red/blue blend and NEVER black (mean luma > floor;
   red channel high near t=0, blue high near t=1). A black bg = FAIL.

2. SPATIAL/GEOMETRY: render golden-quad through wipe + iris at several t.
   Assert quadrant colors stay in their corners (no flip/rotation), and the
   transition reveals geometry correctly (e.g. iris grows from center).

3. BLEND MATH (optional, strong): for fade, expected bg ≈ red*(1-t)+blue*t;
   assert within tolerance. Bless a golden PNG per (kind,t) for SSIM>=0.95.

These run headless on the Pi (needs /dev/video10 free → stop backend in the
test harness) OR on any box with the bcm2835 decoder. The capture is offscreen.
