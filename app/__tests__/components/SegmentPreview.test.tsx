import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';

import { SegmentPreview } from '../../src/components/SegmentPreview';
import type { Segment } from '../../src/types';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});

const seg: Segment = {
  id: 'seg-1',
  recordedAt: '2026-07-04T00:05:00.000Z',
  durationSec: 300,
  s3Key: 'device/seg-1.mp4',
  manuallyFlagged: false,
  userFeedback: null,
};

describe('SegmentPreview', () => {
  it('renders nothing when no segment is selected', async () => {
    const { queryByTestId } = await render(
      <SegmentPreview
        segment={null}
        videoUri={null}
        onSave={jest.fn()}
        onShare={jest.fn()}
        onClose={jest.fn()}
      />,
    );
    expect(queryByTestId('segment-preview')).toBeNull();
  });

  it('calls onSave when the save button is pressed', async () => {
    const onSave = jest.fn();
    const { getByTestId } = await render(
      <SegmentPreview
        segment={seg}
        videoUri={null}
        onSave={onSave}
        onShare={jest.fn()}
        onClose={jest.fn()}
      />,
    );
    await fireEvent.press(getByTestId('save-button'));
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it('calls onShare when the share button is pressed', async () => {
    const onShare = jest.fn();
    const { getByTestId } = await render(
      <SegmentPreview
        segment={seg}
        videoUri={null}
        onSave={jest.fn()}
        onShare={onShare}
        onClose={jest.fn()}
      />,
    );
    await fireEvent.press(getByTestId('share-button'));
    expect(onShare).toHaveBeenCalledTimes(1);
  });

  it('renders the video player when a uri is ready', async () => {
    await render(
      <SegmentPreview
        segment={seg}
        videoUri="https://signed/s1.mp4"
        onSave={jest.fn()}
        onShare={jest.fn()}
        onClose={jest.fn()}
      />,
    );
    expect(screen.getByTestId('video-view')).toBeTruthy();
  });

  it('shows a placeholder while the uri is pending', async () => {
    await render(
      <SegmentPreview
        segment={seg}
        videoUri={null}
        onSave={jest.fn()}
        onShare={jest.fn()}
        onClose={jest.fn()}
      />,
    );
    expect(screen.getByText('Preparing playback...')).toBeTruthy();
  });
});
