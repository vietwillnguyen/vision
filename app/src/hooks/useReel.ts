import { useEffect, useState } from 'react';

import type { VisionClient } from '../lib/supabase';
import type { Reel } from '../types';
import { parseReelStyle } from '../types';
import type { Tables } from '../generated/database';
import { useResetOnInputChange } from './useResetOnInputChange';

type ReelRow = Tables<'reels'>;

export type ReelState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'none' }
  | { kind: 'ready'; reel: Reel };

export type ReelsState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; reels: Reel[] };

export function useReel(client: VisionClient, deviceId: string, date: string | null): ReelState {
  const [state, setState] = useState<ReelState>({ kind: date ? 'loading' : 'none' });

  useResetOnInputChange([client, deviceId, date], () =>
    setState({ kind: date ? 'loading' : 'none' }),
  );

  useEffect(() => {
    if (!date) {
      return;
    }
    let isMounted = true;

    client
      .from('reels')
      .select('*')
      .eq('device_id', deviceId)
      .eq('date', date)
      .order('created_at', { ascending: false })
      .limit(1)
      .then(
        ({ data, error }) => {
          if (!isMounted) return;
          if (error || !data) {
            setState({ kind: 'error', message: error?.message ?? 'reel fetch failed' });
          } else if (data.length === 0) {
            setState({ kind: 'none' });
          } else {
            setState({ kind: 'ready', reel: mapReelRow(data[0]) });
          }
        },
        (error: unknown) => {
          if (isMounted) setState({ kind: 'error', message: String(error) });
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client, deviceId, date]);

  return state;
}

export function useReelsInRange(
  client: VisionClient,
  deviceId: string,
  startDate: string,
  endDate: string,
): ReelsState {
  const [state, setState] = useState<ReelsState>({ kind: 'loading' });

  useResetOnInputChange([client, deviceId, startDate, endDate], () =>
    setState({ kind: 'loading' }),
  );

  useEffect(() => {
    let isMounted = true;

    client
      .from('reels')
      .select('*')
      .eq('device_id', deviceId)
      .gte('date', startDate)
      .lte('date', endDate)
      .then(
        ({ data, error }) => {
          if (!isMounted) return;
          if (error || !data) {
            setState({ kind: 'error', message: error?.message ?? 'reels fetch failed' });
          } else {
            setState({ kind: 'ready', reels: data.map(mapReelRow) });
          }
        },
        (error: unknown) => {
          if (isMounted) setState({ kind: 'error', message: String(error) });
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client, deviceId, startDate, endDate]);

  return state;
}

function mapReelRow(row: ReelRow): Reel {
  return {
    id: row.id,
    date: row.date,
    s3Key: row.s3_key,
    durationSec: row.duration_sec,
    style: parseReelStyle(row.style),
  };
}
