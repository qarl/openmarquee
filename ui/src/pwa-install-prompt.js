// pwa-install-prompt.js — the "bookmark moment" prompt served on the
// LINGER landing page + on any dashboard entry that qualifies.
//
// Per qa/spec-onboarding-ap-sta-concurrent-2026-06-10.md §"Finding
// the device after onboarding" #2:
//     "the LINGER redirect is the bookmark moment. When the portal
//      hands the browser off to the device's permanent address,
//      that landing page should immediately invite permanence."
//
// This module dispatches on capability:
//
//   - **Chromium/Android (beforeinstallprompt)**: cache the deferred
//     event, show a small dismissible banner with an "Install" button
//     that fires prompt(). If the user accepts, the tile is added; if
//     they dismiss (either the banner or the OS-level dialog), we
//     remember the choice for `DISMISS_TTL_MS` so we don't re-nag on
//     every reload.
//
//   - **iOS Safari (no beforeinstallprompt)**: show an instructional
//     banner naming the Share icon + "Add to Home Screen" (iOS has no
//     programmatic install path; the user has to invoke it via the
//     browser toolbar). Same dismissal + TTL contract.
//
//   - **Already installed** (`display-mode: standalone`): don't show.
//     The user is already inside the installed PWA.
//
// The prompt is self-contained: the caller drops a small template
// container into the page (or leaves it to be created dynamically at
// mount time) + calls `mountPwaInstallPrompt(container)`. No CSS
// framework dependency; styles are scoped via a data attribute.

/** localStorage key for the "user dismissed" flag. */
const DISMISS_KEY = "om.pwa-install-dismissed-at";

/** Milliseconds we honor a dismissal before offering the prompt again.
 *  30 days is long enough to not annoy but short enough to catch users
 *  who dismissed on the AP page and come back to the dashboard later.
 *  Value chosen conservatively (spec doesn't nail a number). */
export const DISMISS_TTL_MS = 30 * 24 * 60 * 60 * 1000;

/** Return true when the app is already running as an installed PWA
 *  (Chrome/Android standalone OR iOS Safari `navigator.standalone`).
 *  In that case there's nothing to prompt for. */
export function isAlreadyInstalled(win = window) {
    // matchMedia is the canonical modern signal. iOS 12- doesn't
    // support this query key; we fall back to navigator.standalone.
    if (typeof win.matchMedia === "function") {
        try {
            if (win.matchMedia("(display-mode: standalone)").matches) {
                return true;
            }
        } catch {
            // Older browsers throw on unknown media features. Ignore
            // and fall through to the iOS check.
        }
    }
    // Safari legacy — non-standard but the only way to detect an
    // installed PWA on iOS before display-mode: standalone landed.
    if (win.navigator && win.navigator.standalone === true) {
        return true;
    }
    return false;
}

/** Return true when the user recently dismissed the prompt (within
 *  DISMISS_TTL_MS). Failure to read localStorage (private mode, quota)
 *  treats as "not dismissed" — showing the prompt is preferable to
 *  falsely suppressing it. */
export function wasRecentlyDismissed(storage = window.localStorage, now = Date.now()) {
    try {
        const raw = storage.getItem(DISMISS_KEY);
        if (!raw) return false;
        const at = Number.parseInt(raw, 10);
        if (!Number.isFinite(at)) return false;
        return now - at < DISMISS_TTL_MS;
    } catch {
        return false;
    }
}

/** Persist the "user dismissed at now" flag. Best-effort — swallowed
 *  storage errors just mean we might re-show on next reload. */
export function markDismissed(storage = window.localStorage, now = Date.now()) {
    try {
        storage.setItem(DISMISS_KEY, String(now));
    } catch {
        /* private mode / quota — best-effort */
    }
}

/** Best-effort iOS detection. iOS Safari has no `beforeinstallprompt`
 *  event, so we key the UI copy on this. userAgent sniffing is fragile
 *  but the "share icon → Add to Home Screen" instruction is iOS-
 *  specific, so we NEED the sniff to know which text to show.
 *
 *  iPadOS 13+ reports as "MacIntel" — we cross-check touch support
 *  so a real desktop Mac doesn't get the iOS copy. */
