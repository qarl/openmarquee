//! r110 stage 3 commit 3.1 (2026-06-11) — Task 2 of the
//! Karl-priority transitions audit. Pure-logic tests pinning the
//! `PosterCache` invariants documented in `hdmi.rs`:
//!
//!   * Backing store: `LruMap<PathBuf, (NativeTexture, u32, u32)>`
//!   * Capacity: `POSTER_CACHE_CAPACITY = 4` (hdmi.rs:288)
//!   * Eviction-on-insert at capacity: oldest entry is returned
//!     via `InsertOutcome::evicted_lru`, hdmi.rs:3204 deletes the
//!     GL texture.
//!   * Replace-existing-key: previous value is returned via
//!     `InsertOutcome::replaced`, hdmi.rs:3207 deletes the GL
//!     texture.
//!   * `force_evict_image_caches_for_cma_pressure` (hdmi.rs:7185-7205)
//!     drains the cache and deletes every texture exactly once.
//!
//! These tests use the same `LruMap` that backs `PosterCache` AND
//! pin the `POSTER_CACHE_CAPACITY` constant. They do NOT need a
//! live GL context (the GL texture lifecycle is exercised
//! integration-test-style in hdmi.rs callers; here we pin the
//! POLICY that drives those deletes).

#![cfg(test)]

use crate::lru::LruMap;
use std::path::PathBuf;

// PosterCache stores (NativeTexture, width, height). We model
// the GL handle as u32 (which is what `glow::NativeTexture` wraps
// internally — `NonZeroU32`) for the pure-logic tests; the
// production cache holds a real `NativeTexture`. Lifecycle
// reasoning (delete-on-eviction) is independent of the wrapper
// type.
type FakeTexId = u32;
type FakePosterCache = LruMap<PathBuf, (FakeTexId, u32, u32)>;

// Production constant lives in `hdmi::POSTER_CACHE_CAPACITY`,
// but the `hdmi` module is `#[cfg(target_os = "linux")]`-gated
// (it depends on V4L2 + glow + EGL). To keep these policy tests
// runnable on the macOS dev host, we use a local literal AND
// add a Linux-gated test below that pins the local literal
// against the real constant. Any drift between the two surfaces
// in CI/cross-build (which IS Linux).
const POSTER_CAPACITY_FOR_TESTS: usize = 4;

fn make_cache() -> FakePosterCache {
    LruMap::with_capacity(POSTER_CAPACITY_FOR_TESTS)
}

fn pp(id: u32) -> PathBuf {
    PathBuf::from(format!("/content/{:08x}/poster.png", id))
}

#[cfg(target_os = "linux")]
#[test]
fn poster_cache_capacity_constant_is_four() {
    // Pin the production constant. If a future change adjusts
    // POSTER_CACHE_CAPACITY's wider implications (CMA budget,
    // multi-poster transition coverage) need a deliberate review
    // — this test forces that conversation. Also pins the local
    // POSTER_CAPACITY_FOR_TESTS against the real constant so the
    // policy assertions in the rest of this file remain
    // representative.
    assert_eq!(crate::hdmi::POSTER_CACHE_CAPACITY, 4);
    assert_eq!(POSTER_CAPACITY_FOR_TESTS, crate::hdmi::POSTER_CACHE_CAPACITY);
}

#[test]
fn insert_at_capacity_evicts_oldest_via_evicted_lru() {
    let mut cache = make_cache();
    // Fill to capacity.
    for i in 0..4 {
        let out = cache.insert(pp(i), (100 + i, 1920, 1080));
        assert!(out.evicted_lru.is_none(), "no eviction below capacity");
        assert!(out.replaced.is_none(), "fresh key, no replacement");
    }
    assert_eq!(cache.len(), 4);
    // The 5th insert with a fresh key must evict the oldest.
    let out = cache.insert(pp(4), (104, 1920, 1080));
    let (evicted_tex, _, _) = out
        .evicted_lru
        .expect("5th distinct insert must evict the oldest entry");
    assert_eq!(evicted_tex, 100, "oldest texture handle was id 100");
    assert!(out.replaced.is_none(), "fresh key, no replacement");
    assert_eq!(cache.len(), 4);
}

