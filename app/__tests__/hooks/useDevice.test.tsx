import { renderHook, waitFor } from '@testing-library/react-native';

import { useDevice } from '../../src/hooks/useDevice';
import type { VisionClient } from '../../src/lib/supabase';

type DeviceQueryResult = { data: unknown[] | null; error: { message: string } | null };

function fakeClient(result: DeviceQueryResult | Promise<DeviceQueryResult>) {
  return {
    from: () => ({
      select: () => ({ order: () => ({ limit: () => Promise.resolve(result) }) }),
    }),
  } as unknown as VisionClient;
}

describe('useDevice', () => {
  it('resolves the first device', async () => {
    // An awaited render flushes already-resolved promises, so the fetch is
    // held open to observe the state before it lands.
    let respond!: (value: DeviceQueryResult) => void;
    const client = fakeClient(new Promise((resolve) => (respond = resolve)));
    const { result } = await renderHook(() => useDevice(client));
    expect(result.current).toEqual({ kind: 'loading' });
    respond({ data: [{ device_id: 'dev-1', name: 'Pendant' }], error: null });
    await waitFor(() =>
      expect(result.current).toEqual({ kind: 'ready', deviceId: 'dev-1', name: 'Pendant' }),
    );
  });

  it('resolves none when the user has no devices', async () => {
    const client = fakeClient({ data: [], error: null });
    const { result } = await renderHook(() => useDevice(client));
    await waitFor(() => expect(result.current).toEqual({ kind: 'none' }));
  });

  it('surfaces query errors', async () => {
    const client = fakeClient({ data: null, error: { message: 'boom' } });
    const { result } = await renderHook(() => useDevice(client));
    await waitFor(() => expect(result.current).toEqual({ kind: 'error', message: 'boom' }));
  });
});