export function isIosSafari(win = window) {
    const nav = win.navigator || {};
    const ua = nav.userAgent || "";
    const platform = nav.platform || "";
    const iosPlatform = /iPad|iPhone|iPod/.test(platform);
    const iosUa = /iPhone|iPad|iPod/.test(ua);
    const iPadOsMasquerade = platform === "MacIntel" && (nav.maxTouchPoints || 0) > 1;
    const isIos = iosPlatform || iosUa || iPadOsMasquerade;
    if (!isIos) return false;
    // Only Safari on iOS lets you install PWAs (Chrome/Firefox on iOS
    // are WebKit shells but their Share menu doesn't include "Add to
    // Home Screen" — instructing users there would misfire). Sniff for
    // Safari + NOT Chrome/CriOS/FxiOS/EdgiOS.
    const isSafari = /Safari\//.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(ua);
    return isSafari;
}

/**
 * Mount the install prompt UI into `container`.
 *
 * @param {HTMLElement} container — where to render the banner. If the
 *     prompt shouldn't fire (already installed / recently dismissed /
 *     neither iOS nor beforeinstallprompt fires), returns immediately
 *     without mutating the container.
 * @param {object} [options]
 * @param {Window} [options.win=window] — override for tests.
 * @param {Storage} [options.storage=window.localStorage] — override for tests.
 *
 * Returns a small controller with a `.destroy()` method that unmounts
 * the banner + detaches listeners. Callers can use this on route
 * changes; leaking a listener is harmless (single beforeinstallprompt
 * per page load) but clean unmount is idiomatic.
 */
