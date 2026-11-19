import { useCallback, useEffect, useState } from 'react';

import type { VisionClient } from '../lib/supabase';
import { useResetOnInputChange } from './useResetOnInputChange';

export type DeviceState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'none' }
  // `paired` is false once "Remove device" has revoked the pendant's login:
  // the device row and its footage stay, but no pendant can write to it.
  | { kind: 'ready'; deviceId: string; name: string; paired: boolean };

export type DeviceLookup = DeviceState & {
  // Re-query without going back to loading, e.g. once a first pairing has
  // created the device row or a pendant has been removed.
  refresh: () => void;
};

export function useDevice(client: VisionClient): DeviceLookup {
  const [state, setState] = useState<DeviceState>({ kind: 'loading' });
  const [generation, setGeneration] = useState(0);
  const refresh = useCallback(() => setGeneration((current) => current + 1), []);

  useResetOnInputChange([client], () => setState({ kind: 'loading' }));

  useEffect(() => {
    let isMounted = true;

    client
      .from('devices')
      .select('device_id, name, device_user_id')
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
              paired: data[0].device_user_id !== null,
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
  }, [client, generation]);

  return { ...state, refresh };
}
