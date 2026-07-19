import { act, renderHook, waitFor } from '@testing-library/react-native';
import type { Session, SupabaseClient } from '@supabase/supabase-js';

import { useAuth } from '../../src/hooks/useAuth';

const SESSION = { access_token: 'tok', user: { id: 'u1' } } as unknown as Session;

function fakeAuthClient(initialSession: Session | null) {
  let authCallback: ((event: string, session: Session | null) => void) | null = null;
  const signInWithPassword = jest.fn();
  const signOut = jest.fn().mockResolvedValue({ error: null });
  const client = {
    auth: {
      getSession: () => Promise.resolve({ data: { session: initialSession } }),
      onAuthStateChange: (cb: (event: string, session: Session | null) => void) => {
        authCallback = cb;
        return { data: { subscription: { unsubscribe: jest.fn() } } };
      },
      signInWithPassword,
      signOut,
    },
  } as unknown as SupabaseClient;
  return {
    client,
    signInWithPassword,
    emitAuthChange: (event: string, session: Session | null) =>
      act(() => authCallback?.(event, session)),
  };
}

function fakeAuthClientWithFailingSession(error: Error) {
  const client = {
    auth: {
      getSession: () => Promise.reject(error),
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe: jest.fn() } } }),
      signInWithPassword: jest.fn(),
      signOut: jest.fn().mockResolvedValue({ error: null }),
    },
  } as unknown as SupabaseClient;
  return { client };
}

describe('useAuth', () => {
  it('starts loading then resolves to signed-out without a session', async () => {
    const { client } = fakeAuthClient(null);
    const { result } = renderHook(() => useAuth(client));
    expect(result.current.state).toEqual({ kind: 'loading' });
    await waitFor(() => expect(result.current.state).toEqual({ kind: 'signed-out' }));
  });

  it('falls back to signed-out when getSession rejects', async () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    const { client } = fakeAuthClientWithFailingSession(new Error('AsyncStorage unavailable'));
    const { result } = renderHook(() => useAuth(client));
    expect(result.current.state).toEqual({ kind: 'loading' });
    await waitFor(() => expect(result.current.state).toEqual({ kind: 'signed-out' }));
    errorSpy.mockRestore();
  });

  it('restores a persisted session', async () => {
    const { client } = fakeAuthClient(SESSION);
    const { result } = renderHook(() => useAuth(client));
    await waitFor(() =>
      expect(result.current.state).toEqual({ kind: 'signed-in', session: SESSION }),
    );
  });

  it('follows auth state changes', async () => {
    const { client, emitAuthChange } = fakeAuthClient(null);
    const { result } = renderHook(() => useAuth(client));
    await waitFor(() => expect(result.current.state.kind).toBe('signed-out'));
    emitAuthChange('SIGNED_IN', SESSION);
    expect(result.current.state).toEqual({ kind: 'signed-in', session: SESSION });
    emitAuthChange('SIGNED_OUT', null);
    expect(result.current.state).toEqual({ kind: 'signed-out' });
  });

  it('signIn resolves null on success and a message on failure', async () => {
    const { client, signInWithPassword } = fakeAuthClient(null);
    const { result } = renderHook(() => useAuth(client));
    await waitFor(() => expect(result.current.state.kind).toBe('signed-out'));

    signInWithPassword.mockResolvedValueOnce({ data: {}, error: null });
    await expect(result.current.signIn('a@b.co', 'password123')).resolves.toBeNull();
    expect(signInWithPassword).toHaveBeenCalledWith({ email: 'a@b.co', password: 'password123' });

    signInWithPassword.mockResolvedValueOnce({ data: {}, error: { message: 'Invalid login credentials' } });
    await expect(result.current.signIn('a@b.co', 'wrong-password')).resolves.toBe(
      'Invalid login credentials',
    );
  });
});
