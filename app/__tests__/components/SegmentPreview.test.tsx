import { fireEvent, render } from '@testing-library/react-native';
import React from 'react';

import { SegmentPreview } from '../../src/components/SegmentPreview';
import type { Segment } from '../../src/types';

const seg: Segment = {
  id: 'seg-1',
  recordedAt: '2026-07-04T00:05:00.000Z',
  durationSec: 300,
  s3Key: 'device/seg-1.mp4',
  manuallyFlagged: false,
  userFeedback: null,
};

describe('SegmentPreview', () => {
  it('renders nothing when no segment is selected', () => {
    const { queryByTestId } = render(
      <SegmentPreview segment={null} onSave={jest.fn()} onShare={jest.fn()} onClose={jest.fn()} />,
    );
    expect(queryByTestId('segment-preview')).toBeNull();
  });

  it('calls onSave when the save button is pressed', () => {
    const onSave = jest.fn();
    const { getByTestId } = render(
      <SegmentPreview segment={seg} onSave={onSave} onShare={jest.fn()} onClose={jest.fn()} />,
    );
    fireEvent.press(getByTestId('save-button'));
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it('calls onShare when the share button is pressed', () => {
    const onShare = jest.fn();
    const { getByTestId } = render(
      <SegmentPreview segment={seg} onSave={jest.fn()} onShare={onShare} onClose={jest.fn()} />,
    );
    fireEvent.press(getByTestId('share-button'));
    expect(onShare).toHaveBeenCalledTimes(1);
  });
});
