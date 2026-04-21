// Playback controls: a single toggle that starts/stops the backend loop.
// When playing, the /dev/preview page cycles through saved slides on the
// duration each slide is configured with.

const TEMPLATE = `
    <section class="playback">
        <button type="button" class="playback-btn primary">Play all</button>
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
    const statusEl = container.querySelector(".playback-status");

    let isRunning = false;

    function paint() {
        btn.textContent = isRunning ? "Stop" : "Play all";
        btn.classList.toggle("primary", !isRunning);
        btn.classList.toggle("danger", isRunning);
    }

    async function refresh() {
        try {
            const state = await fetchState();
            isRunning = Boolean(state.is_running);
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
            } else {
                await onStart();
                isRunning = true;
            }
            paint();
        } catch (err) {
            statusEl.textContent = err.message;
        } finally {
            btn.disabled = false;
        }
    });

    refresh();
    return { refresh };
}
