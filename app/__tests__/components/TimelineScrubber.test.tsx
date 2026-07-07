import { fireEvent, render } from '@testing-library/react-native';
import React from 'react';

import { TimelineScrubber } from '../../src/components/TimelineScrubber';
import type { TimelineSlot } from '../../src/logic/timeline';
import type { Segment } from '../../src/types';

const seg: Segment = {
  id: 'seg-1',
  recordedAt: '2026-07-04T00:05:00.000Z',
  durationSec: 300,
  s3Key: 'device/seg-1.mp4',
  manuallyFlagged: true,
  userFeedback: null,
};

const slots: TimelineSlot[] = [
  { startMinute: 0, segment: null, isFlagged: false },
  { startMinute: 5, segment: seg, isFlagged: true },
];

describe('TimelineScrubber', () => {
  it('renders a flag marker only for flagged slots', () => {
    const { getByTestId, queryByTestId } = render(
      <TimelineScrubber slots={slots} onSlotLongPress={jest.fn()} />,
    );

    expect(getByTestId('flag-5')).toBeTruthy();
    expect(queryByTestId('flag-0')).toBeNull();
  });

  it('calls onSlotLongPress with the slot on long press', () => {
    const onSlotLongPress = jest.fn();
    const { getByTestId } = render(
      <TimelineScrubber slots={slots} onSlotLongPress={onSlotLongPress} />,
    );

    fireEvent(getByTestId('slot-5'), 'longPress');

    expect(onSlotLongPress).toHaveBeenCalledWith(slots[1]);
  });
});
