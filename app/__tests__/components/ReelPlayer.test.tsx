import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';

import { ReelPlayer } from '../../src/components/ReelPlayer';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});

describe('ReelPlayer', () => {
  it('renders the video view with an accessibility label when a uri is ready', () => {
    render(<ReelPlayer videoUri="https://signed/reel.mp4" title="Today's Reel" onShare={jest.fn()} />);
    expect(screen.getByTestId('video-view')).toBeTruthy();
    expect(screen.getByLabelText('Reel video player')).toBeTruthy();
  });

  it('shows a placeholder while the uri is pending', () => {
    render(<ReelPlayer videoUri={null} title="Today's Reel" onShare={jest.fn()} />);
    expect(screen.getByText('Preparing playback...')).toBeTruthy();
    expect(screen.queryByTestId('video-view')).toBeNull();
  });

  it('invokes onShare from the labeled share button', () => {
    const onShare = jest.fn();
    render(<ReelPlayer videoUri="https://signed/reel.mp4" title="Today's Reel" onShare={onShare} />);
    fireEvent.press(screen.getByLabelText('Share reel'));
    expect(onShare).toHaveBeenCalled();
  });
});
