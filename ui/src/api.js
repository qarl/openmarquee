// Thin client over the OpenMarquee REST API. Same-origin, no CORS, no auth.

export async function fetchHealth() {
    const response = await fetch("/healthz");
    if (!response.ok) {
        throw new Error(`/healthz returned ${response.status}`);
    }
    return await response.json();
}

/**
 * Upload a text slide. `payload` matches the backend TextSlideUpload schema:
 * name, text, text_color, background_color, png_base64.
 *
 * Returns the full TextSlide object (server-assigned id, created_at, etc.).
 */
export async function saveTextSlide(payload) {
    const response = await fetch("/api/content/text-slides", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Save failed (${response.status}): ${detail}`);
    }
    return await response.json();
}
