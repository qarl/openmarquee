// Playback controls: a single toggle that starts/stops the backend loop.
// When playing, the /dev/preview page cycles through saved slides on the
// duration each slide is configured with.

const TEMPLATE = `
    <section class="playback">
        <button type="button" class="playback-btn primary">Play all</button>
        <button type="button" class="playback-simulator"
                title="Open a pop-out window that simulates the configured device (HDMI / HUB75 / WS2812B / composite)">
            Open simulator ↗
        </button>
        <p class="playback-now-playing" role="status" aria-live="polite"></p>
        <p class="playback-status" role="status" aria-live="polite"></p>
    </section>
`;

/**
 * Mount the playback controls into `container`.
 *
 * @param {HTMLElement} container — parent element (emptied and replaced).
 * @param {object} options
 * @param {() => Promise<{is_running: boolean}>} options.fetchState
 * @param {() => Promise<void>} options.onStart
 * @param {() => Promise<void>} options.onStop
 * @returns {{ refresh: () => Promise<void> }}
 */
export function mountPlaybackControls(container, { fetchState, onStart, onStop }) {
    container.innerHTML = TEMPLATE;
    const btn = container.querySelector(".playback-btn");
    const simulatorBtn = container.querySelector(".playback-simulator");
    const statusEl = container.querySelector(".playback-status");
    const nowPlayingEl = container.querySelector(".playback-now-playing");

    simulatorBtn.addEventListener("click", () => {
        // Named window so repeated clicks focus the existing simulator
        // instead of stacking popups. Features are a hint — browsers
        // may ignore them when the call isn't in direct response to a
        // user gesture, but a click IS a gesture so we usually get the
        // right size. The simulator's own boot code re-calls
        // window.resizeTo once it knows the configured aspect ratio.
        const popup = window.open(
            "/simulator.html",
            "openMarquee-simulator",
            "popup=yes,width=960,height=720",
        );
        if (popup) popup.focus();
    });

    let isRunning = false;
    let currentPlaylistName = null;
    let pollTimer = null;

    function paint() {
        btn.textContent = isRunning ? "Stop" : "Play all";
        btn.classList.toggle("primary", !isRunning);
        btn.classList.toggle("danger", isRunning);
        if (isRunning && currentPlaylistName) {
            nowPlayingEl.textContent = `Now playing: ${currentPlaylistName}`;
        } else if (isRunning) {
            nowPlayingEl.textContent = "Running…";
        } else {
            nowPlayingEl.textContent = "";
        }
    }

    async function refresh() {
        try {
            const state = await fetchState();
            isRunning = Boolean(state.is_running);
            currentPlaylistName = state.current_playlist_name || null;
            paint();
        } catch (err) {
            statusEl.textContent = `Could not read playback state: ${err.message}`;
        }
    }

    btn.addEventListener("click", async () => {
        btn.disabled = true;
        statusEl.textContent = "";
        try {
            if (isRunning) {
                await onStop();
                isRunning = false;
                currentPlaylistName = null;
            } else {
                await onStart();
                isRunning = true;
            }
            paint();
            // Right after start, the loop hasn't necessarily set the playlist
            // name yet. Re-poll quickly to catch up.
            setTimeout(refresh, 200);
        } catch (err) {
            statusEl.textContent = err.message;
        } finally {
            btn.disabled = false;
        }
    });

    refresh();
    // Light polling so the UI catches schedule-driven playlist switches and
    // any external state changes (other tab, etc.). Cheap GET every 5s.
    pollTimer = setInterval(refresh, 5000);

    return {
        refresh,
        // For tests + future cleanup paths.
        stopPolling: () => {
            if (pollTimer !== null) {
                clearInterval(pollTimer);
                pollTimer = null;
            }
        },
    };
}
