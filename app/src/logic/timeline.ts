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
  const slotCount = Math.ceil(MINUTES_PER_DAY / slotMinutes);
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

/**
 * The slots worth drawing: whole hours from the first recorded slot's hour to
 * the end of the last one's, or none on a day with no footage.
 *
 * A day of moments is a few clips across a few hours, so drawing all 24 hours
 * buries them in empty slots and opens the strip at midnight.
 */
export function trimToRecordedHours(slots: TimelineSlot[], slotMinutes: number): TimelineSlot[] {
  const occupied = slots.filter((slot) => slot.segment !== null);
  if (occupied.length === 0) {
    return [];
  }
  const firstHourStart = Math.floor(occupied[0].startMinute / 60) * 60;
  const lastHourEnd = (Math.floor(occupied[occupied.length - 1].startMinute / 60) + 1) * 60;
  return slots.filter(
    (slot) => slot.startMinute >= firstHourStart && slot.startMinute + slotMinutes <= lastHourEnd,
  );
}

export function formatMinuteOfDay(startMinute: number): string {
  const h = String(Math.floor(startMinute / 60)).padStart(2, '0');
  const m = String(startMinute % 60).padStart(2, '0');
  return `${h}:${m}`;
}
