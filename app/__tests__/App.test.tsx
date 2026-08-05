import { render, screen, waitFor } from '@testing-library/react-native';
import type { Session, SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

import { AppRoot } from '../App';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});
jest.mock('expo-video-thumbnails', () => ({
  getThumbnailAsync: jest.fn(() => Promise.resolve({ uri: 'file:///thumb.jpg' })),
}));
jest.mock('expo-media-library', () => ({
  requestPermissionsAsync: jest.fn(() => Promise.resolve({ granted: true })),
  saveToLibraryAsync: jest.fn(() => Promise.resolve()),
}));
jest.mock('expo-file-system', () => ({
  File: { downloadFileAsync: jest.fn(() => Promise.resolve({ uri: 'file:///cache/s1.mp4' })) },
  Directory: jest.fn(),
  Paths: { cache: 'file:///cache/' },
}));
// App.tsx imports src/lib/supabase for the default export; keep env-independent.
jest.mock('../src/lib/supabase', () => ({ supabase: {}, supabaseClientOptions: {} }));

const SESSION = { access_token: 'tok', user: { id: 'u1' } } as unknown as Session;

function fakeClient(session: Session | null, devices: Record<string, unknown>[]) {
  // Scope `limit()` results per table so the devices fixture only feeds the
  // `devices` query - otherwise it would also satisfy `reels`/other queries
  // that end in `.limit()` and confuse assertions about rendered text.
  function makeChain(rows: Record<string, unknown>[]) {
    const chain = {
      select: () => chain,
      eq: () => chain,
      gte: () => chain,
      lt: () => chain,
      lte: () => chain,
      single: () => Promise.resolve({ data: null, error: { code: 'PGRST116' } }),
      order: (..._a: unknown[]) => chain,
      limit: () => Promise.resolve({ data: rows, error: null }),
      then: (onFulfilled: (v: { data: unknown[]; error: null }) => unknown) =>
        Promise.resolve({ data: [], error: null }).then(onFulfilled),
    };
    return chain;
  }
  return {
    auth: {
      getSession: () => Promise.resolve({ data: { session } }),
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe: jest.fn() } } }),
      signInWithPassword: jest.fn(),
      signOut: jest.fn(),
    },
    from: (table: string) => makeChain(table === 'devices' ? devices : []),
    channel: () => ({ on: () => ({ subscribe: () => ({}) }) }),
    removeChannel: () => {},
    storage: { from: () => ({ createSignedUrl: () => Promise.resolve({ data: null, error: null }) }) },
  } as unknown as SupabaseClient;
}

describe('AppRoot', () => {
  it('shows the auth screen when signed out', async () => {
    render(<AppRoot client={fakeClient(null, [])} />);
    await waitFor(() => expect(screen.getByTestId('email-input')).toBeTruthy());
  });

  it('shows the four tabs when signed in with a device', async () => {
    render(<AppRoot client={fakeClient(SESSION, [{ device_id: 'dev-1', name: 'Pendant' }])} />);
    // exact match: the tab label reads "Today's Reel" while the reel content
    // itself (still loading/none for this fixture) uses lowercase phrasing,
    // so exact:false would ambiguously match both.
    await waitFor(() => expect(screen.getByText("Today's Reel")).toBeTruthy());
    expect(screen.getByText('Raw Footage', { exact: false })).toBeTruthy();
    expect(screen.getByText('Device', { exact: false })).toBeTruthy();
    expect(screen.getByText('Archive', { exact: false })).toBeTruthy();
  });

  it('explains when the account has no device', async () => {
    render(<AppRoot client={fakeClient(SESSION, [])} />);
    await waitFor(() =>
      expect(screen.getByText('No device linked to this account yet.')).toBeTruthy(),
    );
  });
});
