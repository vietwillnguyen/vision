import { act, renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import { useDeviceStatus } from '../../src/hooks/useDeviceStatus';

function createFakeClient(
  initialRow: Record<string, unknown> | null,
  options: { deferInitialFetch?: boolean; initialFetchError?: Error } = {},
) {
  const registrations: {
    event: string;
    callback: (payload: { new: Record<string, unknown> }) => void;
  }[] = [];
  let subscribeCalled = false;
  let removeChannelCalls = 0;
  let statusCallback: ((status: string) => void) | null = null;

  let resolveInitialFetch: () => void = () => {};
  const initialFetch: Promise<{ data: Record<string, unknown> | null; error?: { code?: string; message?: string } | null }> =
    options.initialFetchError
      ? Promise.reject(options.initialFetchError)
      : options.deferInitialFetch
        ? new Promise((resolve) => {
            resolveInitialFetch = () => resolve({ data: initialRow });
          })
        : Promise.resolve({ data: initialRow });

  const client = {
    from: () => ({
      select: () => ({
        eq: () => ({
          single: () => initialFetch,
        }),
      }),
    }),
    channel: () => ({
      on: (
        _type: string,
        filter: { event: string },
        callback: (payload: { new: Record<string, unknown> }) => void,
      ) => {
        registrations.push({ event: filter.event, callback });
        return {
          subscribe: (cb?: (status: string) => void) => {
            subscribeCalled = true;
            statusCallback = cb ?? null;
            return {};
          },
        };
      },
    }),
    removeChannel: () => {
      removeChannelCalls += 1;
    },
  };

  const deliver = (eventType: string, row: Record<string, unknown>) =>
    act(() => {
      for (const { event, callback } of registrations) {
        if (event === '*' || event === eventType) {
          callback({ new: row });
        }
      }
    });

  return {
    client: client as unknown as SupabaseClient,
    triggerUpdate: (row: Record<string, unknown>) => deliver('UPDATE', row),
    triggerInsert: (row: Record<string, unknown>) => deliver('INSERT', row),
    resolveInitialFetch: () =>
      act(async () => {
        resolveInitialFetch();
      }),
    getSubscribeCalled: () => subscribeCalled,
    getRemoveChannelCalls: () => removeChannelCalls,
    emitChannelStatus: (status: string) => act(() => statusCallback?.(status)),
  };
}

const ROW = {
  battery_pct: 72,
  storage_used_gb: 4.2,
  storage_free_gb: 118,
  segments_pending: 1,
  segments_uploaded_today: 42,
  recording_active: true,
};

describe('useDeviceStatus', () => {
  it('starts loading', () => {
    const { client } = createFakeClient(null, { deferInitialFetch: true });
    const { result } = renderHook(() => useDeviceStatus(client, 'dev-1'));
    expect(result.current).toEqual({ kind: 'loading' });
  });

  it('reaches ready with connecting realtime after the initial fetch', async () => {
    const { client } = createFakeClient(ROW);
    const { result } = renderHook(() => useDeviceStatus(client, 'dev-1'));
    await waitFor(() => expect(result.current.kind).toBe('ready'));
    expect(result.current).toMatchObject({ kind: 'ready', realtime: 'connecting' });
  });

  it('loads the initial status from the database', async () => {
    const { client } = createFakeClient(ROW);
    const { result } = renderHook(() => useDeviceStatus(client, 'device-abc'));

    await waitFor(() => expect(result.current.kind).toBe('ready'));

    expect(result.current).toMatchObject({
      kind: 'ready',
      status: {
        batteryPct: 72,
        storageUsedGb: 4.2,
        storageFreeGb: 118,
        segmentsPending: 1,
        segmentsUploadedToday: 42,
        recordingActive: true,
      },
    });
  });

  it('updates status when a realtime event arrives', async () => {
    const { client, triggerUpdate } = createFakeClient(ROW);
    const { result } = renderHook(() => useDeviceStatus(client, 'device-abc'));

    await waitFor(() => expect(result.current.kind).toBe('ready'));

    triggerUpdate({ ...ROW, battery_pct: 65 });

    expect(result.current).toMatchObject({ kind: 'ready', status: { batteryPct: 65 } });
  });

  it('does not overwrite a realtime update with a slower initial fetch', async () => {
    const { client, triggerUpdate, resolveInitialFetch } = createFakeClient(ROW, {
      deferInitialFetch: true,
    });
    const { result } = renderHook(() => useDeviceStatus(client, 'device-abc'));

    triggerUpdate({ ...ROW, battery_pct: 55 });
    expect(result.current).toMatchObject({ kind: 'ready', status: { batteryPct: 55 } });

    await resolveInitialFetch();

    expect(result.current).toMatchObject({ kind: 'ready', status: { batteryPct: 55 } });
  });

  it('surfaces an initial fetch failure as an error state', async () => {
    const { client } = createFakeClient(null, { initialFetchError: new Error('network down') });
    const { result } = renderHook(() => useDeviceStatus(client, 'dev-1'));
    await waitFor(() => expect(result.current.kind).toBe('error'));
  });

  it('shows status from a first-ever INSERT when no row existed at mount', async () => {
    const { client, triggerInsert, getSubscribeCalled } = createFakeClient(null);
    const { result } = renderHook(() => useDeviceStatus(client, 'device-abc'));

    await waitFor(() => expect(getSubscribeCalled()).toBe(true));
    expect(result.current).toEqual({ kind: 'loading' });

    triggerInsert(ROW);

    expect(result.current).toMatchObject({ kind: 'ready', status: { batteryPct: 72 } });
  });

  it('subscribes on mount and removes the channel on unmount', async () => {
    const { client, getSubscribeCalled, getRemoveChannelCalls } = createFakeClient(ROW);
    const { result, unmount } = renderHook(() => useDeviceStatus(client, 'device-abc'));

    await waitFor(() => expect(result.current.kind).toBe('ready'));
    expect(getSubscribeCalled()).toBe(true);
    expect(getRemoveChannelCalls()).toBe(0);

    unmount();

    expect(getRemoveChannelCalls()).toBe(1);
  });

  it('goes live on SUBSCRIBED and stale on CHANNEL_ERROR', async () => {
    const { client, emitChannelStatus } = createFakeClient(ROW);
    const { result } = renderHook(() => useDeviceStatus(client, 'dev-1'));
    await waitFor(() => expect(result.current.kind).toBe('ready'));
    emitChannelStatus('SUBSCRIBED');
    expect(result.current).toMatchObject({ realtime: 'live' });
    emitChannelStatus('CHANNEL_ERROR');
    expect(result.current).toMatchObject({ realtime: 'stale' });
  });

  it('marks stale on TIMED_OUT and CLOSED too', async () => {
    const { client, emitChannelStatus } = createFakeClient(ROW);
    const { result } = renderHook(() => useDeviceStatus(client, 'dev-1'));
    await waitFor(() => expect(result.current.kind).toBe('ready'));
    emitChannelStatus('TIMED_OUT');
    expect(result.current).toMatchObject({ realtime: 'stale' });
    emitChannelStatus('SUBSCRIBED');
    expect(result.current).toMatchObject({ realtime: 'live' });
    emitChannelStatus('CLOSED');
    expect(result.current).toMatchObject({ realtime: 'stale' });
  });
});
