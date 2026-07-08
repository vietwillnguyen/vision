import { act, renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import { useDeviceStatus } from '../../src/hooks/useDeviceStatus';

function createFakeClient(
  initialRow: Record<string, unknown> | null,
  options: { deferInitialFetch?: boolean; initialFetchError?: Error } = {},
) {
  let onUpdate: ((payload: { new: Record<string, unknown> }) => void) | null = null;
  let subscribeCalled = false;
  let removeChannelCalls = 0;

  let resolveInitialFetch: () => void = () => {};
  const initialFetch: Promise<{ data: Record<string, unknown> | null }> = options.initialFetchError
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
        _event: string,
        _filter: unknown,
        callback: (payload: { new: Record<string, unknown> }) => void,
      ) => {
        onUpdate = callback;
        return {
          subscribe: () => {
            subscribeCalled = true;
            return {};
          },
        };
      },
    }),
    removeChannel: () => {
      removeChannelCalls += 1;
    },
  };

  return {
    client: client as unknown as SupabaseClient,
    triggerUpdate: (row: Record<string, unknown>) => act(() => onUpdate?.({ new: row })),
    resolveInitialFetch: () =>
      act(async () => {
        resolveInitialFetch();
      }),
    getSubscribeCalled: () => subscribeCalled,
    getRemoveChannelCalls: () => removeChannelCalls,
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
  it('loads the initial status from the database', async () => {
    const { client } = createFakeClient(ROW);
    const { result } = renderHook(() => useDeviceStatus(client, 'device-abc'));

    await waitFor(() => expect(result.current).not.toBeNull());

    expect(result.current).toEqual({
      batteryPct: 72,
      storageUsedGb: 4.2,
      storageFreeGb: 118,
      segmentsPending: 1,
      segmentsUploadedToday: 42,
      recordingActive: true,
    });
  });

  it('updates status when a realtime event arrives', async () => {
    const { client, triggerUpdate } = createFakeClient(ROW);
    const { result } = renderHook(() => useDeviceStatus(client, 'device-abc'));

    await waitFor(() => expect(result.current).not.toBeNull());

    triggerUpdate({ ...ROW, battery_pct: 65 });

    expect(result.current?.batteryPct).toBe(65);
  });

  it('does not overwrite a realtime update with a slower initial fetch', async () => {
    const { client, triggerUpdate, resolveInitialFetch } = createFakeClient(ROW, {
      deferInitialFetch: true,
    });
    const { result } = renderHook(() => useDeviceStatus(client, 'device-abc'));

    triggerUpdate({ ...ROW, battery_pct: 55 });
    expect(result.current?.batteryPct).toBe(55);

    await resolveInitialFetch();

    expect(result.current?.batteryPct).toBe(55);
  });

  it('logs the error and stays null when the initial fetch fails', async () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    const fetchError = new Error('network down');
    const { client } = createFakeClient(null, { initialFetchError: fetchError });
    const { result } = renderHook(() => useDeviceStatus(client, 'device-abc'));

    await waitFor(() =>
      expect(errorSpy).toHaveBeenCalledWith('useDeviceStatus: initial fetch failed', fetchError),
    );
    expect(result.current).toBeNull();
    errorSpy.mockRestore();
  });

  it('subscribes on mount and removes the channel on unmount', async () => {
    const { client, getSubscribeCalled, getRemoveChannelCalls } = createFakeClient(ROW);
    const { result, unmount } = renderHook(() => useDeviceStatus(client, 'device-abc'));

    await waitFor(() => expect(result.current).not.toBeNull());
    expect(getSubscribeCalled()).toBe(true);
    expect(getRemoveChannelCalls()).toBe(0);

    unmount();

    expect(getRemoveChannelCalls()).toBe(1);
  });
});
