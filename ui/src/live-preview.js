// Live preview: shows the currently-playing slide animating in the
// browser, mirroring the playlist as the device would render it. Polls
// /api/playback/state; swaps between <video> and <img> based on the
// current item's type.
//
// Design: purely client-side rendering. The server-side playback loop
// drives the physical renderer (MockRenderer in dev, HUB75/HDMI etc.
// on device); this widget is parallel — it watches the same state
// stream and reproduces the slide locally. That means videos actually
// play in the preview even though the current loop renders only the
// thumbnail to disk. Which is the point: the preview shows what the
// operator's playlist *is*, not what today's MockRenderer happens to
// draw.

const TEMPLATE = `
    <section class="live-preview" aria-label="live playlist preview">
        <div class="live-preview-stage">
            <div class="live-preview-idle">
                Press <strong>Play all</strong> to preview the playlist.
            </div>
        </div>
        <p class="live-preview-caption" role="status" aria-live="polite"></p>
    </section>
`;

const POLL_INTERVAL_MS = 500;

/**
 * Mount the live-preview widget into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {number} options.width  — sign width in pixels (drives preview aspect)
 * @param {number} options.height — sign height in pixels
 * @param {() => Promise<{is_running, current_item_id, current_item_type, current_playlist_name}>} options.fetchState
 * @param {number} [options.pollIntervalMs] — override poll cadence (tests)
 * @returns {{ stop: () => void, refresh: () => Promise<void> }}
 */
export function mountLivePreview(container, options) {
    const {
        width,
        height,
        fetchState,
        pollIntervalMs = POLL_INTERVAL_MS,
    } = options;

    container.innerHTML = TEMPLATE;
    const stage = container.querySelector(".live-preview-stage");
    const caption = container.querySelector(".live-preview-caption");

    stage.style.aspectRatio = `${width} / ${height}`;

    // Track what's currently on screen so we can diff against the next
    // poll and only swap elements when the item actually changes (avoids
    // re-loading the <video> every 500ms, which would reset playback).
    let currentId = null;
    let currentType = null;
    let pollTimer = null;
    let stopped = false;

    function renderIdle(message) {
        stage.innerHTML = `<div class="live-preview-idle">${escapeHtml(message)}</div>`;
        currentId = null;
        currentType = null;
    }

    function renderSlide(id, type) {
        // Bust the cache with the id so replayed playlists pick up any
        // mid-loop asset edits; not perfect (the asset URL doesn't
        // include created_at here because we don't fetch the item), but
        // close enough — a stale-for-500ms preview is fine.
        const assetUrl = `/api/content/${id}/asset`;
        const videoUrl = `/api/content/${id}/video`;
        if (type === "video") {
            stage.innerHTML = `
                <video class="live-preview-media" autoplay muted playsinline loop
                       src="${videoUrl}" aria-label="live video preview"></video>
            `;
        } else {
            // text_slide + image both have a rendered PNG at /asset.
            stage.innerHTML = `
                <img class="live-preview-media" alt="live slide preview" src="${assetUrl}">
            `;
        }
        currentId = id;
        currentType = type;
    }

    async function refresh() {
        if (stopped) return;
        let state;
        try {
            state = await fetchState();
        } catch (err) {
            caption.textContent = `Preview paused: ${err.message}`;
            return;
        }

        const running = Boolean(state.is_running);
        const id = state.current_item_id || null;
        const type = state.current_item_type || null;

        if (!running) {
            renderIdle("Press Play all to preview the playlist.");
            caption.textContent = "";
            return;
        }

        if (!id) {
            renderIdle("Waiting for the first slide…");
            caption.textContent = state.current_playlist_name
                ? `Playing: ${state.current_playlist_name}`
                : "";
            return;
        }

        if (id !== currentId || type !== currentType) {
            renderSlide(id, type);
        }
        caption.textContent = state.current_playlist_name
            ? `Playing: ${state.current_playlist_name}`
            : "";
    }

    refresh();
    pollTimer = setInterval(refresh, pollIntervalMs);

    return {
        refresh,
        stop: () => {
            stopped = true;
            if (pollTimer !== null) {
                clearInterval(pollTimer);
                pollTimer = null;
            }
        },
    };
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
