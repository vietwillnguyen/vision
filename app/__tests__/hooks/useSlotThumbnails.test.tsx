import { renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import * as VideoThumbnails from 'expo-video-thumbnails';

import { useSlotThumbnails } from '../../src/hooks/useSlotThumbnails';
import type { TimelineSlot } from '../../src/logic/timeline';

jest.mock('expo-video-thumbnails', () => ({
  getThumbnailAsync: jest.fn((uri: string) => Promise.resolve({ uri: `thumb:${uri}` })),
}));

const SEGMENT = {
  id: 's1',
  recordedAt: '2026-07-18T08:00:00Z',
  durationSec: 120,
  s3Key: 'dev-1/s1.mp4',
  manuallyFlagged: false,
  userFeedback: null,
};

const SLOTS: TimelineSlot[] = [
  { startMinute: 480, segment: SEGMENT, isFlagged: false },
  { startMinute: 485, segment: null, isFlagged: false },
];

function fakeStorageClient() {
  return {
    storage: {
      from: () => ({
        createSignedUrl: (path: string) =>
          Promise.resolve({ data: { signedUrl: `https://signed/${path}` }, error: null }),
      }),
    },
  } as unknown as SupabaseClient;
}

describe('useSlotThumbnails', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('extracts thumbnails for occupied slots only', async () => {
    const { result } = renderHook(() => useSlotThumbnails(fakeStorageClient(), SLOTS));
    await waitFor(() =>
      expect(result.current).toEqual({ 480: 'thumb:https://signed/dev-1/s1.mp4' }),
    );
  });

  it('caches by segment id across slot-array identity changes', async () => {
    const { result, rerender } = renderHook(
      (props: { slots: TimelineSlot[] }) => useSlotThumbnails(fakeStorageClient(), props.slots),
      { initialProps: { slots: SLOTS } },
    );
    await waitFor(() => expect(Object.keys(result.current)).toHaveLength(1));
    rerender({ slots: [...SLOTS] });
    await waitFor(() => expect(Object.keys(result.current)).toHaveLength(1));
    expect(VideoThumbnails.getThumbnailAsync).toHaveBeenCalledTimes(1);
  });

  it('prunes stale thumbnails when a segment is removed from the slots', async () => {
    const client = fakeStorageClient();
    const { result, rerender } = renderHook(
      (props: { slots: TimelineSlot[] }) => useSlotThumbnails(client, props.slots),
      { initialProps: { slots: SLOTS } },
    );
    await waitFor(() => expect(result.current).toEqual({ 480: 'thumb:https://signed/dev-1/s1.mp4' }));

    const withoutSegment: TimelineSlot[] = [
      { startMinute: 480, segment: null, isFlagged: false },
      { startMinute: 485, segment: null, isFlagged: false },
    ];
    rerender({ slots: withoutSegment });
    await waitFor(() => expect(result.current[480]).toBeUndefined());
    expect(result.current).toEqual({});
  });
});
