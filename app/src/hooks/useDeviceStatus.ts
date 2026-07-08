import type { SupabaseClient } from '@supabase/supabase-js';
import { useEffect, useState } from 'react';

import type { DeviceStatus } from '../types';

export function useDeviceStatus(client: SupabaseClient, deviceId: string): DeviceStatus | null {
  const [status, setStatus] = useState<DeviceStatus | null>(null);

  useEffect(() => {
    let isMounted = true;
    let realtimeUpdateArrived = false;

    client
      .from('device_status')
      .select('*')
      .eq('device_id', deviceId)
      .single()
      .then(
        ({ data }: { data: Record<string, unknown> | null }) => {
          if (isMounted && !realtimeUpdateArrived && data) {
            setStatus(mapRow(data));
          }
        },
        (error: unknown) => {
          console.error('useDeviceStatus: initial fetch failed', error);
        },
      );

    const channel = client
      .channel(`device_status:${deviceId}`)
      .on(
        'postgres_changes',
        { event: 'UPDATE', schema: 'public', table: 'device_status', filter: `device_id=eq.${deviceId}` },
        (payload: { new: Record<string, unknown> }) => {
          if (isMounted) {
            realtimeUpdateArrived = true;
            setStatus(mapRow(payload.new));
          }
        },
      )
      .subscribe();

    return () => {
      isMounted = false;
      client.removeChannel(channel);
    };
  }, [client, deviceId]);

  return status;
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
