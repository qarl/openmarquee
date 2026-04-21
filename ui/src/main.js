// Entry point for the OpenMarquee web UI.
// Phase 0: placeholder. Replaced with the captive-portal UI in Phase 3.

export function greeting(name) {
    return `Hello, ${name}. Welcome to OpenMarquee.`;
}

if (typeof window !== "undefined") {
    console.log(greeting("there"));
}
