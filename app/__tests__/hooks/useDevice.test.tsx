import { act, renderHook, waitFor } from '@testing-library/react-native';

import { useDevice } from '../../src/hooks/useDevice';
import type { VisionClient } from '../../src/lib/supabase';

type DeviceQueryResult = { data: unknown[] | null; error: { message: string } | null };

// Each query takes the next result; the last one repeats.
function fakeClient(...results: (DeviceQueryResult | Promise<DeviceQueryResult>)[]) {
  const selected: string[] = [];
  let queries = 0;
  const client = {
    from: () => ({
      select: (columns: string) => {
        selected.push(columns);
        const result = results[Math.min(queries, results.length - 1)];
        queries += 1;
        return { order: () => ({ limit: () => Promise.resolve(result) }) };
      },
    }),
  } as unknown as VisionClient;
  return { client, selected, queries: () => queries };
}

const PAIRED_ROW = { device_id: 'dev-1', name: 'Pendant', device_user_id: 'pendant-user' };

describe('useDevice', () => {
  it('resolves the first device', async () => {
    // An awaited render flushes already-resolved promises, so the fetch is
    // held open to observe the state before it lands.
    let respond!: (value: DeviceQueryResult) => void;
    const { client } = fakeClient(new Promise((resolve) => (respond = resolve)));
    const { result } = await renderHook(() => useDevice(client));
    expect(result.current).toMatchObject({ kind: 'loading' });
    respond({ data: [PAIRED_ROW], error: null });
    await waitFor(() =>
      expect(result.current).toMatchObject({
        kind: 'ready',
        deviceId: 'dev-1',
        name: 'Pendant',
        paired: true,
      }),
    );
  });

  it('selects the pairing link with the device', async () => {
    const { client, selected } = fakeClient({ data: [PAIRED_ROW], error: null });
    await renderHook(() => useDevice(client));
    expect(selected).toEqual(['device_id, name, device_user_id']);
  });

  it('reports a device whose pendant was removed as not paired', async () => {
    const { client } = fakeClient({
      data: [{ ...PAIRED_ROW, device_user_id: null }],
      error: null,
    });
    const { result } = await renderHook(() => useDevice(client));
    await waitFor(() => expect(result.current).toMatchObject({ kind: 'ready', paired: false }));
  });

  it('resolves none when the user has no devices', async () => {
    const { client } = fakeClient({ data: [], error: null });
    const { result } = await renderHook(() => useDevice(client));
    await waitFor(() => expect(result.current).toMatchObject({ kind: 'none' }));
  });

  it('surfaces query errors', async () => {
    const { client } = fakeClient({ data: null, error: { message: 'boom' } });
    const { result } = await renderHook(() => useDevice(client));
    await waitFor(() => expect(result.current).toMatchObject({ kind: 'error', message: 'boom' }));
  });

  it('refresh re-queries and picks up a device the pendant just created', async () => {
    // A first pairing creates the device row when the pendant claims the code.
    const { client, queries } = fakeClient(
      { data: [], error: null },
      { data: [PAIRED_ROW], error: null },
    );
    const { result } = await renderHook(() => useDevice(client));
    await waitFor(() => expect(result.current).toMatchObject({ kind: 'none' }));

    await act(async () => result.current.refresh());

    await waitFor(() =>
      expect(result.current).toMatchObject({ kind: 'ready', deviceId: 'dev-1', paired: true }),
    );
    expect(queries()).toBe(2);
  });
});
