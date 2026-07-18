import { renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import { useDevice } from '../../src/hooks/useDevice';

function fakeClient(result: { data: unknown[] | null; error: { message: string } | null }) {
  return {
    from: () => ({
      select: () => ({ order: () => ({ limit: () => Promise.resolve(result) }) }),
    }),
  } as unknown as SupabaseClient;
}

describe('useDevice', () => {
  it('resolves the first device', async () => {
    const client = fakeClient({ data: [{ device_id: 'dev-1', name: 'Pendant' }], error: null });
    const { result } = renderHook(() => useDevice(client));
    expect(result.current).toEqual({ kind: 'loading' });
    await waitFor(() =>
      expect(result.current).toEqual({ kind: 'ready', deviceId: 'dev-1', name: 'Pendant' }),
    );
  });

  it('resolves none when the user has no devices', async () => {
    const { result } = renderHook(() => useDevice(fakeClient({ data: [], error: null })));
    await waitFor(() => expect(result.current).toEqual({ kind: 'none' }));
  });

  it('surfaces query errors', async () => {
    const { result } = renderHook(() =>
      useDevice(fakeClient({ data: null, error: { message: 'boom' } })),
    );
    await waitFor(() => expect(result.current).toEqual({ kind: 'error', message: 'boom' }));
  });
});