export function mountPwaInstallPrompt(container, options = {}) {
    const win = options.win || window;
    const storage = options.storage || (win && win.localStorage);
    if (!container) {
        return { destroy() {} };
    }
    if (isAlreadyInstalled(win)) {
        return { destroy() {} };
    }
    if (storage && wasRecentlyDismissed(storage)) {
        return { destroy() {} };
    }

    let deferredEvent = null;
    let banner = null;
    let onBeforeInstallPrompt = null;
    let onAppInstalled = null;

    // Helper: render the banner. Dispatches copy + button behavior on
    // whether we have a native prompt in hand (Chromium) or need to
    // instruct manually (iOS).
    const render = (mode) => {
        // mode: 'native' when we have a deferredEvent to fire;
        // 'ios' when we should show manual instructions.
        // Bail if the container has already been detached.
        if (!container.isConnected) return;
        // Clear any prior render.
        if (banner) {
            banner.remove();
            banner = null;
        }
        banner = win.document.createElement("div");
        banner.setAttribute("role", "dialog");
        banner.setAttribute("aria-labelledby", "pwa-install-title");
        banner.setAttribute("data-om-pwa-install", "");
        // Styles inlined so the module works without a stylesheet
        // dependency — the LINGER page might be served ahead of the
        // main dashboard styles.css bundle.
        banner.style.cssText = [
            "position: relative",
            "margin: 12px auto",
            "max-width: 480px",
            "padding: 14px 16px",
            "background: rgba(255, 180, 60, 0.10)",
            "border: 1px solid rgba(255, 180, 60, 0.35)",
            "border-radius: 12px",
            "color: #f2f2f4",
            "font: 500 14px/1.45 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
            "display: flex",
            "gap: 12px",
            "align-items: center",
        ].join("; ");

        const body = win.document.createElement("div");
        body.style.flex = "1 1 auto";
        const title = win.document.createElement("div");
        title.id = "pwa-install-title";
        title.style.cssText = "font-weight: 700; color: #ffb43c; margin-bottom: 2px;";
        title.textContent = "Add openMarquee to your Home Screen";
        body.appendChild(title);

        const detail = win.document.createElement("div");
        detail.style.cssText = "color: #d9d9dd; font-size: 13px;";
        if (mode === "ios") {
            detail.textContent =
                "Tap the Share icon in Safari, then choose “Add to Home Screen” for a one-tap tile.";
        } else {
            detail.textContent = "One tap opens the sign’s dashboard — no bookmark needed.";
        }
        body.appendChild(detail);
        banner.appendChild(body);

        // Actions column: Install button (native only) + Dismiss.
        const actions = win.document.createElement("div");
        actions.style.cssText = "display: flex; flex-direction: column; gap: 6px; flex: 0 0 auto;";

        if (mode === "native") {
            const installBtn = win.document.createElement("button");
            installBtn.type = "button";
            installBtn.setAttribute("data-om-pwa-install-btn", "");
            installBtn.textContent = "Install";
            installBtn.style.cssText = [
                "padding: 8px 14px",
                "background: #ffb43c",
                "color: #1a0f00",
                "border: 0",
                "border-radius: 8px",
                "font: 700 13px -apple-system, BlinkMacSystemFont, sans-serif",
                "cursor: pointer",
            ].join("; ");
            installBtn.addEventListener("click", async () => {
                if (!deferredEvent) return;
                try {
                    deferredEvent.prompt();
                    // .userChoice resolves regardless of accept/dismiss;
                    // treat dismiss as an implicit UI dismissal so the
                    // banner doesn't re-appear next reload.
                    const choice = await deferredEvent.userChoice;
                    if (choice && choice.outcome === "dismissed") {
                        markDismissed(storage);
                    }
                } catch {
                    // prompt() throws if called twice; ignore quietly.
                }
                deferredEvent = null;
                // Whether accepted or dismissed, drop the banner.
                if (banner) {
                    banner.remove();
                    banner = null;
                }
            });
            actions.appendChild(installBtn);
        }

        const dismissBtn = win.document.createElement("button");
        dismissBtn.type = "button";
        dismissBtn.setAttribute("data-om-pwa-install-dismiss", "");
        dismissBtn.setAttribute("aria-label", "Dismiss install prompt");
        dismissBtn.textContent = "Not now";
        dismissBtn.style.cssText = [
            "padding: 6px 10px",
            "background: transparent",
            "color: #9c9ca3",
            "border: 1px solid rgba(156, 156, 163, 0.4)",
            "border-radius: 8px",
            "font: 500 12px -apple-system, BlinkMacSystemFont, sans-serif",
            "cursor: pointer",
        ].join("; ");
        dismissBtn.addEventListener("click", () => {
            markDismissed(storage);
            if (banner) {
                banner.remove();
                banner = null;
            }
        });
        actions.appendChild(dismissBtn);
        banner.appendChild(actions);

        container.appendChild(banner);
    };

    // --- Chromium/Android path: wait for beforeinstallprompt --------
    if (typeof win.addEventListener === "function") {
        onBeforeInstallPrompt = (e) => {
            // Preventing the default lets us fire prompt() ourselves
            // via the Install button rather than the browser
            // deciding when.
            if (typeof e.preventDefault === "function") {
                e.preventDefault();
            }
            deferredEvent = e;
            render("native");
        };
        onAppInstalled = () => {
            // The user installed via any surface (our button or the
            // browser's own menu). Tear the banner down.
            if (banner) {
                banner.remove();
                banner = null;
            }
            deferredEvent = null;
        };
        win.addEventListener("beforeinstallprompt", onBeforeInstallPrompt);
        win.addEventListener("appinstalled", onAppInstalled);
    }

    // --- iOS Safari path: show the instructional banner immediately -
    if (isIosSafari(win)) {
        render("ios");
    }

    return {
        destroy() {
            if (win && typeof win.removeEventListener === "function") {
                if (onBeforeInstallPrompt) {
                    win.removeEventListener("beforeinstallprompt", onBeforeInstallPrompt);
                }
                if (onAppInstalled) {
                    win.removeEventListener("appinstalled", onAppInstalled);
                }
            }
            if (banner) {
                banner.remove();
                banner = null;
            }
            deferredEvent = null;
        },
    };
}
