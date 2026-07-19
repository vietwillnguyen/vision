import type { SupabaseClient } from '@supabase/supabase-js';
import { useEffect, useState } from 'react';

import type { Reel } from '../types';

export type ReelState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'none' }
  | { kind: 'ready'; reel: Reel };

export type ReelsState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; reels: Reel[] };

export function useReel(client: SupabaseClient, deviceId: string, date: string | null): ReelState {
  const [state, setState] = useState<ReelState>({ kind: date ? 'loading' : 'none' });

  useEffect(() => {
    if (!date) {
      setState({ kind: 'none' });
      return;
    }
    let isMounted = true;
    setState({ kind: 'loading' });

    client
      .from('reels')
      .select('*')
      .eq('device_id', deviceId)
      .eq('date', date)
      .order('created_at', { ascending: false })
      .limit(1)
      .then(
        ({ data, error }: { data: Record<string, unknown>[] | null; error: { message: string } | null }) => {
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
  client: SupabaseClient,
  deviceId: string,
  startDate: string,
  endDate: string,
): ReelsState {
  const [state, setState] = useState<ReelsState>({ kind: 'loading' });

  useEffect(() => {
    let isMounted = true;
    setState({ kind: 'loading' });

    client
      .from('reels')
      .select('*')
      .eq('device_id', deviceId)
      .gte('date', startDate)
      .lte('date', endDate)
      .then(
        ({ data, error }: { data: Record<string, unknown>[] | null; error: { message: string } | null }) => {
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

function mapReelRow(row: Record<string, unknown>): Reel {
  return {
    id: row.id as string,
    date: row.date as string,
    s3Key: row.s3_key as string,
    durationSec: row.duration_sec as number,
    style: row.style as Reel['style'],
  };
}
