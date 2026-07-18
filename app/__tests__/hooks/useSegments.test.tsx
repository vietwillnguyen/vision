import { act, renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import { useSegments } from '../../src/hooks/useSegments';

const ROWS = [
  {
    id: 's1',
    recorded_at: '2026-07-18T08:00:00Z',
    duration_sec: 120,
    s3_key: 'dev-1/s1.mp4',
    manually_flagged: true,
    user_feedback: null,
  },
];

function fakeClient(options: { updateError?: { message: string } } = {}) {
  const updateCalls: { values: Record<string, unknown>; id: string }[] = [];
  const client = {
    from: () => ({
      select: () => ({
        eq: () => ({
          gte: () => ({
            lt: () => ({
              order: () => Promise.resolve({ data: ROWS, error: null }),
            }),
          }),
        }),
      }),
      update: (values: Record<string, unknown>) => ({
        eq: (_col: string, id: string) => {
          updateCalls.push({ values, id });
          return Promise.resolve({ error: options.updateError ?? null });
        },
      }),
    }),
  } as unknown as SupabaseClient;
  return { client, updateCalls };
}

describe('useSegments', () => {
  it('fetches and maps segments for the day', async () => {
    const { client } = fakeClient();
    const { result } = renderHook(() =>
      useSegments(client, 'dev-1', '2026-07-18T00:00:00.000Z'),
    );
    expect(result.current.state).toEqual({ kind: 'loading' });
    await waitFor(() => expect(result.current.state.kind).toBe('ready'));
    expect(result.current.state).toEqual({
      kind: 'ready',
      segments: [
        {
          id: 's1',
          recordedAt: '2026-07-18T08:00:00Z',
          durationSec: 120,
          s3Key: 'dev-1/s1.mp4',
          manuallyFlagged: true,
          userFeedback: null,
        },
      ],
    });
  });

  it('applies user feedback optimistically and persists it', async () => {
    const { client, updateCalls } = fakeClient();
    const { result } = renderHook(() =>
      useSegments(client, 'dev-1', '2026-07-18T00:00:00.000Z'),
    );
    await waitFor(() => expect(result.current.state.kind).toBe('ready'));
    await act(() => result.current.setUserFeedback('s1', 'include'));
    expect(updateCalls).toEqual([{ values: { user_feedback: 'include' }, id: 's1' }]);
    expect(result.current.state).toMatchObject({
      segments: [expect.objectContaining({ userFeedback: 'include' })],
    });
  });

  it('reverts the optimistic update when the write fails', async () => {
    const { client } = fakeClient({ updateError: { message: 'denied' } });
    const { result } = renderHook(() =>
      useSegments(client, 'dev-1', '2026-07-18T00:00:00.000Z'),
    );
    await waitFor(() => expect(result.current.state.kind).toBe('ready'));
    await act(() => result.current.setUserFeedback('s1', 'exclude'));
    expect(result.current.state).toMatchObject({
      segments: [expect.objectContaining({ userFeedback: null })],
    });
  });
});
