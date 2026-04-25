// UI-side constants shared across modules.
//
// The default playlist's UUID is well-known and stable across all
// devices — kept in sync with backend's openmarquee.playlist.DEFAULT_PLAYLIST_ID.
// Used by main.js (currentPlaylistId initial value), playlist-browser.js
// (default-first sort), and schedule.js (fallback when an existing
// schedule references a playlist that no longer resolves).
export const DEFAULT_PLAYLIST_ID = "00000000-0000-4000-8000-000000000001";
