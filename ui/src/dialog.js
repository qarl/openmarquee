// Confirm + alert helpers. Currently 1-line passthroughs over
// `window.confirm` / `window.alert`; the abstraction boundary
// exists so a future "replace with a real in-DOM modal" lands as
// one helper-body change instead of N call-site edits.
//
// Surfaced via the main.js survey 680cd55 target #4: native
// dialogs block the JS thread + look different from the rest of
// the captive-portal UI, and they're hard to test reliably under
// jsdom. Today they're still the right shape for destructive-
// action confirms; the helper just locks the abstraction so the
// migration is cheap when the product call comes.

/**
 * Prompt the operator to confirm a destructive action. Returns
 * true if confirmed, false if cancelled.
 *
 * @param {string} message
 * @returns {boolean}
 */
export function confirmAction(message) {
    return window.confirm(message);
}

/**
 * Surface a one-shot operator-visible alert. No return value;
 * caller is responsible for any follow-up branching.
 *
 * @param {string} message
 */
export function alertOperator(message) {
    window.alert(message);
}
