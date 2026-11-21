import { useEffect, useState } from 'react';

import type { VisionClient } from '../lib/supabase';
import type { SignedUrlRequest } from '../logic/segmentExport';
import { useResetOnInputChange } from './useResetOnInputChange';

export type SignedUrlState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'ready'; url: string }
  | { kind: 'error'; message: string };

/**
 * Sign a storage object for playback, keeping a failure distinguishable from
 * "still signing" so a player can say so instead of waiting forever.
 */
export function useSignedUrlState(
  client: VisionClient,
  request: SignedUrlRequest | null,
): SignedUrlState {
  const bucket = request?.bucket ?? null;
  const path = request?.path ?? null;
  const expiresInSec = request?.expiresInSec ?? null;
  const active = Boolean(bucket && path && expiresInSec);
  const [state, setState] = useState<SignedUrlState>({ kind: active ? 'loading' : 'idle' });

  useResetOnInputChange([client, bucket, path, expiresInSec], () =>
    setState({ kind: active ? 'loading' : 'idle' }),
  );

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
            setState({ kind: 'ready', url: data.signedUrl });
          } else {
            console.warn('useSignedUrl: signing failed', error);
            setState({ kind: 'error', message: describeError(error) });
          }
        },
        (error: unknown) => {
          console.error('useSignedUrl: signing failed', error);
          if (isMounted) setState({ kind: 'error', message: describeError(error) });
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client, bucket, path, expiresInSec]);

  return state;
}

export function useSignedUrl(
  client: VisionClient,
  request: SignedUrlRequest | null,
): string | null {
  const state = useSignedUrlState(client, request);
  return state.kind === 'ready' ? state.url : null;
}

function describeError(error: unknown): string {
  if (error && typeof error === 'object' && 'message' in error) {
    return String((error as { message: unknown }).message);
  }
  return error ? String(error) : 'signing failed';
}