#[test]
fn insert_replaces_existing_key_via_replaced_no_eviction() {
    let mut cache = make_cache();
    for i in 0..4 {
        cache.insert(pp(i), (100 + i, 1920, 1080));
    }
    // Re-insert with key 1; must return the prior tex via
    // `replaced` (so the caller can glDeleteTextures it) and must
    // NOT also evict another entry.
    let out = cache.insert(pp(1), (999, 1920, 1080));
    let (replaced_tex, _, _) = out
        .replaced
        .expect("same-key insert returns prior value");
    assert_eq!(replaced_tex, 101);
    assert!(
        out.evicted_lru.is_none(),
        "same-key replace must NOT evict another entry"
    );
    assert_eq!(cache.len(), 4);
}

#[test]
fn force_evict_drains_every_entry_exactly_once() {
    // This is the load-bearing property for
    // force_evict_image_caches_for_cma_pressure: under CMA
    // pressure the cache must surrender every texture so the
    // caller can delete each via the GL context. No leak
    // (entries left behind), no double-free (same entry yielded
    // twice).
    let mut cache = make_cache();
    for i in 0..4 {
        cache.insert(pp(i), (100 + i, 1920, 1080));
    }
    let mut seen: Vec<FakeTexId> = Vec::new();
    for (_path, (tex, _w, _h)) in cache.drain() {
        seen.push(tex);
    }
    seen.sort_unstable();
    assert_eq!(seen, vec![100, 101, 102, 103], "every cached tex must drain exactly once");
    assert_eq!(cache.len(), 0, "drain leaves cache empty");
}

#[test]
fn drain_resets_capacity_so_subsequent_inserts_work() {
    let mut cache = make_cache();
    for i in 0..4 {
        cache.insert(pp(i), (100 + i, 1920, 1080));
    }
    let _: Vec<_> = cache.drain().collect();
    // Post-drain, a fresh fill works normally and does NOT evict
    // until capacity is reached again.
    for i in 100..104 {
        let out = cache.insert(pp(i), (200 + i, 1920, 1080));
        assert!(out.evicted_lru.is_none());
    }
    assert_eq!(cache.len(), 4);
}

#[test]
fn get_promotes_entry_to_most_recently_used() {
    // The hottest poster (likely the slide currently transitioning
    // INTO) should not be evicted by a fifth distinct insert. This
    // mirrors `ImageBgCache`'s policy and prevents the active
    // transition's B-side poster from being silently dropped.
    let mut cache = make_cache();
    for i in 0..4 {
        cache.insert(pp(i), (100 + i, 1920, 1080));
    }
    // Touch entry 0 — now it's MRU.
    let _ = cache.get(&pp(0));
    // 5th insert evicts the NEW LRU — entry 1, not entry 0.
    let out = cache.insert(pp(4), (104, 1920, 1080));
    let (evicted_tex, _, _) = out
        .evicted_lru
        .expect("eviction at capacity");
    assert_eq!(evicted_tex, 101, "entry 1 is now LRU after touching entry 0");
    // Entry 0 must still be present.
    assert!(cache.get(&pp(0)).is_some());
}

#[test]
fn distinct_uuid_paths_do_not_collide_in_cache_keys() {
    // Regression-lock against a future "shorten uuid in path"
    // optimisation that would silently merge cache entries for
    // distinct slides whose ids share a prefix.
    let mut cache = make_cache();
    let pa = PathBuf::from(
        "/content/deadbeef-0000-4000-8000-000000000001/poster.png",
    );
    let pb = PathBuf::from(
        "/content/deadbeef-0000-4000-8000-000000000002/poster.png",
    );
    cache.insert(pa.clone(), (1, 1920, 1080));
    cache.insert(pb.clone(), (2, 1920, 1080));
    assert_eq!(cache.len(), 2);
    assert_eq!(cache.get(&pa).map(|t| t.0), Some(1));
    assert_eq!(cache.get(&pb).map(|t| t.0), Some(2));
}

#[test]
fn drain_count_equals_len_for_complete_force_evict_logging() {
    // force_evict_image_caches_for_cma_pressure logs
    // `freed_poster = poster_cache.len()` BEFORE the drain loop
    // (hdmi.rs:7189). That count must equal the number of textures
    // actually deleted. Pin: len-before-drain == drain count.
    let mut cache = make_cache();
    for i in 0..3 {
        cache.insert(pp(i), (100 + i, 1920, 1080));
    }
    let before = cache.len();
    let drained: Vec<_> = cache.drain().collect();
    assert_eq!(before, drained.len(), "freed_poster log count must match drain count");
}
