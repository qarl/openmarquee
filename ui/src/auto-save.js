// Shared auto-save helper.
//
// Each editable surface (text editor, image upload, video upload, playlist
// track) used to have an explicit "Save X" button. The redesign drops
// those in favor of debounced auto-save while showing a small status pill
// where the button used to live.
//
// `attachAutoSave(form, { save })` wires `input` + `change` listeners to
// the form so any field mutation triggers a debounced `save()`. Call
// `handle.flush()` to fire any pending save immediately (e.g. on tab
// blur), `handle.cancel()` to drop a pending one (e.g. when navigating
// away from an unsaved-but-incomplete form), and `handle.kick()` to
// trigger a save outside the form-event path (drag-end, file pick, etc.).
//
// Status indicator: pass an `HTMLElement` `status` and the helper paints
// it through 3 states — saving · saved · error — with a fade-out timer
// so the operator sees green long enough to register but the chrome
// settles back to neutral.

const SAVED_STICKY_MS = 2400;
const DEFAULT_DEBOUNCE_MS = 600;

/**
 * @param {HTMLElement} form — form / container whose input + change events
 *   trigger an auto-save attempt. Pass null to skip auto-wiring (then use
 *   `handle.kick()` from your own event handlers).
 * @param {object} options
 * @param {() => Promise<any>} options.save — called when the debounce fires.
 *   Should be idempotent (auto-save may fire many times for the same change
 *   set) and resolve once the server has confirmed the write.
 * @param {HTMLElement} [options.status] — element painted with state copy.
 * @param {number} [options.debounceMs=600] — quiet-time before a save fires.
 * @param {() => boolean} [options.canSave] — gate; auto-save is suppressed
 *   when this returns false. Useful for "no file picked yet" / "draft is
 *   empty" cases where there's nothing meaningful to persist.
 * @returns {{ kick: () => void, flush: () => Promise<void>, cancel: () => void }}
 */
export function attachAutoSave(form, { save, status, debounceMs, canSave }) {
    const wait = Number.isFinite(debounceMs) ? debounceMs : DEFAULT_DEBOUNCE_MS;
    let timer = null;
    let pending = false;
    let inFlight = null;
    let stickyTimer = null;

    const setStatus = (state, text) => {
        if (!status) return;
        if (stickyTimer) {
            clearTimeout(stickyTimer);
            stickyTimer = null;
        }
        status.dataset.state = state;
        status.textContent = text;
        // Drop the "Saved" copy after a beat so the chrome doesn't shout.
        if (state === "saved") {
            stickyTimer = setTimeout(() => {
                if (status.dataset.state === "saved") {
                    status.textContent = "";
                    status.dataset.state = "idle";
                }
                stickyTimer = null;
            }, SAVED_STICKY_MS);
        }
    };

    async function attempt() {
        timer = null;
        if (canSave && !canSave()) {
            // Nothing meaningful yet; quietly suppress so the operator
            // doesn't see a "saved" toast when nothing was saved.
            pending = false;
            return;
        }
        if (inFlight) {
            // Coalesce: a save is already running. Mark pending so we
            // re-enqueue once it finishes.
            pending = true;
            return;
        }
        pending = false;
        setStatus("saving", "Saving…");
        inFlight = (async () => {
            try {
                await save();
                setStatus("saved", "Saved");
            } catch (err) {
                setStatus("error", `Couldn't save · ${err?.message || err}`);
            } finally {
                inFlight = null;
                if (pending) {
                    pending = false;
                    schedule();
                }
            }
        })();
    }

    function schedule() {
        if (timer) clearTimeout(timer);
        timer = setTimeout(attempt, wait);
    }

    function kick() {
        schedule();
    }

    async function flush() {
        // Drain everything: pending timer, in-flight save, and any
        // follow-up queued by `pending=true` while we waited. Looping
        // matters when a save is in-flight at flush() time — the
        // finally-block schedules a follow-up that single-await wouldn't
        // catch.
        // eslint-disable-next-line no-constant-condition
        while (true) {
            if (timer) {
                clearTimeout(timer);
                timer = null;
            }
            if (!inFlight && !pending) {
                await attempt();
            }
            if (inFlight) await inFlight;
            if (!timer && !inFlight && !pending) return;
        }
    }

    function cancel() {
        if (timer) clearTimeout(timer);
        timer = null;
        pending = false;
    }

    if (form) {
        form.addEventListener("input", schedule);
        form.addEventListener("change", schedule);
    }

    return { kick, flush, cancel };
}
