import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

import { ArchiveContainer } from '../../src/containers/ArchiveContainer';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});

const REEL_ROW = {
  id: 'r1',
  date: '2026-07-10',
  s3_key: 'dev-1/2026-07-10.mp4',
  duration_sec: 60,
  style: 'clean',
};

const REEL_ROW_EMPTY_KEY = {
  id: 'r2',
  date: '2026-07-09',
  s3_key: '',
  duration_sec: 60,
  style: 'clean',
};

function fakeClient(options?: { reelsInRange?: unknown[]; singleReelData?: unknown; singleReelError?: { message: string } }) {
  const reelsInRangeData = options?.reelsInRange ?? [REEL_ROW];
  const singleReelData = options?.singleReelData ?? REEL_ROW;
  const singleReelError = options?.singleReelError ?? null;

  let queryType: 'range' | 'single' = 'range';

  const chain = {
    select: () => chain,
    eq: () => chain,
    gte: () => {
      queryType = 'range';
      return chain;
    },
    lte: () => {
      queryType = 'range';
      return chain;
    },
    order: () => {
      queryType = 'single';
      return chain;
    },
    limit: () => {
      const data = queryType === 'range' ? reelsInRangeData : singleReelData ? [singleReelData] : null;
      const error = queryType === 'range' ? null : singleReelError;
      return Promise.resolve({ data, error });
    },
    then: (onFulfilled: (v: { data: unknown; error: unknown }) => unknown) => {
      const data = queryType === 'range' ? reelsInRangeData : singleReelData ? [singleReelData] : null;
      const error = queryType === 'range' ? null : singleReelError;
      return Promise.resolve({ data, error }).then(onFulfilled);
    },
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

  it('handles empty s3_key without crashing', async () => {
    render(
      <ArchiveContainer
        client={fakeClient({ reelsInRange: [REEL_ROW_EMPTY_KEY], singleReelData: REEL_ROW_EMPTY_KEY })}
        deviceId="dev-1"
        now={() => new Date('2026-07-18T12:00:00Z')}
      />,
    );
    await waitFor(() => expect(screen.getByTestId('heatmap-cell-2026-07-09')).toBeTruthy());
    fireEvent.press(screen.getByTestId('heatmap-cell-2026-07-09'));
    await waitFor(() => {
      expect(screen.getByLabelText('Back to archive')).toBeTruthy();
      expect(screen.queryByTestId('video-view')).toBeNull();
    });
  });

  it('shows error when per-day reel fetch fails', async () => {
    const errorClient = fakeClient({
      reelsInRange: [REEL_ROW],
      singleReelData: null,
      singleReelError: { message: 'Failed to fetch reel details' },
    });
    render(
      <ArchiveContainer client={errorClient} deviceId="dev-1" now={() => new Date('2026-07-18T12:00:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('heatmap-cell-2026-07-10')).toBeTruthy());
    fireEvent.press(screen.getByTestId('heatmap-cell-2026-07-10'));
    await waitFor(() => {
      expect(screen.getByLabelText('Reel error')).toBeTruthy();
      expect(screen.getByText('Failed to fetch reel details')).toBeTruthy();
      expect(screen.getByLabelText('Back to archive')).toBeTruthy();
    });
  });
});
