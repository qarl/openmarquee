// First-run welcome screen — shown on a freshly-flashed device the
// FIRST time the operator opens the captive portal in their browser.
// Dismiss writes settings.ui_first_run_seen=true so subsequent visits
// go straight to the editor.
//
// Note: this is a SEPARATE flow from `welcome.html`, which is what
// the SIGN ITSELF displays at HDMI output before any content is
// uploaded. That one is for "the screen on the wall"; this one is
// for "the operator's phone".
//
// Layout follows the Claude Design "classic" welcome variant: twin
// scrolling marquee strips top + bottom, glowing LED-card hero in the
// middle showing a teal "Hello, friend." preview, and a Make-it-mine
// CTA. CSS classes (.om-welcome, .om-marquee-strip, .om-welcome-card)
// are defined in styles.css's new design-system block.

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
        c === "&" ? "&amp;" :
        c === "<" ? "&lt;" :
        c === ">" ? "&gt;" :
        c === "\"" ? "&quot;" : "&#39;"
    ));
}

const TEMPLATE = (signName) => `
    <div class="om-welcome">
        <div class="om-marquee-strip top" aria-hidden="true">
            <div>${"<span>FREE YOUR SIGN <i>·</i></span>".repeat(8)}</div>
            <div>${"<span>FREE YOUR SIGN <i>·</i></span>".repeat(8)}</div>
        </div>
        <div class="om-welcome-hero">
            <div class="om-welcome-card">
                <div style="aspect-ratio: 2/1; background: radial-gradient(120% 90% at 30% 20%, #0d4a52 0%, #06222a 70%); border-radius: 4px; display: flex; align-items: center; justify-content: center; padding: 4%; box-sizing: border-box; container-type: size;">
                    <div style="color: #f4ecd8; font-weight: 700; font-family: Impact, Arial Black, var(--om-display); white-space: pre-line; font-size: 36cqh; line-height: 1.05; text-align: center;">Hello,
friend.</div>
                </div>
                <div style="margin-top: 10px; display: flex; justify-content: space-between; font-family: var(--om-mono); font-size: 10px; color: var(--om-text-fade); letter-spacing: 0.06em;">
                    <span>SLIDE 01 / 01</span>
                    <span style="color: var(--om-accent);">● LIVE</span>
                </div>
            </div>
        </div>
        <div style="padding: 0 22px 24px; text-align: center; position: relative; z-index: 2;">
            <div style="font-size: 26px; font-weight: 700; margin-bottom: 6px; letter-spacing: -0.02em;">
                Your sign is on.
            </div>
            <div style="color: var(--om-text-dim); font-size: 14px; line-height: 1.5; margin-bottom: 18px; max-width: 360px; margin-inline: auto;">
                That's it on the wall — it's playing the welcome slide right now.
                Make it yours: pick a background, write a message, send a video.
            </div>
            <button type="button" class="om-btn primary first-run-continue"
                    style="width: 100%; max-width: 320px; height: 48px; font-size: 14.5px;">
                Make it mine
            </button>
            <div style="margin-top: 14px; font-family: var(--om-mono); font-size: 10.5px; color: var(--om-text-fade); letter-spacing: 0.05em;">
                connected to <b style="color: var(--om-text-dim);">${escapeHtml(signName)}</b>
            </div>
        </div>
        <div class="om-marquee-strip bot" aria-hidden="true">
            <div>${"<span><i>·</i> NO ACCOUNT · NO CLOUD · NO SUBSCRIPTION </span>".repeat(8)}</div>
            <div>${"<span><i>·</i> NO ACCOUNT · NO CLOUD · NO SUBSCRIPTION </span>".repeat(8)}</div>
        </div>
    </div>
`;

/**
 * Mount the first-run welcome screen.
 *
 * @param {HTMLElement} container — the host element (cleared + replaced).
 * @param {object} options
 * @param {string} options.signName — for the "connected to <name>" footer.
 * @param {() => Promise<void>} options.onContinue — called when "Make it
 *     mine" is tapped. Should persist `ui_first_run_seen=true` and
 *     reload the UI.
 */
export function mountFirstRunWelcome(container, { signName, onContinue }) {
    container.innerHTML = TEMPLATE(signName || "this device");
    const btn = container.querySelector(".first-run-continue");
    btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
            await onContinue();
        } catch (err) {
            // Soft fail — let the operator try again rather than getting
            // stuck on the welcome with no error feedback.
            btn.disabled = false;
            console.error("[first-run] continue failed:", err);
        }
    });
}
