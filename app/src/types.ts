/**
 * Hand-written camelCase domain types.
 *
 * These are deliberately *not* the generated snake_case row shapes in
 * `generated/database.ts`: rows are mapped into these at the data-access edge (the
 * hooks' `mapReelRow()` and friends) so components and hook signatures never
 * see a column name. The generated types type the query; these type the app.
 */

/**
 * Reel styles the app knows how to render.
 *
 * `reels.style` is a bare `text` column with no check constraint, so the
 * database can hold anything at all here and the generated row type is
 * `string`. {@link parseReelStyle} is the boundary that reconciles the two.
 */
export const REEL_STYLES = ['clean', 'vintage'] as const;
export type ReelStyle = (typeof REEL_STYLES)[number];

/** Feedback values `segments.user_feedback` accepts, per its check constraint. */
export const USER_FEEDBACK_VALUES = ['include', 'exclude'] as const;
export type UserFeedback = (typeof USER_FEEDBACK_VALUES)[number];

export interface Segment {
  id: string;
  recordedAt: string;
  durationSec: number;
  s3Key: string;
  manuallyFlagged: boolean;
  userFeedback: UserFeedback | null;
}

export interface Reel {
  id: string;
  date: string;
  s3Key: string;
  durationSec: number;
  style: ReelStyle;
}

export interface DeviceStatus {
  batteryPct: number;
  storageUsedGb: number;
  storageFreeGb: number;
  segmentsPending: number;
  segmentsUploadedToday: number;
  recordingActive: boolean;
}

/**
 * Narrow a stored `reels.style` to a style the app can render.
 *
 * Nothing in the schema stops a writer from storing another string, so an
 * unrecognised one falls back to the column's own default rather than
 * propagating a value no renderer handles.
 */
export function parseReelStyle(value: string): ReelStyle {
  if (isReelStyle(value)) {
    return value;
  }
  console.warn(`unknown reel style ${JSON.stringify(value)}; falling back to 'clean'`);
  return 'clean';
}

function isReelStyle(value: string): value is ReelStyle {
  return REEL_STYLES.some((style) => style === value);
}

/**
 * Narrow a stored `segments.user_feedback` to the app's union.
 *
 * The check constraint already restricts this column, so an unrecognised value
 * means the constraint and this union have diverged - treat it as no feedback.
 */
export function parseUserFeedback(value: string | null): UserFeedback | null {
  if (value === null) {
    return null;
  }
  if (isUserFeedback(value)) {
    return value;
  }
  console.warn(`unknown segment user_feedback ${JSON.stringify(value)}; treating as none`);
  return null;
}

function isUserFeedback(value: string): value is UserFeedback {
  return USER_FEEDBACK_VALUES.some((feedback) => feedback === value);
}
