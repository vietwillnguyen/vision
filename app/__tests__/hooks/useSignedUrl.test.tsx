import { renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import { useSignedUrl } from '../../src/hooks/useSignedUrl';

function fakeStorageClient(signedUrl: string | null) {
  const createSignedUrl = jest.fn().mockResolvedValue({
    data: signedUrl ? { signedUrl } : null,
    error: signedUrl ? null : { message: 'not found' },
  });
  const client = { storage: { from: jest.fn(() => ({ createSignedUrl })) } } as unknown as SupabaseClient;
  return { client, createSignedUrl };
}

describe('useSignedUrl', () => {
  it('resolves a signed url for the request', async () => {
    const { client, createSignedUrl } = fakeStorageClient('https://signed/url.mp4');
    const request = { bucket: 'reels' as const, path: 'dev-1/r.mp4', expiresInSec: 3600 };
    const { result } = renderHook(() => useSignedUrl(client, request));
    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current).toBe('https://signed/url.mp4'));
    expect(createSignedUrl).toHaveBeenCalledWith('dev-1/r.mp4', 3600);
  });

  it('stays null for a null request', () => {
    const { client } = fakeStorageClient('https://signed/url.mp4');
    const { result } = renderHook(() => useSignedUrl(client, null));
    expect(result.current).toBeNull();
  });
});
