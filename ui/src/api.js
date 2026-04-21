// Thin client over the OpenMarquee REST API. Same-origin, no CORS, no auth.

export async function fetchHealth() {
    const response = await fetch("/healthz");
    if (!response.ok) {
        throw new Error(`/healthz returned ${response.status}`);
    }
    return await response.json();
}
