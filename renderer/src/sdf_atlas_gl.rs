//! SDF arc slice B.2 -- GL-bound side of the MSDF atlas pipeline.
//!
//! Linux-only because it depends on `glow`. Cross-platform parsing
//! + plane-bounds + lookup lives in `sdf_atlas.rs`; this module
//! takes the parsed `Vec<MsdfAtlas>` and uploads each atlas as a
//! GL_RGB / GL_UNSIGNED_BYTE texture, then exposes the per-stem
//! `glow::NativeTexture` for `draw_text_layer_msdf` to bind.
//!
//! Lifecycle: `upload_all` runs once at `with_egl_session`
//! bring-up immediately after `make_current`. `delete_all` runs
//! at session teardown while the GL context is still bound. The
//! `MsdfAtlasGl` Vec is stored on `EglSession`.

use anyhow::{anyhow, Result};

use crate::sdf_atlas::{AtlasManifest, MsdfAtlas};

/// One atlas, ready to bind at draw time.
pub struct MsdfAtlasGl {
    pub stem: String,
    pub manifest: AtlasManifest,
    pub tex: glow::NativeTexture,
}

/// Upload every parsed atlas as a GL_RGB texture. Returns the
/// per-atlas GL handles + manifests for runtime use.
///
/// `gl.tex_image_2d` with internal_format = GL_RGB, format = GL_RGB,
/// type = GL_UNSIGNED_BYTE; the slice A atlas blob is exactly
/// `atlas_w * atlas_h * 3` raw RGB888 bytes, row-major, top-left
/// origin (matches our atlas upload convention).
///
/// LINEAR filter (the MSDF shader does its own threshold/AA; we
/// want the GL sampler to bilerp distance values smoothly so AA
/// edges stay clean across magnification). CLAMP_TO_EDGE matches
/// the existing glyph texture convention.
///
/// UNPACK_ALIGNMENT temporarily forced to 1 because RGB rows aren't
/// 4-byte aligned at arbitrary atlas widths; restore to default
/// (4) after the upload so other textures stay on the GL default
/// fast path.
pub fn upload_all(
    gl: &glow::Context,
    atlases: &[MsdfAtlas],
) -> Result<Vec<MsdfAtlasGl>> {
    use glow::HasContext;

    // r41 (2026-06-02): early-return on a per-iteration failure
    // previously dropped `out: Vec<MsdfAtlasGl>` without releasing
    // the GL texture handles it carried -- glow's NativeTexture is
    // a u32 alias, no Drop semantics. delete_all() only runs at
    // session teardown via the success path; an Err from
    // upload_all means the caller never receives `out`, so its
    // textures live until the GL context dies. Wrap both bubble
    // sites + the success path's pixel_store restore so a partial
    // upload doesn't leak prior textures or leave GL state on
    // UNPACK_ALIGNMENT=1. Session-startup is fatal-on-fail today,
    // but the canonical pattern survives future refactors that
    // might want to retry / continue. See qa/r41-capture-startup-
    // cleanup-2026-06-02.md.
    let cleanup_partial = |gl: &glow::Context, out: &mut Vec<MsdfAtlasGl>| unsafe {
        for entry in out.drain(..) {
            gl.delete_texture(entry.tex);
        }
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
        gl.bind_texture(glow::TEXTURE_2D, None);
    };

    let mut out = Vec::with_capacity(atlases.len());
    unsafe {
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
        for atlas in atlases {
            let tex = match gl.create_texture() {
                Ok(t) => t,
                Err(e) => {
                    cleanup_partial(gl, &mut out);
                    return Err(anyhow!(
                        "glGenTextures(msdf {}): {e}",
                        atlas.manifest.font
                    ));
                }
            };
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            gl.tex_image_2d(
                glow::TEXTURE_2D,
                0,
                glow::RGB as i32,
                atlas.manifest.atlas_w as i32,
                atlas.manifest.atlas_h as i32,
                0,
                glow::RGB,
                glow::UNSIGNED_BYTE,
                Some(atlas.atlas_rgb),
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MIN_FILTER,
                glow::LINEAR as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MAG_FILTER,
                glow::LINEAR as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_WRAP_S,
                glow::CLAMP_TO_EDGE as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_WRAP_T,
                glow::CLAMP_TO_EDGE as i32,
            );
            let err = gl.get_error();
            if err != 0 {
                gl.delete_texture(tex);
                cleanup_partial(gl, &mut out);
                return Err(anyhow!(
                    "msdf atlas {} upload failed: GL error 0x{err:x}",
                    atlas.manifest.font
                ));
            }
            out.push(MsdfAtlasGl {
                stem: atlas.manifest.font.clone(),
                manifest: atlas.manifest.clone(),
                tex,
            });
        }
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
        gl.bind_texture(glow::TEXTURE_2D, None);
    }
    eprintln!(
        "msdf: uploaded {} atlases ({} MB total)",
        out.len(),
        out.iter()
            .map(|a| (a.manifest.atlas_w * a.manifest.atlas_h * 3) as usize)
            .sum::<usize>() / (1024 * 1024)
    );
    Ok(out)
}

/// Delete every uploaded atlas texture. Called at session teardown
/// while the GL context is still bound. Idempotent: if called with
/// an already-emptied Vec, no-op.
pub fn delete_all(gl: &glow::Context, atlases: &mut Vec<MsdfAtlasGl>) {
    use glow::HasContext;
    for atlas in atlases.drain(..) {
        unsafe { gl.delete_texture(atlas.tex); }
    }
}

/// Find an uploaded atlas by font stem (matches
/// `font_family_to_filename`'s output without the `.ttf` suffix).
/// Returns `None` if the font isn't in the baked set (e.g.
/// noto-color-emoji is slice C, not B).
pub fn atlas_for_stem<'a>(
    atlases: &'a [MsdfAtlasGl],
    stem: &str,
) -> Option<&'a MsdfAtlasGl> {
    atlases.iter().find(|a| a.stem == stem)
}
