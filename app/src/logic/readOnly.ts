/**
 * Whether the signed-in account is read-only (the public demo).
 *
 * The flag is `app_metadata.read_only`, which only the server's admin API can
 * set, and the database refuses every write from such an account regardless
 * of what the app shows. The app reads the same flag only to explain that,
 * and to hide controls that could never succeed.
 */
export function isReadOnlyUser(
  user: { app_metadata?: Record<string, unknown> } | null | undefined,
): boolean {
  return user?.app_metadata?.read_only === true;
}

/** Midday on a reel's UTC date: a stable "now" for browsing that day. */
export function middayOf(isoDate: string): Date {
  return new Date(`${isoDate}T12:00:00.000Z`);
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "Thu 20 Nov 2026" for a `YYYY-MM-DD` UTC date. */
export function formatDayLabel(isoDate: string): string {
  const d = middayOf(isoDate);
  return `${WEEKDAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}
