import {
  buildTimelineSlots,
  formatMinuteOfDay,
  trimToRecordedHours,
} from '../../src/logic/timeline';
import type { Segment } from '../../src/types';

function segment(overrides: Partial<Segment>): Segment {
  return {
    id: 'seg-1',
    recordedAt: '2026-07-04T00:00:00.000Z',
    durationSec: 300,
    s3Key: 'device/seg-1.mp4',
    manuallyFlagged: false,
    userFeedback: null,
    ...overrides,
  };
}

describe('buildTimelineSlots', () => {
  const dayStart = new Date('2026-07-04T00:00:00.000Z');

  it('returns 288 five-minute slots for a full day', () => {
    const slots = buildTimelineSlots([], dayStart);
    expect(slots).toHaveLength(288);
    expect(slots[0].startMinute).toBe(0);
    expect(slots[287].startMinute).toBe(1435);
  });

  it('places a segment into the slot matching its recorded time', () => {
    const seg = segment({ recordedAt: '2026-07-04T00:07:00.000Z' });
    const slots = buildTimelineSlots([seg], dayStart);
    expect(slots[1].segment).toBe(seg);
    expect(slots[0].segment).toBeNull();
  });

  it('marks manually flagged segments on their slot', () => {
    const seg = segment({ recordedAt: '2026-07-04T00:07:00.000Z', manuallyFlagged: true });
    const slots = buildTimelineSlots([seg], dayStart);
    expect(slots[1].isFlagged).toBe(true);
  });

  it('ignores segments outside the given day', () => {
    const seg = segment({ recordedAt: '2026-07-05T00:07:00.000Z' });
    const slots = buildTimelineSlots([seg], dayStart);
    expect(slots.every((slot) => slot.segment === null)).toBe(true);
  });

  it('keeps the later-listed segment when two segments fall in the same slot', () => {
    const first = segment({ id: 'seg-1', recordedAt: '2026-07-04T00:06:00.000Z' });
    const second = segment({ id: 'seg-2', recordedAt: '2026-07-04T00:08:00.000Z' });
    const slots = buildTimelineSlots([first, second], dayStart);
    expect(slots[1].segment).toBe(second);
  });

  it('places an end-of-day segment when slotMinutes does not divide the day evenly', () => {
    const seg = segment({ recordedAt: '2026-07-04T23:59:00.000Z' });
    const slots = buildTimelineSlots([seg], dayStart, 7);
    expect(slots).toHaveLength(206);
    expect(slots[205].startMinute).toBe(1435);
    expect(slots[205].segment).toBe(seg);
  });
});

describe('trimToRecordedHours', () => {
  const dayStart = new Date('2026-07-04T00:00:00.000Z');

  it('keeps every one-minute segment of a run in its own slot', () => {
    const first = segment({ id: 'a', recordedAt: '2026-07-04T09:05:00.000Z', durationSec: 60 });
    const second = segment({ id: 'b', recordedAt: '2026-07-04T09:06:01.000Z', durationSec: 60 });
    const slots = trimToRecordedHours(buildTimelineSlots([first, second], dayStart, 1), 1);
    expect(slots.filter((s) => s.segment).map((s) => s.segment?.id)).toEqual(['a', 'b']);
  });

  it('draws whole hours from the first recording to the last', () => {
    const early = segment({ id: 'a', recordedAt: '2026-07-04T09:05:00.000Z' });
    const late = segment({ id: 'b', recordedAt: '2026-07-04T10:39:00.000Z' });
    const slots = trimToRecordedHours(buildTimelineSlots([early, late], dayStart, 1), 1);
    expect(slots).toHaveLength(120);
    expect(slots[0].startMinute).toBe(9 * 60);
    expect(slots[119].startMinute).toBe(10 * 60 + 59);
  });

  it('draws nothing on a day with no footage', () => {
    expect(trimToRecordedHours(buildTimelineSlots([], dayStart, 1), 1)).toEqual([]);
  });

  it('formats a minute of the day as HH:MM', () => {
    expect(formatMinuteOfDay(9 * 60 + 5)).toBe('09:05');
  });
});
