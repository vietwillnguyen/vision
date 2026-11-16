import { useCallback, useEffect, useState } from 'react';

import type { VisionClient } from '../lib/supabase';
import type { Segment } from '../types';
import { parseUserFeedback } from '../types';
import type { Tables } from '../generated/database';
import { useResetOnInputChange } from './useResetOnInputChange';

type SegmentRow = Tables<'segments'>;

export type SegmentsState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; segments: Segment[] };

const DAY_MS = 24 * 60 * 60 * 1000;

export function useSegments(
  client: VisionClient,
  deviceId: string,
  dayStartIso: string,
): {
  state: SegmentsState;
  setUserFeedback: (segmentId: string, feedback: Segment['userFeedback']) => Promise<void>;
} {
  const [state, setState] = useState<SegmentsState>({ kind: 'loading' });

  useResetOnInputChange([client, deviceId, dayStartIso], () => setState({ kind: 'loading' }));

  useEffect(() => {
    let isMounted = true;
    const dayEndIso = new Date(new Date(dayStartIso).getTime() + DAY_MS).toISOString();

    client
      .from('segments')
      .select('*')
      .eq('device_id', deviceId)
      .gte('recorded_at', dayStartIso)
      .lt('recorded_at', dayEndIso)
      .order('recorded_at', { ascending: true })
      .then(
        ({ data, error }) => {
          if (!isMounted) return;
          if (error || !data) {
            setState({ kind: 'error', message: error?.message ?? 'segments fetch failed' });
          } else {
            setState({ kind: 'ready', segments: data.map(mapSegmentRow) });
          }
        },
        (error: unknown) => {
          if (isMounted) setState({ kind: 'error', message: String(error) });
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client, deviceId, dayStartIso]);

  const setUserFeedback = useCallback(
    async (segmentId: string, feedback: Segment['userFeedback']) => {
      let previous: Segment['userFeedback'] = null;
      setState((prev) => {
        if (prev.kind !== 'ready') return prev;
        return {
          kind: 'ready',
          segments: prev.segments.map((s) => {
            if (s.id !== segmentId) return s;
            previous = s.userFeedback;
            return { ...s, userFeedback: feedback };
          }),
        };
      });

      const { error } = await client
        .from('segments')
        .update({ user_feedback: feedback })
        .eq('id', segmentId);

      if (error) {
        setState((prev) => {
          if (prev.kind !== 'ready') return prev;
          return {
            kind: 'ready',
            segments: prev.segments.map((s) =>
              s.id === segmentId ? { ...s, userFeedback: previous } : s,
            ),
          };
        });
      }
    },
    [client],
  );

  return { state, setUserFeedback };
}

function mapSegmentRow(row: SegmentRow): Segment {
  return {
    id: row.id,
    recordedAt: row.recorded_at,
    durationSec: row.duration_sec,
    s3Key: row.s3_key,
    manuallyFlagged: row.manually_flagged,
    userFeedback: parseUserFeedback(row.user_feedback),
  };
}
