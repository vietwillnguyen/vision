import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';
import { StyleSheet } from 'react-native';

import { ReelPlayer } from '../../src/components/ReelPlayer';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});

describe('ReelPlayer', () => {
  it('renders the video view with an accessibility label when a uri is ready', async () => {
    await render(
      <ReelPlayer videoUri="https://signed/reel.mp4" title="Today's Reel" onShare={jest.fn()} />,
    );
    expect(screen.getByTestId('video-view')).toBeTruthy();
    expect(screen.getByLabelText('Reel video player')).toBeTruthy();
  });

  it('shows a placeholder while the uri is pending', async () => {
    await render(<ReelPlayer videoUri={null} title="Today's Reel" onShare={jest.fn()} />);
    expect(screen.getByText('Preparing playback...')).toBeTruthy();
    expect(screen.queryByTestId('video-view')).toBeNull();
  });

  it('invokes onShare from the labeled share button', async () => {
    const onShare = jest.fn();
    await render(
      <ReelPlayer videoUri="https://signed/reel.mp4" title="Today's Reel" onShare={onShare} />,
    );
    await fireEvent.press(screen.getByLabelText('Share reel'));
    expect(onShare).toHaveBeenCalled();
  });
});

describe('ReelPlayer states', () => {
  it('says why when the video cannot be loaded instead of preparing forever', async () => {
    await render(<ReelPlayer videoUri={null} videoError="Object not found" title="Reel" />);
    expect(screen.getByLabelText('Reel video error')).toHaveTextContent('Object not found');
    expect(screen.queryByText('Preparing playback...')).toBeNull();
  });

  it('hides Share where the platform cannot share', async () => {
    await render(<ReelPlayer videoUri="https://signed/reel.mp4" title="Reel" />);
    expect(screen.queryByLabelText('Share reel')).toBeNull();
  });

  it('frames the landscape reel at 16:9 and shows a subtitle', async () => {
    await render(
      <ReelPlayer videoUri="https://signed/reel.mp4" title="Reel" subtitle="Fri 20 Nov 2026" />,
    );
    expect(screen.getByText('Fri 20 Nov 2026')).toBeTruthy();
    const style = StyleSheet.flatten(screen.getByTestId('video-view').props.style);
    expect(style.aspectRatio).toBeCloseTo(16 / 9);
  });
});
