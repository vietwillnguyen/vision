import type { Session, SupabaseClient } from '@supabase/supabase-js';
import { useCallback, useEffect, useState } from 'react';

export type AuthState =
  | { kind: 'loading' }
  | { kind: 'signed-out' }
  | { kind: 'signed-in'; session: Session };

export function useAuth(client: SupabaseClient): {
  state: AuthState;
  signIn: (email: string, password: string) => Promise<string | null>;
  signOut: () => Promise<void>;
} {
  const [state, setState] = useState<AuthState>({ kind: 'loading' });

  useEffect(() => {
    let isMounted = true;

    client.auth.getSession().then(({ data: { session } }) => {
      if (isMounted) {
        setState(session ? { kind: 'signed-in', session } : { kind: 'signed-out' });
      }
    });

    const {
      data: { subscription },
    } = client.auth.onAuthStateChange((_event, session) => {
      if (isMounted) {
        setState(session ? { kind: 'signed-in', session } : { kind: 'signed-out' });
      }
    });

    return () => {
      isMounted = false;
      subscription.unsubscribe();
    };
  }, [client]);

  const signIn = useCallback(
    async (email: string, password: string): Promise<string | null> => {
      const { error } = await client.auth.signInWithPassword({ email, password });
      return error ? error.message : null;
    },
    [client],
  );

  const signOut = useCallback(async () => {
    await client.auth.signOut();
  }, [client]);

  return { state, signIn, signOut };
}
