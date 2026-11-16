import { useEffect, useState } from 'react';

import type { VisionClient } from '../lib/supabase';
import type { DeviceStatus } from '../types';
import type { Tables } from '../generated/database';
import { useResetOnInputChange } from './useResetOnInputChange';

type DeviceStatusRow = Tables<'device_status'>;

export type RealtimeHealth = 'connecting' | 'live' | 'stale';

export type DeviceStatusState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; status: DeviceStatus; realtime: RealtimeHealth };

type FetchState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; status: DeviceStatus };

const NO_ROW_CODE = 'PGRST116';

export function useDeviceStatus(client: VisionClient, deviceId: string): DeviceStatusState {
  const [fetchState, setFetchState] = useState<FetchState>({ kind: 'loading' });
  const [realtime, setRealtime] = useState<RealtimeHealth>('connecting');

  useResetOnInputChange([client, deviceId], () => {
    setFetchState({ kind: 'loading' });
    setRealtime('connecting');
  });

  useEffect(() => {
    let isMounted = true;

    client
      .from('device_status')
      .select('*')
      .eq('device_id', deviceId)
      .single()
      .then(
        ({ data, error }) => {
          if (!isMounted) return;
          setFetchState((prev) => {
            if (prev.kind === 'ready') return prev; // realtime beat the fetch
            if (data) return { kind: 'ready', status: mapRow(data) };
            // No row yet (first boot) stays loading; anything else is an error.
            if (error && error.code !== NO_ROW_CODE) {
              return { kind: 'error', message: error.message ?? 'device status fetch failed' };
            }
            return prev;
          });
        },
        (error: unknown) => {
          if (!isMounted) return;
          setFetchState((prev) =>
            prev.kind === 'ready' ? prev : { kind: 'error', message: String(error) },
          );
        },
      );

    // '*' rather than UPDATE: the device's first-ever status upsert arrives
    // as an INSERT, and the initial fetch above finds no row on first boot.
    const channel = client
      .channel(`device_status:${deviceId}`)
      .on<DeviceStatusRow>(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'device_status', filter: `device_id=eq.${deviceId}` },
        (payload) => {
          if (!isMounted) return;
          // DELETE carries an empty `new` (it is the *old* row that went away),
          // so mapping it would publish a status whose every field is
          // undefined. The row only disappears when the device itself is
          // deleted; keep the last known status and let the device query drive
          // that transition.
          if (payload.eventType === 'DELETE') return;
          setFetchState({ kind: 'ready', status: mapRow(payload.new) });
        },
      )
      .subscribe((status: string) => {
        if (!isMounted) return;
        if (status === 'SUBSCRIBED') {
          setRealtime('live');
        } else if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT' || status === 'CLOSED') {
          setRealtime('stale');
        }
      });

    return () => {
      isMounted = false;
      client.removeChannel(channel);
    };
  }, [client, deviceId]);

  if (fetchState.kind === 'ready') {
    return { kind: 'ready', status: fetchState.status, realtime };
  }
  return fetchState;
}

function mapRow(row: DeviceStatusRow): DeviceStatus {
  return {
    batteryPct: row.battery_pct,
    storageUsedGb: row.storage_used_gb,
    storageFreeGb: row.storage_free_gb,
    segmentsPending: row.segments_pending,
    segmentsUploadedToday: row.segments_uploaded_today,
    recordingActive: row.recording_active,
  };
}
