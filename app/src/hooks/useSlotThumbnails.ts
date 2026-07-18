import type { SupabaseClient } from '@supabase/supabase-js';
import * as VideoThumbnails from 'expo-video-thumbnails';
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

    (async () => {
      for (const slot of slots) {
        if (!slot.segment) continue;
        let uri = cache.current.get(slot.segment.id);
        if (!uri) {
          try {
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
