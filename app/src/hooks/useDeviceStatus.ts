import type { SupabaseClient } from '@supabase/supabase-js';
import { useEffect, useState } from 'react';

import type { DeviceStatus } from '../types';

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

export function useDeviceStatus(client: SupabaseClient, deviceId: string): DeviceStatusState {
  const [fetchState, setFetchState] = useState<FetchState>({ kind: 'loading' });
  const [realtime, setRealtime] = useState<RealtimeHealth>('connecting');

  useEffect(() => {
    let isMounted = true;
    setFetchState({ kind: 'loading' });
    setRealtime('connecting');

    client
      .from('device_status')
      .select('*')
      .eq('device_id', deviceId)
      .single()
      .then(
        ({ data, error }: { data: Record<string, unknown> | null; error?: { code?: string; message?: string } | null }) => {
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
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'device_status', filter: `device_id=eq.${deviceId}` },
        (payload: { new: Record<string, unknown> }) => {
          if (isMounted) {
            setFetchState({ kind: 'ready', status: mapRow(payload.new) });
          }
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

function mapRow(row: Record<string, unknown>): DeviceStatus {
  return {
    batteryPct: row.battery_pct as number,
    storageUsedGb: row.storage_used_gb as number,
    storageFreeGb: row.storage_free_gb as number,
    segmentsPending: row.segments_pending as number,
    segmentsUploadedToday: row.segments_uploaded_today as number,
    recordingActive: row.recording_active as boolean,
  };
}
