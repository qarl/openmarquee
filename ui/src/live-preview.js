// Live preview: shows the currently-playing slide animating in the
// browser, mirroring the playlist as the device would render it.
//
// Polls /api/playback/state; swaps between <video> and <img> based on
// the current item's type + pipeline. When /api/playback/state reports
// an id change AND the *outgoing* item's transition is "fade", the
// preview cross-fades between the old and new media elements over
// transition_ms — matching what the server-side loop does frame-by-
// frame on the device.
//
// Client-side rendering deliberately: the server-side loop still
// drives the physical renderer; this widget is parallel — it watches
// the same state stream and reproduces the slide locally so the
// operator sees what their playlist *is*, not what today's mock
// happens to draw.

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
 * @param {() => Promise<object>} options.fetchState — returns the full
 *     /api/playback/state payload (includes transition + transition_ms).
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

    // Track the last-rendered item so we can diff against the next poll
    // and only swap elements when the item actually changes (avoids
    // re-loading the <video> every 500ms, which would reset playback).
    // Also track the PREVIOUS transition so a cut↔fade change between
    // polls animates correctly — the transition on the outgoing item
    // is what governs how it leaves the stage.
    let currentId = null;
    let currentType = null;
    let lastTransition = null;
    let lastTransitionMs = null;
    let pollTimer = null;
    let stopped = false;

    function renderIdle(message) {
        stage.innerHTML = `<div class="live-preview-idle">${escapeHtml(message)}</div>`;
        currentId = null;
        currentType = null;
    }

    function buildMediaElement(id, type, pipeline) {
        const assetUrl = `/api/content/${id}/asset`;
        const videoUrl = `/api/content/${id}/video`;
        // Raw-frames video has no browser-playable stream; show the
        // thumbnail until a client-side RGB animator lands.
        const isHtmlVideo = type === "video" && pipeline !== "raw_frames";
        let el;
        if (isHtmlVideo) {
            el = document.createElement("video");
            el.autoplay = true;
            el.muted = true;
            el.loop = true;
            el.playsInline = true;
            el.setAttribute("aria-label", "live video preview");
            el.src = videoUrl;
        } else {
            el = document.createElement("img");
            el.alt = "live slide preview";
            el.src = assetUrl;
        }
        el.className = "live-preview-media";
        return el;
    }

    function renderSlide(id, type, pipeline, transition, transitionMs) {
        const next = buildMediaElement(id, type, pipeline);
        const outgoing = stage.querySelector(".live-preview-media");
        const doFade =
            outgoing !== null
            && transition === "fade"
            && Number.isFinite(transitionMs)
            && transitionMs > 0;

        if (doFade) {
            // Layer the new element on top of the old; animate opacity
            // on both via a transition that matches transitionMs. When
            // the transition completes the outgoing node is removed.
            stage.classList.add("live-preview-stage--transitioning");
            next.classList.add("live-preview-media--entering");
            next.style.transition = `opacity ${transitionMs}ms linear`;
            next.style.opacity = "0";
            outgoing.classList.add("live-preview-media--leaving");
            outgoing.style.transition = `opacity ${transitionMs}ms linear`;
            stage.appendChild(next);
            // Next frame: flip opacities so the CSS transition actually
            // runs (applying opacity=0 in the same frame as insertion
            // is a no-op in most engines).
            requestAnimationFrame(() => {
                next.style.opacity = "1";
                outgoing.style.opacity = "0";
            });
            const finish = () => {
                outgoing.remove();
                next.style.transition = "";
                next.style.opacity = "";
                stage.classList.remove("live-preview-stage--transitioning");
            };
            // Safety net: setTimeout after transitionMs + slack in case
            // transitionend doesn't fire (e.g. display:none hijinks).
            const timer = setTimeout(finish, transitionMs + 150);
            outgoing.addEventListener(
                "transitionend",
                () => {
                    clearTimeout(timer);
                    finish();
                },
                { once: true },
            );
        } else {
            // Cut: instant swap. Clear the stage first so only the new
            // element is there on next paint.
            stage.innerHTML = "";
            stage.appendChild(next);
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
        const pipeline = state.current_item_pipeline || null;
        const transition = state.current_item_transition || null;
        const transitionMs = state.current_item_transition_ms ?? null;

        if (!running) {
            renderIdle("Press Play all to preview the playlist.");
            caption.textContent = "";
            lastTransition = null;
            lastTransitionMs = null;
            return;
        }

        if (!id) {
            renderIdle("Waiting for the first slide…");
            caption.textContent = state.current_playlist_name
                ? `Playing: ${state.current_playlist_name}`
                : "";
            lastTransition = null;
            lastTransitionMs = null;
            return;
        }

        if (id !== currentId || type !== currentType) {
            // The transition governing THIS change is the outgoing
            // item's transition — which was captured in lastTransition
            // on the previous poll.
            renderSlide(id, type, pipeline, lastTransition, lastTransitionMs);
        }
        // Stash the current item's transition for the next id change.
        lastTransition = transition;
        lastTransitionMs = transitionMs;
        caption.textContent = state.current_playlist_name
            ? `Playing: ${state.current_playlist_name}`
            : "";

        // Auto-mode text slides get a client-side ticking overlay on top
        // of the thumbnail PNG so the preview reflects what the device
        // is rendering each tick server-side, without polling a
        // /asset/live endpoint every 500ms.
        const autoMode = state.current_item_auto_mode || null;
        const autoFormat = state.current_item_auto_format || null;
        updateAutoOverlay(autoMode, autoFormat);
    }

    function updateAutoOverlay(mode, format) {
        let overlay = stage.querySelector(".live-preview-auto-text");
        if (!mode) {
            if (overlay) overlay.remove();
            return;
        }
        if (!overlay) {
            overlay = document.createElement("div");
            overlay.className = "live-preview-auto-text";
            stage.appendChild(overlay);
        }
        overlay.textContent = formatAutoText(mode, format, new Date());
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

/**
 * Client-side mirror of openmarquee.auto_render.render_auto_text —
 * formats a JS Date for the live preview's ticking overlay. Uses the
 * browser's local timezone (the backend uses the device's configured
 * IANA tz, so preview and device can drift by a zone — acceptable for
 * a preview, documented in the README if it trips anyone up).
 *
 * Kept as a pure exported function so vitest can lock in the format
 * strings without booting the whole preview widget.
 */
export function formatAutoText(mode, format, now) {
    const fmt = format || defaultFormatFor(mode);
    if (mode === "time") {
        if (fmt === "time_hms") {
            return `${pad2(now.getHours())}:${pad2(now.getMinutes())}:${pad2(now.getSeconds())}`;
        }
        return `${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
    }
    if (mode === "date") {
        if (fmt === "date_iso") {
            return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
        }
        if (fmt === "date_medium") {
            return `${MONTHS_SHORT[now.getMonth()]} ${now.getDate()}`;
        }
        // date_long default
        return `${MONTHS_LONG[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()}`;
    }
    if (mode === "day") {
        if (fmt === "day_short") return DAYS_SHORT[now.getDay()];
        return DAYS_LONG[now.getDay()];
    }
    return "";
}

function pad2(n) {
    return String(n).padStart(2, "0");
}

function defaultFormatFor(mode) {
    if (mode === "time") return "time_hm";
    if (mode === "date") return "date_iso";
    if (mode === "day") return "day_long";
    return null;
}

const MONTHS_LONG = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
];
const MONTHS_SHORT = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];
const DAYS_LONG = [
    "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
];
const DAYS_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
