import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';
import { Alert } from 'react-native';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});
jest.mock('expo-video-thumbnails', () => ({
  getThumbnailAsync: jest.fn(() => Promise.resolve({ uri: 'file:///thumb.jpg' })),
}));
jest.mock('expo-media-library', () => ({
  requestPermissionsAsync: jest.fn(() => Promise.resolve({ granted: true })),
  saveToLibraryAsync: jest.fn(() => Promise.resolve()),
}));
jest.mock('expo-file-system', () => ({
  File: { downloadFileAsync: jest.fn(() => Promise.resolve({ uri: 'file:///cache/s1.mp4' })) },
  Directory: jest.fn(),
  Paths: { cache: 'file:///cache/' },
}));

import { RawFootageContainer } from '../../src/containers/RawFootageContainer';

const SEGMENT_ROW = {
  id: 's1',
  recorded_at: '2026-07-18T08:00:00Z',
  duration_sec: 120,
  s3_key: 'dev-1/s1.mp4',
  manually_flagged: false,
  user_feedback: null,
};

function fakeClient() {
  const updateCalls: { values: Record<string, unknown>; id: string }[] = [];
  const chain = {
    select: () => chain,
    eq: () => chain,
    gte: () => chain,
    lt: () => chain,
    order: () => Promise.resolve({ data: [SEGMENT_ROW], error: null }),
  };
  const client = {
    from: () => ({
      ...chain,
      update: (values: Record<string, unknown>) => ({
        eq: (_c: string, id: string) => {
          updateCalls.push({ values, id });
          return Promise.resolve({ error: null });
        },
      }),
    }),
    storage: {
      from: () => ({
        createSignedUrl: () =>
          Promise.resolve({ data: { signedUrl: 'https://signed/s1.mp4' }, error: null }),
      }),
    },
  } as unknown as SupabaseClient;
  return { client, updateCalls };
}

describe('RawFootageContainer', () => {
  it('renders the timeline once segments load', async () => {
    const { client } = fakeClient();
    render(
      <RawFootageContainer client={client} deviceId="dev-1" now={() => new Date('2026-07-18T15:00:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('timeline-scrubber')).toBeTruthy());
    expect(screen.getByTestId('slot-480')).toBeTruthy();
  });

  it('long-press offers feedback options that persist user_feedback', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    const { client, updateCalls } = fakeClient();
    render(
      <RawFootageContainer client={client} deviceId="dev-1" now={() => new Date('2026-07-18T15:00:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('slot-480')).toBeTruthy());
    fireEvent(screen.getByTestId('slot-480'), 'longPress');
    expect(alertSpy).toHaveBeenCalled();
    const buttons = alertSpy.mock.calls[0][2] as { text: string; onPress?: () => void }[];
    const alwaysInclude = buttons.find((b) => b.text === 'Always include');
    expect(alwaysInclude).toBeDefined();
    await waitFor(async () => {
      alwaysInclude?.onPress?.();
      expect(updateCalls).toEqual([{ values: { user_feedback: 'include' }, id: 's1' }]);
    });
    alertSpy.mockRestore();
  });

  it('long-press Preview opens the segment preview with playback', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    const { client } = fakeClient();
    render(
      <RawFootageContainer client={client} deviceId="dev-1" now={() => new Date('2026-07-18T15:00:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('slot-480')).toBeTruthy());
    fireEvent(screen.getByTestId('slot-480'), 'longPress');
    const buttons = alertSpy.mock.calls[0][2] as { text: string; onPress?: () => void }[];
    buttons.find((b) => b.text === 'Preview')?.onPress?.();
    await waitFor(() => expect(screen.getByTestId('segment-preview')).toBeTruthy());
    alertSpy.mockRestore();
  });
});
