import { renderHook, waitFor } from '@testing-library/react-native';

import { useReel, useReelsInRange } from '../../src/hooks/useReel';
import type { VisionClient } from '../../src/lib/supabase';

const ROW = {
  id: 'r1',
  date: '2026-07-18',
  s3_key: 'dev-1/2026-07-18.mp4',
  duration_sec: 60,
  style: 'clean',
};

function fakeReelClient(rows: Record<string, unknown>[]) {
  const chain = {
    select: () => chain,
    eq: () => chain,
    gte: () => chain,
    lte: () => chain,
    order: () => chain,
    limit: () => Promise.resolve({ data: rows, error: null }),
    then: (onFulfilled: (v: { data: unknown; error: null }) => unknown) =>
      Promise.resolve({ data: rows, error: null }).then(onFulfilled),
  };
  return { from: () => chain } as unknown as VisionClient;
}

describe('useReel', () => {
  it('maps the reel row', async () => {
    const client = fakeReelClient([ROW]);
    const { result } = renderHook(() => useReel(client, 'dev-1', '2026-07-18'));
    await waitFor(() =>
      expect(result.current).toEqual({
        kind: 'ready',
        reel: { id: 'r1', date: '2026-07-18', s3Key: 'dev-1/2026-07-18.mp4', durationSec: 60, style: 'clean' },
      }),
    );
  });

  it('resolves none when no reel exists', async () => {
    const client = fakeReelClient([]);
    const { result } = renderHook(() => useReel(client, 'dev-1', '2026-07-18'));
    await waitFor(() => expect(result.current).toEqual({ kind: 'none' }));
  });

  it('falls back to the clean style for a style the app cannot render', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const client = fakeReelClient([{ ...ROW, style: 'noir' }]);
    const { result } = renderHook(() => useReel(client, 'dev-1', '2026-07-18'));

    await waitFor(() => expect(result.current).toMatchObject({ kind: 'ready' }));
    expect(result.current).toMatchObject({ kind: 'ready', reel: { style: 'clean' } });
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('noir'));

    warn.mockRestore();
  });

  it('short-circuits to none for a null date', () => {
    const client = fakeReelClient([ROW]);
    const { result } = renderHook(() => useReel(client, 'dev-1', null));
    expect(result.current).toEqual({ kind: 'none' });
  });
});

describe('useReelsInRange', () => {
  it('maps all rows in the range', async () => {
    const client = fakeReelClient([ROW]);
    const { result } = renderHook(() =>
      useReelsInRange(client, 'dev-1', '2026-06-19', '2026-07-18'),
    );
    await waitFor(() =>
      expect(result.current).toMatchObject({ kind: 'ready', reels: [{ id: 'r1' }] }),
    );
  });
});
