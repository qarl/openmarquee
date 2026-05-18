//! SDF arc slice C.2 -- GL-bound side of the emoji color-bitmap
//! atlas pipeline. Linux-only because it depends on `glow`.
//!
//! Decode each PNG-compressed atlas page at session bring-up and
//! upload as GL_RGBA8. Pages are PNG-compressed in the binary
//! (~8 MB on disk for 4 full atlas pages) -- one-shot decode at
//! bring-up trades a few ms of startup time for ~56 MB of saved
//! binary size vs raw RGBA.
//!
//! Lifecycle: `upload_all` at `with_egl_session` bring-up
//! immediately after `make_current`. `delete_all` at session
//! teardown while the GL context is still bound.

use anyhow::{anyhow, Result};

use crate::sdf_atlas_emoji::EmojiAtlas;

/// One uploaded atlas page (one GL texture). The atlas manifest
/// owns the codepoint-to-page mapping; the runtime indexes into
/// `Vec<EmojiAtlasGl>` by page number.
pub struct EmojiAtlasGl {
    pub page: u32,
    /// Decoded width/height for diagnostic + draw-side UV math.
    /// Always equal to `manifest.atlas_dim` post-decode; recorded
    /// here so callers don't need to thread the manifest through.
    pub width: u32,
    pub height: u32,
    pub tex: glow::NativeTexture,
}

/// Decode each baked PNG page and upload as a GL_RGBA8 texture.
/// Returns one [`EmojiAtlasGl`] per page that the manifest claims
/// is in use (post-trim).
///
/// Empty placeholder pages (slot >= manifest.pages, written as
/// 0-byte files by build.rs) are detected by `pages_png[i].is_empty()`
/// and skipped without GL state -- they were never going to be
/// looked up by codepoint anyway.
///
/// LINEAR sampling for both min + mag: emoji upscale beyond the
/// 96x96 cell uses bilerp, which is fine for emoji (slight blur
/// > pixel-snap blocky). CLAMP_TO_EDGE so tile edges don't bleed
/// across cell boundaries when the draw quad's UVs are at the
/// cell rectangle's exact corners.
pub fn upload_all(
    gl: &glow::Context,
    atlas: &EmojiAtlas,
) -> Result<Vec<EmojiAtlasGl>> {
    use glow::HasContext;

    let mut out = Vec::with_capacity(atlas.pages_png.len());
    unsafe {
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
        for (page_idx, png_bytes) in atlas.pages_png.iter().enumerate() {
            if png_bytes.is_empty() {
                // Placeholder slot beyond manifest.pages. Should not
                // happen post-trim (load_emoji_atlas already trims
                // pages_png to manifest.pages) but be defensive.
                continue;
            }
            let (rgba, w, h) = decode_emoji_png(png_bytes)
                .map_err(|e| anyhow!("decode emoji page {page_idx}: {e}"))?;
            if w != atlas.manifest.atlas_dim || h != atlas.manifest.atlas_dim {
                return Err(anyhow!(
                    "emoji page {page_idx} decoded as {}x{} but manifest says {}x{}",
                    w, h, atlas.manifest.atlas_dim, atlas.manifest.atlas_dim,
                ));
            }
            let tex = gl
                .create_texture()
                .map_err(|e| anyhow!("glGenTextures(emoji page {page_idx}): {e}"))?;
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            gl.tex_image_2d(
                glow::TEXTURE_2D,
                0,
                glow::RGBA as i32,
                w as i32,
                h as i32,
                0,
                glow::RGBA,
                glow::UNSIGNED_BYTE,
                Some(&rgba),
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
                return Err(anyhow!(
                    "emoji page {page_idx} upload failed: GL error 0x{err:x}",
                ));
            }
            out.push(EmojiAtlasGl {
                page: page_idx as u32,
                width: w,
                height: h,
                tex,
            });
        }
        gl.bind_texture(glow::TEXTURE_2D, None);
    }
    eprintln!(
        "emoji: uploaded {} atlas pages ({} MB total RGBA)",
        out.len(),
        out.iter()
            .map(|p| (p.width * p.height * 4) as usize)
            .sum::<usize>()
            / (1024 * 1024),
    );
    Ok(out)
}

/// Decode an emoji atlas PNG payload into RGBA8 bytes + dimensions.
/// Slice C.1 encodes atlases as straight RGBA8 PNGs so the runtime
/// decoder doesn't have to handle palettes -- but defensive
/// transformations (EXPAND | ALPHA) are still set so a future
/// atlas-encoding change doesn't silently break the runtime.
fn decode_emoji_png(data: &[u8]) -> Result<(Vec<u8>, u32, u32), String> {
    let mut decoder = png::Decoder::new(data);
    decoder.set_transformations(
        png::Transformations::EXPAND | png::Transformations::ALPHA,
    );
    let mut reader = decoder
        .read_info()
        .map_err(|e| format!("read_info: {e}"))?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader
        .next_frame(&mut buf)
        .map_err(|e| format!("next_frame: {e}"))?;
    buf.truncate(info.buffer_size());
    let w = info.width;
    let h = info.height;
    // Expect RGBA post-transformations. Match the build-side normalization.
    match info.color_type {
        png::ColorType::Rgba => Ok((buf, w, h)),
        png::ColorType::Rgb => {
            let mut out = Vec::with_capacity(buf.len() * 4 / 3);
            for px in buf.chunks_exact(3) {
                out.extend_from_slice(&[px[0], px[1], px[2], 255]);
            }
            Ok((out, w, h))
        }
        other => Err(format!(
            "unexpected png color type after EXPAND|ALPHA: {other:?}"
        )),
    }
}

/// Delete every uploaded atlas texture. Idempotent on an empty Vec.
pub fn delete_all(gl: &glow::Context, atlases: &mut Vec<EmojiAtlasGl>) {
    use glow::HasContext;
    for atlas in atlases.drain(..) {
        unsafe {
            gl.delete_texture(atlas.tex);
        }
    }
}

/// Find an uploaded atlas page by page number. Returns None if the
/// page wasn't uploaded (e.g. trimmed empty placeholder slot or a
/// page index out of range).
#[allow(dead_code)] // wired by C.3
pub fn atlas_for_page<'a>(
    atlases: &'a [EmojiAtlasGl],
    page: u32,
) -> Option<&'a EmojiAtlasGl> {
    atlases.iter().find(|a| a.page == page)
}
