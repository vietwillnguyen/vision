import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});

import { ArchiveContainer } from '../../src/containers/ArchiveContainer';

const REEL_ROW = {
  id: 'r1',
  date: '2026-07-10',
  s3_key: 'dev-1/2026-07-10.mp4',
  duration_sec: 60,
  style: 'clean',
};

function fakeClient() {
  const chain = {
    select: () => chain,
    eq: () => chain,
    gte: () => chain,
    lte: () => chain,
    order: () => chain,
    limit: () => Promise.resolve({ data: [REEL_ROW], error: null }),
    then: (onFulfilled: (v: { data: unknown; error: null }) => unknown) =>
      Promise.resolve({ data: [REEL_ROW], error: null }).then(onFulfilled),
  };
  return {
    from: () => chain,
    storage: {
      from: () => ({
        createSignedUrl: () =>
          Promise.resolve({ data: { signedUrl: 'https://signed/r1.mp4' }, error: null }),
      }),
    },
  } as unknown as SupabaseClient;
}

describe('ArchiveContainer', () => {
  it('renders 30 UTC-midnight-aligned cells ending today', async () => {
    render(
      <ArchiveContainer client={fakeClient()} deviceId="dev-1" now={() => new Date('2026-07-18T23:30:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('archive-heatmap')).toBeTruthy());
    expect(screen.getByTestId('heatmap-cell-2026-06-19')).toBeTruthy();
    expect(screen.getByTestId('heatmap-cell-2026-07-18')).toBeTruthy();
    expect(screen.queryByTestId('heatmap-cell-2026-06-18')).toBeNull();
    expect(screen.queryByTestId('heatmap-cell-2026-07-19')).toBeNull();
  });

  it('plays the reel for a pressed day', async () => {
    render(
      <ArchiveContainer client={fakeClient()} deviceId="dev-1" now={() => new Date('2026-07-18T12:00:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('heatmap-cell-2026-07-10')).toBeTruthy());
    fireEvent.press(screen.getByTestId('heatmap-cell-2026-07-10'));
    await waitFor(() => expect(screen.getByTestId('video-view')).toBeTruthy());
    fireEvent.press(screen.getByLabelText('Back to archive'));
    expect(screen.getByTestId('archive-heatmap')).toBeTruthy();
  });
});
