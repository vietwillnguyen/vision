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
    const client = fakeClient({ data: [], error: null });
    const { result } = renderHook(() => useDevice(client));
    await waitFor(() => expect(result.current).toEqual({ kind: 'none' }));
  });

  it('surfaces query errors', async () => {
    const client = fakeClient({ data: null, error: { message: 'boom' } });
    const { result } = renderHook(() => useDevice(client));
    await waitFor(() => expect(result.current).toEqual({ kind: 'error', message: 'boom' }));
  });
});
