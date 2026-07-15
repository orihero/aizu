/**
 * Shared deep-link target for the System health tile on the Reports page.
 * The halt banner links here; the Reports page scrolls to + highlights it.
 * Kept in one place so the banner and page can never drift apart.
 */
export const HEALTH_ANCHOR_ID = 'system-health';

/** react-router location.hash value (leading '#') that triggers the scroll. */
export const HEALTH_ANCHOR_HASH = `#${HEALTH_ANCHOR_ID}`;

/** How long the tile keeps its highlight ring after a deep-link arrival. */
export const HEALTH_HIGHLIGHT_MS = 2200;
