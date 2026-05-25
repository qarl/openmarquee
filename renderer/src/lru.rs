//! v1-spec-delta #12 (image-bg eviction): bounded-capacity LRU
//! map. Cross-platform (no GL/DRM deps) so the eviction policy
//! is testable on the Mac dev box -- hdmi.rs is Linux-only.
//!
//! Used as the backing store for `hdmi::ImageBgCache` (keyed on
//! the asset PathBuf, valued at (NativeTexture, w, h)). The
//! caller (draw_image_bg) holds the gl context; on insert when
//! at capacity, the evicted entry's value is handed back via
//! `InsertOutcome` for the caller to free via gl.delete_texture.
//! The cache only owns the LRU policy, never the GPU resource.
//!
//! Capacity is operator-configurable via the type alias's caller;
//! the budget §4 hard ceiling for image-bg is 6 entries.

use std::collections::{HashMap, VecDeque};
use std::hash::Hash;

/// Outcome of `LruMap::insert`. When the cache is at capacity and
/// the inserted key is new, the LRU entry's value lands in
/// `evicted_lru`. When the inserted key already existed, the old
/// value lands in `replaced`. Both are Some only when the caller
/// owns external state (e.g. a GPU texture handle) tied to the
/// value lifetime; the caller must drop them.
#[derive(Default)]
pub struct InsertOutcome<V> {
    pub evicted_lru: Option<V>,
    pub replaced: Option<V>,
}

pub struct LruMap<K, V>
where
    K: Hash + Eq + Clone,
{
    map: HashMap<K, V>,
    /// Access order: front = least-recently used, back = most-recent.
    order: VecDeque<K>,
    capacity: usize,
}

impl<K, V> Default for LruMap<K, V>
where
    K: Hash + Eq + Clone,
{
    fn default() -> Self {
        Self::with_capacity(1)
    }
}

