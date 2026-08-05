import type { SupabaseClient } from '@supabase/supabase-js';
import { useEffect, useState } from 'react';

import type { SignedUrlRequest } from '../logic/segmentExport';
import { useResetOnInputChange } from './useResetOnInputChange';

export function useSignedUrl(
  client: SupabaseClient,
  request: SignedUrlRequest | null,
): string | null {
  const [url, setUrl] = useState<string | null>(null);
  const bucket = request?.bucket ?? null;
  const path = request?.path ?? null;
  const expiresInSec = request?.expiresInSec ?? null;

  useResetOnInputChange([client, bucket, path, expiresInSec], () => setUrl(null));

  useEffect(() => {
    if (!bucket || !path || !expiresInSec) {
      return;
    }
    let isMounted = true;

    client.storage
      .from(bucket)
      .createSignedUrl(path, expiresInSec)
      .then(
        ({ data, error }: { data: { signedUrl: string } | null; error?: unknown }) => {
          if (!isMounted) return;
          if (data) {
            setUrl(data.signedUrl);
          } else {
            console.warn('useSignedUrl: signing failed', error);
          }
        },
        (error: unknown) => {
          console.error('useSignedUrl: signing failed', error);
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client, bucket, path, expiresInSec]);

  return url;
}
