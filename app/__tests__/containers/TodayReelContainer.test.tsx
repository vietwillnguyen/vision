import { render, screen, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

import { TodayReelContainer } from '../../src/containers/TodayReelContainer';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});

const REEL_ROW = {
  id: 'r1',
  date: '2026-07-18',
  s3_key: 'dev-1/2026-07-18.mp4',
  duration_sec: 60,
  style: 'clean',
};

function fakeClient(rows: Record<string, unknown>[]) {
  const chain = {
    select: () => chain,
    eq: () => chain,
    order: () => chain,
    limit: () => Promise.resolve({ data: rows, error: null }),
  };
  return {
    from: () => chain,
    storage: {
      from: () => ({
        createSignedUrl: () => Promise.resolve({ data: { signedUrl: 'https://signed/reel.mp4' }, error: null }),
      }),
    },
  } as unknown as SupabaseClient;
}

describe('TodayReelContainer', () => {
  it("plays today's reel through a signed url", async () => {
    render(
      <TodayReelContainer
        client={fakeClient([REEL_ROW])}
        deviceId="dev-1"
        now={() => new Date('2026-07-18T15:00:00Z')}
      />,
    );
    await waitFor(() => expect(screen.getByTestId('video-view')).toBeTruthy());
  });

  it('explains when no reel exists yet', async () => {
    render(
      <TodayReelContainer
        client={fakeClient([])}
        deviceId="dev-1"
        now={() => new Date('2026-07-18T15:00:00Z')}
      />,
    );
    await waitFor(() => expect(screen.getByText("Today's reel isn't ready yet.")).toBeTruthy());
  });

  it('renders placeholder when reel has empty s3_key', async () => {
    const rowWithEmptyKey = { ...REEL_ROW, s3_key: '' };
    render(
      <TodayReelContainer
        client={fakeClient([rowWithEmptyKey])}
        deviceId="dev-1"
        now={() => new Date('2026-07-18T15:00:00Z')}
      />,
    );
    await waitFor(() => expect(screen.getByText('Preparing playback...')).toBeTruthy());
    expect(screen.queryByTestId('video-view')).toBeNull();
  });
});
