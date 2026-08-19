import { useEffect, useState } from 'react';

import type { VisionClient } from '../lib/supabase';
import { useResetOnInputChange } from './useResetOnInputChange';

export type DeviceState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'none' }
  | { kind: 'ready'; deviceId: string; name: string };

export function useDevice(client: VisionClient): DeviceState {
  const [state, setState] = useState<DeviceState>({ kind: 'loading' });

  useResetOnInputChange([client], () => setState({ kind: 'loading' }));

  useEffect(() => {
    let isMounted = true;

    client
      .from('devices')
      .select('device_id, name')
      .order('created_at', { ascending: true })
      .limit(1)
      .then(
        ({ data, error }) => {
          if (!isMounted) return;
          if (error) {
            setState({ kind: 'error', message: error.message });
          } else if (!data || data.length === 0) {
            setState({ kind: 'none' });
          } else {
            setState({
              kind: 'ready',
              deviceId: data[0].device_id,
              name: data[0].name,
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
