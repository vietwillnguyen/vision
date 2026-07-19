import type { SupabaseClient } from '@supabase/supabase-js';
import { useEffect, useState } from 'react';

export type DeviceState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'none' }
  | { kind: 'ready'; deviceId: string; name: string };

export function useDevice(client: SupabaseClient): DeviceState {
  const [state, setState] = useState<DeviceState>({ kind: 'loading' });

  useEffect(() => {
    let isMounted = true;
    setState({ kind: 'loading' });

    client
      .from('devices')
      .select('device_id, name')
      .order('created_at', { ascending: true })
      .limit(1)
      .then(
        ({ data, error }: { data: Record<string, unknown>[] | null; error: { message: string } | null }) => {
          if (!isMounted) return;
          if (error) {
            setState({ kind: 'error', message: error.message });
          } else if (!data || data.length === 0) {
            setState({ kind: 'none' });
          } else {
            setState({
              kind: 'ready',
              deviceId: data[0].device_id as string,
              name: data[0].name as string,
            });
          }
        },
        (error: unknown) => {
          if (isMounted) setState({ kind: 'error', message: String(error) });
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client]);

  return state;
}
