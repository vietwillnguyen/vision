type DocumentLike = { documentElement: { style: { colorScheme: string } } };

/**
 * Tell the browser the page is dark.
 *
 * The app only has a dark theme, but browser-drawn chrome (scrollbars, focus
 * rings, autofill) follows the document's color scheme, which defaults to
 * light - so the timeline strip's horizontal scrollbar rendered as a white bar
 * on the dark Raw Footage screen. Native has no document, so this is a no-op
 * there.
 */
export function applyWebColorScheme(doc: DocumentLike | undefined): void {
  if (!doc) return;
  doc.documentElement.style.colorScheme = 'dark';
}
