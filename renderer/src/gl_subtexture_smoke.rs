// Bug 3 Slice 1 prep (2026-05-19) — standalone glTexSubImage2D
// smoke test on the vc4 driver. Verifies that:
//   1. A 2048×2048 RGBA8 texture can be created via glTexImage2D.
//   2. glTexSubImage2D correctly writes a 48×48 sub-region.
//   3. The remaining pixels (outside the sub-region) stay intact.
//   4. A non-4-aligned sub-region width also works (UNPACK_ALIGNMENT=1).
//
// Reads back via FBO + glReadPixels.
//
// Reason this is its own module + smoke flag: glTexSubImage2D is not
// currently used anywhere in the renderer (the existing MSDF + emoji
// atlases both use full glTexImage2D at boot). The Bug 3 runtime
// glyph cache will be built on incremental sub-texture upload. We
// verify the underlying driver supports it cleanly before committing
// the atlas-management infrastructure that depends on it.
//
// Expected lifetime: this module is deleted once Slice 1 lands +
// is exercised by real workloads.

use anyhow::{anyhow, Result};
use glow::HasContext;

use crate::Card;
use crate::hdmi;

/// Result of the smoke test. Each sub-check reports a tag so a
/// failure surfaces which step diverged.
#[derive(Debug, Clone)]
pub struct SmokeReport {
    pub steps: Vec<(&'static str, bool, String)>,
}

impl SmokeReport {
    fn check(&mut self, tag: &'static str, ok: bool, detail: impl Into<String>) {
        self.steps.push((tag, ok, detail.into()));
    }
    pub fn all_pass(&self) -> bool {
        self.steps.iter().all(|(_, ok, _)| *ok)
    }
}

pub fn run(card: &Card) -> Result<SmokeReport> {
    // Caller is responsible for stopping the openmarquee-backend
    // systemd unit before running the smoke so DRM master is free,
    // same convention as --play-pattern-test in the smoke harness.
    hdmi::run_in_egl_session(card, 0, |session| {
        let gl = session.gl();
        let mut report = SmokeReport { steps: Vec::new() };

        unsafe {
            // === Test 1: RGBA8 2048×2048 base texture, 48×48 sub-region ===
            let tex = gl.create_texture()
                .map_err(|e| anyhow!("create_texture failed: {e}"))?;
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::NEAREST as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::NEAREST as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);

            // 2048×2048 RGBA8, initially solid red (255, 0, 0, 255).
            const ATLAS_W: i32 = 2048;
            const ATLAS_H: i32 = 2048;
            let red_full: Vec<u8> = (0..ATLAS_W * ATLAS_H)
                .flat_map(|_| [255u8, 0, 0, 255])
                .collect();
            gl.tex_image_2d(
                glow::TEXTURE_2D, 0,
                glow::RGBA as i32,
                ATLAS_W, ATLAS_H, 0,
                glow::RGBA, glow::UNSIGNED_BYTE,
                Some(&red_full),
            );
            check_gl_error(gl, "tex_image_2d RGBA8 2048×2048", &mut report);

            // 48×48 green sub-region payload (0, 255, 0, 255 each pixel).
            const SUB_W: i32 = 48;
            const SUB_H: i32 = 48;
            const SUB_X: i32 = 40;
            const SUB_Y: i32 = 40;
            let green_sub: Vec<u8> = (0..SUB_W * SUB_H)
                .flat_map(|_| [0u8, 255, 0, 255])
                .collect();
            gl.tex_sub_image_2d(
                glow::TEXTURE_2D, 0,
                SUB_X, SUB_Y, SUB_W, SUB_H,
                glow::RGBA, glow::UNSIGNED_BYTE,
                glow::PixelUnpackData::Slice(&green_sub),
            );
            check_gl_error(gl, "tex_sub_image_2d 48×48 @ (40,40)", &mut report);

            // Attach to an FBO so glReadPixels can read it back.
            let fbo = gl.create_framebuffer()
                .map_err(|e| anyhow!("create_framebuffer failed: {e}"))?;
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            gl.framebuffer_texture_2d(
                glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0,
                glow::TEXTURE_2D, Some(tex), 0,
            );
            let status = gl.check_framebuffer_status(glow::FRAMEBUFFER);
            report.check(
                "fbo_status_t1",
                status == glow::FRAMEBUFFER_COMPLETE,
                format!("status=0x{:x}", status),
            );

            // Probe pixels at known coordinates.
            let probes: &[(i32, i32, [u8; 4], &'static str)] = &[
                (10, 10, [255, 0, 0, 255], "outside_red_topleft"),
                (40, 40, [0, 255, 0, 255], "inside_green_tl_corner"),
                (60, 60, [0, 255, 0, 255], "inside_green_middle"),
                (87, 87, [0, 255, 0, 255], "inside_green_br_corner"),
                (88, 88, [255, 0, 0, 255], "outside_red_just_past_br"),
                (39, 39, [255, 0, 0, 255], "outside_red_just_before_tl"),
                (1000, 1000, [255, 0, 0, 255], "outside_red_far"),
            ];
            for &(x, y, expected, tag) in probes {
                let actual = read_pixel(gl, x, y)?;
                let ok = actual == expected;
                report.check(
                    tag, ok,
                    format!("at ({x},{y}) got={:?} want={:?}", actual, expected),
                );
            }

            // === Test 2: non-4-aligned width (47×48), UNPACK_ALIGNMENT semantics ===
            // 47×4 = 188 bytes/row (4-aligned in RGBA), but in RGB it
            // would be 141 (not 4-aligned). We use RGBA so all widths
            // are naturally 4-aligned; this sub-test really validates
            // an ODD width works at all on vc4.
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            const SUB2_W: i32 = 47;
            const SUB2_H: i32 = 48;
            const SUB2_X: i32 = 200;
            const SUB2_Y: i32 = 200;
            let blue_sub: Vec<u8> = (0..SUB2_W * SUB2_H)
                .flat_map(|_| [0u8, 0, 255, 255])
                .collect();
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
            gl.tex_sub_image_2d(
                glow::TEXTURE_2D, 0,
                SUB2_X, SUB2_Y, SUB2_W, SUB2_H,
                glow::RGBA, glow::UNSIGNED_BYTE,
                glow::PixelUnpackData::Slice(&blue_sub),
            );
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
            check_gl_error(gl, "tex_sub_image_2d 47×48 @ (200,200) UNPACK_ALIGNMENT=1", &mut report);
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            let probes2: &[(i32, i32, [u8; 4], &'static str)] = &[
                (200, 200, [0, 0, 255, 255], "blue_tl_corner_t2"),
                (246, 247, [0, 0, 255, 255], "blue_br_corner_t2"),
                (247, 247, [255, 0, 0, 255], "red_just_past_47_t2"),
                (200, 248, [255, 0, 0, 255], "red_just_past_48_t2"),
            ];
            for &(x, y, expected, tag) in probes2 {
                let actual = read_pixel(gl, x, y)?;
                let ok = actual == expected;
                report.check(
                    tag, ok,
                    format!("at ({x},{y}) got={:?} want={:?}", actual, expected),
                );
            }

            // === Cleanup ===
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            gl.delete_framebuffer(fbo);
            gl.delete_texture(tex);
        }

        Ok(report)
    })
}

unsafe fn read_pixel(gl: &glow::Context, x: i32, y: i32) -> Result<[u8; 4]> {
    let mut buf = [0u8; 4];
    gl.read_pixels(
        x, y, 1, 1,
        glow::RGBA, glow::UNSIGNED_BYTE,
        glow::PixelPackData::Slice(&mut buf),
    );
    let err = gl.get_error();
    if err != glow::NO_ERROR {
        return Err(anyhow!("glReadPixels at ({x},{y}) returned err=0x{:x}", err));
    }
    Ok(buf)
}

unsafe fn check_gl_error(gl: &glow::Context, tag: &'static str, report: &mut SmokeReport) {
    let err = gl.get_error();
    report.check(
        tag,
        err == glow::NO_ERROR,
        format!("gl_error=0x{:x}", err),
    );
}

/// Format the smoke report as a series of lines, one per check.
/// Prepended with a PASS/FAIL summary line.
pub fn format_report(r: &SmokeReport) -> String {
    let mut out = String::new();
    let pass = r.all_pass();
    out.push_str(&format!(
        "GL sub-texture smoke: {} ({} checks)\n",
        if pass { "PASS" } else { "FAIL" },
        r.steps.len(),
    ));
    for (tag, ok, detail) in &r.steps {
        out.push_str(&format!(
            "  [{}] {} -- {}\n",
            if *ok { "OK" } else { "XX" },
            tag,
            detail,
        ));
    }
    out
}