impl<K, V> LruMap<K, V>
where
    K: Hash + Eq + Clone,
{
    /// New LRU map with the given capacity. A capacity of 0 is
    /// clamped to 1 so the cache always has at least one slot --
    /// otherwise every insert immediately evicts itself.
    pub fn with_capacity(capacity: usize) -> Self {
        let capacity = capacity.max(1);
        Self {
            map: HashMap::with_capacity(capacity),
            order: VecDeque::with_capacity(capacity),
            capacity,
        }
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    /// Lookup with LRU touch -- a hit moves the key to back-of-
    /// order so a subsequent insert evicts colder entries first.
    /// O(capacity) per call (linear retain over the deque); fine
    /// at the budget §4 cap of 6.
    ///
    /// Generic over Borrow<Q> to match HashMap's contract: callers
    /// can look up `&PathBuf`-keyed entries with `&Path` etc.
    pub fn get<Q>(&mut self, key: &Q) -> Option<&V>
    where
        K: std::borrow::Borrow<Q>,
        Q: Hash + Eq + ?Sized,
    {
        if !self.map.contains_key(key) {
            return None;
        }
        // Move the matching K within the order deque (without
        // cloning -- find by borrow + remove + push_back).
        if let Some(pos) = self.order.iter().position(|k| k.borrow() == key) {
            if let Some(k) = self.order.remove(pos) {
                self.order.push_back(k);
            }
        }
        self.map.get(key)
    }

    /// Insert. Returns the evicted LRU entry's value (if at
    /// capacity AND the key is new) and the replaced value (if
    /// the key already existed). Caller owns dropping any
    /// external resources tied to those values.
    ///
    /// Existing-key path moves the K already in `order` to the
    /// back without cloning it — at 30 fps with a warm
    /// ImageBgCache (cap=6) that is the hot path, and the prior
    /// touch(&key) clone was a PathBuf::clone (heap alloc + OS-
    /// string byte copy) per frame for no semantic reason. New-
    /// key path still needs an owned copy: one goes into `order`,
    /// the other into `map`'s key slot.
    pub fn insert(&mut self, key: K, value: V) -> InsertOutcome<V> {
        let mut evicted_lru: Option<V> = None;
        let key_existed = self.map.contains_key(&key);
        if !key_existed && self.map.len() >= self.capacity {
            if let Some(lru_key) = self.order.pop_front() {
                evicted_lru = self.map.remove(&lru_key);
            }
        }
        if key_existed {
            // Move the existing K within `order` to the back
            // without cloning. position+remove+push_back mirrors
            // touch() but reuses the owned K already in the deque.
            if let Some(pos) = self.order.iter().position(|k| k == &key) {
                if let Some(k) = self.order.remove(pos) {
                    self.order.push_back(k);
                }
            }
        } else {
            // New key needs an owned copy for `order`; another
            // copy goes into `map` below.
            self.order.push_back(key.clone());
        }
        let replaced = self.map.insert(key, value);
        InsertOutcome { evicted_lru, replaced }
    }

    /// Drain all entries. Resets the access order; map.drain
    /// hands the values back to the caller for resource cleanup.
    pub fn drain(&mut self) -> std::collections::hash_map::Drain<'_, K, V> {
        self.order.clear();
        self.map.drain()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn insert_under_capacity_does_not_evict() {
        let mut c: LruMap<&'static str, u32> = LruMap::with_capacity(3);
        let o = c.insert("a", 1);
        assert!(o.evicted_lru.is_none());
        assert!(o.replaced.is_none());
        let o = c.insert("b", 2);
        assert!(o.evicted_lru.is_none());
        let o = c.insert("c", 3);
        assert!(o.evicted_lru.is_none());
        assert_eq!(c.len(), 3);
    }

    #[test]
    fn insert_at_capacity_evicts_least_recent() {
        let mut c: LruMap<&'static str, u32> = LruMap::with_capacity(3);
        c.insert("a", 1);
        c.insert("b", 2);
        c.insert("c", 3);
        // Insert d; LRU is a (oldest).
        let o = c.insert("d", 4);
        assert_eq!(o.evicted_lru, Some(1));
        assert_eq!(c.len(), 3);
        assert!(c.get(&"a").is_none()); // Evicted.
        assert!(c.get(&"d").is_some());
    }

    #[test]
    fn get_touches_to_most_recent() {
        let mut c: LruMap<&'static str, u32> = LruMap::with_capacity(3);
        c.insert("a", 1);
        c.insert("b", 2);
        c.insert("c", 3);
        // Touch a -- now LRU is b.
        let _ = c.get(&"a");
        let o = c.insert("d", 4);
        assert_eq!(o.evicted_lru, Some(2)); // b evicted, not a.
        assert!(c.get(&"a").is_some());
        assert!(c.get(&"b").is_none());
    }

    #[test]
    fn insert_existing_key_returns_replaced_no_evict() {
        let mut c: LruMap<&'static str, u32> = LruMap::with_capacity(3);
        c.insert("a", 1);
        c.insert("b", 2);
        c.insert("c", 3);
        // Replace b at capacity -- no eviction (key already there).
        // Picking b (not c) so the move-to-back assertion below has
        // a meaningful effect (c is already at back; b is in middle).
        let o = c.insert("b", 20);
        assert!(o.evicted_lru.is_none());
        assert_eq!(o.replaced, Some(2));
        assert_eq!(c.len(), 3);
        // Existing-key insert must still move the key to back-of-
        // order (most-recent) so the next eviction targets the
        // truly-stalest entry. Locks the no-clone branch in insert.
        assert_eq!(c.order.back(), Some(&"b"));
        assert_eq!(c.order.front(), Some(&"a"));
    }

    #[test]
    fn capacity_zero_clamps_to_one() {
        let c: LruMap<&'static str, u32> = LruMap::with_capacity(0);
        assert_eq!(c.capacity(), 1);
    }

    #[test]
    fn drain_returns_all_entries_and_resets_order() {
        let mut c: LruMap<&'static str, u32> = LruMap::with_capacity(3);
        c.insert("a", 1);
        c.insert("b", 2);
        c.insert("c", 3);
        let drained: Vec<_> = c.drain().collect();
        assert_eq!(drained.len(), 3);
        assert!(c.is_empty());
        assert_eq!(c.len(), 0);
        // Re-insert after drain works (order was reset).
        c.insert("d", 4);
        assert_eq!(c.len(), 1);
    }

    #[test]
    fn cycling_more_than_capacity_keeps_only_recent() {
        let mut c: LruMap<u32, u32> = LruMap::with_capacity(3);
        let mut evicted_count = 0;
        for i in 0..10_u32 {
            let o = c.insert(i, i * 10);
            if o.evicted_lru.is_some() {
                evicted_count += 1;
            }
        }
        assert_eq!(c.len(), 3);
        assert_eq!(evicted_count, 7); // 10 inserts - 3 cap = 7 evictions.
        assert!(c.get(&7).is_some());
        assert!(c.get(&8).is_some());
        assert!(c.get(&9).is_some());
        assert!(c.get(&0).is_none());
    }

    #[test]
    fn touch_via_get_then_insert_evicts_other_entry() {
        let mut c: LruMap<u32, u32> = LruMap::with_capacity(2);
        c.insert(1, 10);
        c.insert(2, 20);
        // Touch 1 -- now 2 is LRU.
        let _ = c.get(&1);
        let o = c.insert(3, 30);
        assert_eq!(o.evicted_lru, Some(20));
        assert!(c.get(&1).is_some());
        assert!(c.get(&2).is_none());
        assert!(c.get(&3).is_some());
    }
}
