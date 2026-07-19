import type { SupabaseClient } from '@supabase/supabase-js';
import { useEffect, useRef, useState } from 'react';

import { buildSegmentSignedUrlRequest } from '../logic/segmentExport';
import type { TimelineSlot } from '../logic/timeline';

export function useSlotThumbnails(
  client: SupabaseClient,
  slots: TimelineSlot[],
): Record<number, string> {
  const [thumbnails, setThumbnails] = useState<Record<number, string>>({});
  const cache = useRef(new Map<string, string>());

  useEffect(() => {
    let cancelled = false;

    // Rebuild the visible map from the current slots so thumbnails for
    // segments that are no longer present get pruned immediately, while
    // the segment-id cache is kept around for extraction dedupe.
    setThumbnails((prev) => {
      const next: Record<number, string> = {};
      for (const slot of slots) {
        if (!slot.segment) continue;
        const cached = cache.current.get(slot.segment.id);
        if (cached) {
          next[slot.startMinute] = cached;
        }
      }
      const prevKeys = Object.keys(prev);
      const nextKeys = Object.keys(next);
      if (
        prevKeys.length === nextKeys.length &&
        prevKeys.every((key) => prev[Number(key)] === next[Number(key)])
      ) {
        return prev;
      }
      return next;
    });

    (async () => {
      for (const slot of slots) {
        if (!slot.segment) continue;
        let uri = cache.current.get(slot.segment.id);
        if (!uri) {
          try {
            // Required lazily: expo-video-thumbnails has no web implementation,
            // and executing the module at import time crashes the web bundle.
            // Failures here already log and skip, so web degrades to no thumbnails.
            const VideoThumbnails =
              require('expo-video-thumbnails') as typeof import('expo-video-thumbnails');
            const request = buildSegmentSignedUrlRequest(slot.segment.s3Key);
            const { data } = await client.storage
              .from(request.bucket)
              .createSignedUrl(request.path, request.expiresInSec);
            if (!data?.signedUrl) continue;
            const result = await VideoThumbnails.getThumbnailAsync(data.signedUrl, { time: 0 });
            uri = result.uri;
            cache.current.set(slot.segment.id, uri);
          } catch (error) {
            console.warn('useSlotThumbnails: extraction failed', slot.segment.id, error);
            continue;
          }
        }
        if (cancelled) return;
        const resolved = uri;
        setThumbnails((prev) =>
          prev[slot.startMinute] === resolved ? prev : { ...prev, [slot.startMinute]: resolved },
        );
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [client, slots]);

  return thumbnails;
}
