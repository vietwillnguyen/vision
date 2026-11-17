import { act, renderHook, waitFor } from '@testing-library/react-native';
import type { Session } from '@supabase/supabase-js';

import { useAuth } from '../../src/hooks/useAuth';
import type { VisionClient } from '../../src/lib/supabase';

const SESSION = { access_token: 'tok', user: { id: 'u1' } } as unknown as Session;

type SessionResult = { data: { session: Session | null } };

function fakeAuthClient(initialSession: Session | null | Promise<SessionResult>) {
  let authCallback: ((event: string, session: Session | null) => void) | null = null;
  const signInWithPassword = jest.fn();
  const signOut = jest.fn().mockResolvedValue({ error: null });
  const client = {
    auth: {
      getSession: () =>
        initialSession instanceof Promise
          ? initialSession
          : Promise.resolve({ data: { session: initialSession } }),
      onAuthStateChange: (cb: (event: string, session: Session | null) => void) => {
        authCallback = cb;
        return { data: { subscription: { unsubscribe: jest.fn() } } };
      },
      signInWithPassword,
      signOut,
    },
  } as unknown as VisionClient;
  return {
    client,
    signInWithPassword,
    emitAuthChange: (event: string, session: Session | null) =>
      act(() => authCallback?.(event, session)),
  };
}

// getSession stays pending until failSession() so the loading state is observable.
function fakeAuthClientWithFailingSession(error: Error) {
  let failSession!: () => void;
  const pending = new Promise<never>((_resolve, reject) => (failSession = () => reject(error)));
  const client = {
    auth: {
      getSession: () => pending,
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe: jest.fn() } } }),
      signInWithPassword: jest.fn(),
      signOut: jest.fn().mockResolvedValue({ error: null }),
    },
  } as unknown as VisionClient;
  return { client, failSession };
}

describe('useAuth', () => {
  it('starts loading then resolves to signed-out without a session', async () => {
    // An awaited render flushes already-resolved promises, so the fetch is
    // held open to observe the state before it lands.
    let respond!: (value: SessionResult) => void;
    const { client } = fakeAuthClient(new Promise((resolve) => (respond = resolve)));
    const { result } = await renderHook(() => useAuth(client));
    expect(result.current.state).toEqual({ kind: 'loading' });
    respond({ data: { session: null } });
    await waitFor(() => expect(result.current.state).toEqual({ kind: 'signed-out' }));
  });

  it('falls back to signed-out when getSession rejects', async () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    const { client, failSession } = fakeAuthClientWithFailingSession(
      new Error('AsyncStorage unavailable'),
    );
    const { result } = await renderHook(() => useAuth(client));
    expect(result.current.state).toEqual({ kind: 'loading' });
    failSession();
    await waitFor(() => expect(result.current.state).toEqual({ kind: 'signed-out' }));
    errorSpy.mockRestore();
  });

  it('restores a persisted session', async () => {
    const { client } = fakeAuthClient(SESSION);
    const { result } = await renderHook(() => useAuth(client));
    await waitFor(() =>
      expect(result.current.state).toEqual({ kind: 'signed-in', session: SESSION }),
    );
  });

  it('follows auth state changes', async () => {
    const { client, emitAuthChange } = fakeAuthClient(null);
    const { result } = await renderHook(() => useAuth(client));
    await waitFor(() => expect(result.current.state.kind).toBe('signed-out'));
    await emitAuthChange('SIGNED_IN', SESSION);
    expect(result.current.state).toEqual({ kind: 'signed-in', session: SESSION });
    await emitAuthChange('SIGNED_OUT', null);
    expect(result.current.state).toEqual({ kind: 'signed-out' });
  });

  it('signIn resolves null on success and a message on failure', async () => {
    const { client, signInWithPassword } = fakeAuthClient(null);
    const { result } = await renderHook(() => useAuth(client));
    await waitFor(() => expect(result.current.state.kind).toBe('signed-out'));

    signInWithPassword.mockResolvedValueOnce({ data: {}, error: null });
    await expect(result.current.signIn('a@b.co', 'password123')).resolves.toBeNull();
    expect(signInWithPassword).toHaveBeenCalledWith({ email: 'a@b.co', password: 'password123' });

    signInWithPassword.mockResolvedValueOnce({
      data: {},
      error: { message: 'Invalid login credentials' },
    });
    await expect(result.current.signIn('a@b.co', 'wrong-password')).resolves.toBe(
      'Invalid login credentials',
    );
  });
});
