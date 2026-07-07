import { buildTimelineSlots } from '../../src/logic/timeline';
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
});
