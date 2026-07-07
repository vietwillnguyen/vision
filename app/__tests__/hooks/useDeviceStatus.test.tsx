import { act, renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import { useDeviceStatus } from '../../src/hooks/useDeviceStatus';

function createFakeClient(initialRow: Record<string, unknown> | null) {
  let onUpdate: ((payload: { new: Record<string, unknown> }) => void) | null = null;

  const client = {
    from: () => ({
      select: () => ({
        eq: () => ({
          single: () => Promise.resolve({ data: initialRow }),
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
        return { subscribe: () => ({}) };
      },
    }),
    removeChannel: () => {},
  };

  return {
    client: client as unknown as SupabaseClient,
    triggerUpdate: (row: Record<string, unknown>) => act(() => onUpdate?.({ new: row })),
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
});
