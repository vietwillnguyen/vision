import { renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

// Simulates the web platform, where Metro resolves videoThumbnails.web.ts
// (isAvailable: false) instead of videoThumbnails.ts - distinct from a
// single segment's extraction failing after the module resolved fine.
jest.mock('../../src/lib/videoThumbnails', () => ({
  isAvailable: false,
  getThumbnailAsync: jest.fn(() => Promise.reject(new Error('not supported'))),
}));

import { useSlotThumbnails } from '../../src/hooks/useSlotThumbnails';
import type { TimelineSlot } from '../../src/logic/timeline';

const SEGMENT = {
  id: 's1',
  recordedAt: '2026-07-18T08:00:00Z',
  durationSec: 120,
  s3Key: 'dev-1/s1.mp4',
  manuallyFlagged: false,
  userFeedback: null,
};

const SLOTS: TimelineSlot[] = [{ startMinute: 480, segment: SEGMENT, isFlagged: false }];

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

describe('useSlotThumbnails when the platform has no video-thumbnails support', () => {
  it('logs once at error level and never populates thumbnails, without throwing', async () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    const { result } = renderHook(() => useSlotThumbnails(fakeStorageClient(), SLOTS));

    await waitFor(() =>
      expect(errorSpy).toHaveBeenCalledWith(
        'useSlotThumbnails: video thumbnails unavailable on this platform',
      ),
    );
    expect(result.current).toEqual({});
    errorSpy.mockRestore();
  });
});
