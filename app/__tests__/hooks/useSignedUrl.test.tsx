import { renderHook, waitFor } from '@testing-library/react-native';

import { useSignedUrl, useSignedUrlState } from '../../src/hooks/useSignedUrl';
import type { VisionClient } from '../../src/lib/supabase';

function fakeStorageClient(signedUrl: string | null) {
  const createSignedUrl = jest.fn().mockResolvedValue({
    data: signedUrl ? { signedUrl } : null,
    error: signedUrl ? null : { message: 'not found' },
  });
  const client = {
    storage: { from: jest.fn(() => ({ createSignedUrl })) },
  } as unknown as VisionClient;
  return { client, createSignedUrl };
}

describe('useSignedUrl', () => {
  it('resolves a signed url for the request', async () => {
    const { client, createSignedUrl } = fakeStorageClient('https://signed/url.mp4');
    // An awaited render flushes already-resolved promises, so the fetch is
    // held open to observe the state before it lands.
    const signed = { data: { signedUrl: 'https://signed/url.mp4' }, error: null };
    let respond!: (value: typeof signed) => void;
    createSignedUrl.mockReturnValueOnce(new Promise((resolve) => (respond = resolve)));
    const request = { bucket: 'reels' as const, path: 'dev-1/r.mp4', expiresInSec: 3600 };
    const { result } = await renderHook(() => useSignedUrl(client, request));
    expect(result.current).toBeNull();
    respond(signed);
    await waitFor(() => expect(result.current).toBe('https://signed/url.mp4'));
    expect(createSignedUrl).toHaveBeenCalledWith('dev-1/r.mp4', 3600);
  });

  it('stays null for a null request', async () => {
    const { client } = fakeStorageClient('https://signed/url.mp4');
    const { result } = await renderHook(() => useSignedUrl(client, null));
    expect(result.current).toBeNull();
  });
});

describe('useSignedUrlState', () => {
  it('reports a signing failure instead of staying pending', async () => {
    const { client } = fakeStorageClient(null);
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const request = { bucket: 'reels' as const, path: 'dev-1/r.mp4', expiresInSec: 3600 };
    const { result } = await renderHook(() => useSignedUrlState(client, request));
    await waitFor(() => expect(result.current).toEqual({ kind: 'error', message: 'not found' }));
    warn.mockRestore();
  });

  it('is idle without a request', async () => {
    const { client } = fakeStorageClient('https://signed/url.mp4');
    const { result } = await renderHook(() => useSignedUrlState(client, null));
    expect(result.current).toEqual({ kind: 'idle' });
  });
});
