import type { Segment } from '../types';

const MINUTES_PER_DAY = 24 * 60;

export interface TimelineSlot {
  startMinute: number;
  segment: Segment | null;
  isFlagged: boolean;
}

export function buildTimelineSlots(
  segments: Segment[],
  dayStart: Date,
  slotMinutes: number = 5,
): TimelineSlot[] {
  const slotCount = MINUTES_PER_DAY / slotMinutes;
  const slots: TimelineSlot[] = Array.from({ length: slotCount }, (_, i) => ({
    startMinute: i * slotMinutes,
    segment: null,
    isFlagged: false,
  }));

  for (const seg of segments) {
    const recordedAt = new Date(seg.recordedAt);
    const minutesSinceStart = Math.floor((recordedAt.getTime() - dayStart.getTime()) / 60000);
    if (minutesSinceStart < 0 || minutesSinceStart >= MINUTES_PER_DAY) {
      continue;
    }
    const slotIndex = Math.floor(minutesSinceStart / slotMinutes);
    slots[slotIndex] = {
      startMinute: slots[slotIndex].startMinute,
      segment: seg,
      isFlagged: seg.manuallyFlagged,
    };
  }

  return slots;
}
