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
// Status indicator: pass an `HTMLElement` `status`. Saving is implicit
// (FYS bug 6) — the helper shows NO "Saving…" / "Saved" copy; it only
// renders an error message when a save fails. The `data-state`
// attribute still cycles (saving · saved · error · idle) for CSS
// error-colouring and for e2e save-completion sync.

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
 * @param {() => string | null} [options.validate] — client-side validity
 *   check; return null when the form is OK to send, or an operator-facing
 *   error message when it isn't. On a non-null return the helper paints
 *   the message into `status` (state="error") and skips the network call,
 *   so a known-invalid form never round-trips through the server. Distinct
 *   from `canSave` (silent suppression for "nothing to do").
 * @returns {{ kick: () => void, flush: () => Promise<void>, cancel: () => void }}
 *   `flush()` drains pending + in-flight saves and REJECTS with the
 *   most recent save error if any drained attempt failed (r20). It
 *   consumes the error on each call, so a second flush after a
 *   successful retry won't re-throw a stale failure.
 */
export function attachAutoSave(form, { save, status, debounceMs, canSave, validate }) {
    const wait = Number.isFinite(debounceMs) ? debounceMs : DEFAULT_DEBOUNCE_MS;
    let timer = null;
    let pending = false;
    let inFlight = null;
    // Last error from the most recent save() attempt. Cleared on a
    // subsequent successful save. flush() surfaces this on its
    // resolved-or-rejected boundary so callers awaiting flush get a
    // truthful "did the work persist" signal (pre-r20 the IIFE
    // swallowed errors and flush always resolved — operator scenario:
    // beforeunload awaits flush, PUT 500s, navigation proceeds,
    // edit lost). flush() consumes the value on each call so a
    // follow-up flush doesn't double-surface a stale error.
    let lastError = null;

    // FYS bug 6 — saving is implicit: no "Saving…" / "Saved"
    // confirmation copy on any panel (qarl: no save-confirmation
    // messages anywhere). Only an error keeps visible text; the
    // data-state attribute still cycles so CSS can colour an error
    // and e2e can sync on save completion.
    const setStatus = (state, text) => {
        if (!status) return;
        status.dataset.state = state;
        status.textContent = state === "error" ? (text || "") : "";
    };

    async function attempt() {
        timer = null;
        if (canSave && !canSave()) {
            // Nothing meaningful yet; quietly suppress so the operator
            // doesn't see a "saved" toast when nothing was saved.
            pending = false;
            return;
        }
        if (validate) {
            const message = validate();
            if (message) {
                // Known-invalid form: surface the error message to the
                // operator and bail before the network call. Don't fire
                // a doomed PUT just to have the server bounce it back.
                pending = false;
                setStatus("error", message);
                return;
            }
        }
        if (inFlight) {
            // Coalesce: a save is already running. Mark pending so we
            // re-enqueue once it finishes.
            pending = true;
            return;
        }
        pending = false;
        setStatus("saving");
        inFlight = (async () => {
            try {
                await save();
                setStatus("saved");
                // Success consumes any prior error so the next flush
                // doesn't surface a stale failure from a previous
                // attempt that was retried (and succeeded).
                lastError = null;
            } catch (err) {
                setStatus("error", `Couldn't save · ${err?.message || err}`);
                lastError = err;
                // Don't rethrow here — preserves the per-debounce-tick
                // observer contract that pre-r20 callers (just the
                // status pill) relied on. flush() surfaces the error
                // when it's the right boundary to do so.
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
            if (!timer && !inFlight && !pending) break;
        }
        // Surface any save error from the drained attempts so callers
        // awaiting flush() get a truthful "did the work persist?"
        // signal. Consume the value so a follow-up flush after a
        // successful retry doesn't double-throw a stale error.
        if (lastError) {
            const err = lastError;
            lastError = null;
            throw err;
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
