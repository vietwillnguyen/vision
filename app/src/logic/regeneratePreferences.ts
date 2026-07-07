export type TargetLengthSec = 30 | 60 | 90 | 120;
export type ReelStyle = 'clean' | 'vintage';
export type MoodWeighting = 'conversation-heavy' | 'action-heavy' | 'balanced';

export interface RegenerateRequest {
  targetLengthSec: TargetLengthSec;
  style: ReelStyle;
  mood: MoodWeighting;
}

const VALID_LENGTHS: TargetLengthSec[] = [30, 60, 90, 120];
const VALID_STYLES: ReelStyle[] = ['clean', 'vintage'];
const VALID_MOODS: MoodWeighting[] = ['conversation-heavy', 'action-heavy', 'balanced'];

export class InvalidRegenerateRequestError extends Error {}

export function validateRegenerateRequest(input: {
  targetLengthSec: number;
  style: string;
  mood: string;
}): RegenerateRequest {
  if (!VALID_LENGTHS.includes(input.targetLengthSec as TargetLengthSec)) {
    throw new InvalidRegenerateRequestError(`invalid target length: ${input.targetLengthSec}`);
  }
  if (!VALID_STYLES.includes(input.style as ReelStyle)) {
    throw new InvalidRegenerateRequestError(`invalid style: ${input.style}`);
  }
  if (!VALID_MOODS.includes(input.mood as MoodWeighting)) {
    throw new InvalidRegenerateRequestError(`invalid mood: ${input.mood}`);
  }
  return {
    targetLengthSec: input.targetLengthSec as TargetLengthSec,
    style: input.style as ReelStyle,
    mood: input.mood as MoodWeighting,
  };
}
