import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { Session } from '@supabase/supabase-js';
import React from 'react';
import { StyleSheet } from 'react-native';

import { AppRoot } from '../App';
import type { VisionClient } from '../src/lib/supabase';

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

jest.mock(
  'react-native-qrcode-svg',
  () =>
    function MockQrCode(props: { value: string }) {
      const { Text } = jest.requireActual('react-native');
      return <Text testID="qr-code">{props.value}</Text>;
    },
);

type Rows = Record<string, unknown>[];

// `devices` is either fixed rows, or a function giving the rows for each
// successive devices read (the app's own lookup and the pairing screen's
// polls both read devices).
function fakeClient(session: Session | null, devices: Rows | (() => Rows)) {
  const readDevices = typeof devices === 'function' ? devices : () => devices;
  // Scope `limit()` results per table so the devices fixture only feeds the
  // `devices` query - otherwise it would also satisfy `reels`/other queries
  // that end in `.limit()` and confuse assertions about rendered text.
  function makeChain(rows: () => Rows) {
    const chain = {
      select: () => chain,
      eq: () => chain,
      gte: () => chain,
      lt: () => chain,
      lte: () => chain,
      single: () => Promise.resolve({ data: null, error: { code: 'PGRST116' } }),
      order: (..._a: unknown[]) => chain,
      limit: () => Promise.resolve({ data: rows(), error: null }),
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
    from: (table: string) => makeChain(table === 'devices' ? readDevices : () => []),
    rpc: jest.fn(() =>
      Promise.resolve({
        data: [{ code: 'code-1', expires_at: new Date(Date.now() + 600000).toISOString() }],
        error: null,
      }),
    ),
    channel: () => ({ on: () => ({ subscribe: () => ({}) }) }),
    removeChannel: () => {},
    storage: {
      from: () => ({ createSignedUrl: () => Promise.resolve({ data: null, error: null }) }),
    },
  } as unknown as VisionClient;
}

describe('AppRoot', () => {
  it('shows the auth screen when signed out', async () => {
    await render(<AppRoot client={fakeClient(null, [])} />);
    await waitFor(() => expect(screen.getByTestId('email-input')).toBeTruthy());
  });

  it('shows the four tabs when signed in with a device', async () => {
    await render(
      <AppRoot client={fakeClient(SESSION, [{ device_id: 'dev-1', name: 'Pendant' }])} />,
    );
    // exact match: the tab label reads "Today's Reel" while the reel content
    // itself (still loading/none for this fixture) uses lowercase phrasing,
    // so exact:false would ambiguously match both.
    await waitFor(() => expect(screen.getByText("Today's Reel")).toBeTruthy());
    expect(screen.getByText('Raw Footage', { exact: false })).toBeTruthy();
    expect(screen.getByText('Device', { exact: false })).toBeTruthy();
    expect(screen.getByText('Archive', { exact: false })).toBeTruthy();
  });

  it('gives every tab a real icon instead of the missing-icon glyph', async () => {
    await render(
      <AppRoot client={fakeClient(SESSION, [{ device_id: 'dev-1', name: 'Pendant' }])} />,
    );
    await waitFor(() => expect(screen.getByText("Today's Reel")).toBeTruthy());
    // Without a tabBarIcon, bottom-tabs falls back to a text glyph (U+23F7)
    // that web system fonts lack, so it renders as an empty box.
    expect(screen.queryAllByText('⏷')).toHaveLength(0);
    for (const id of ['today', 'raw', 'device', 'archive']) {
      // bottom-tabs renders each icon twice (active + inactive) to cross-fade.
      expect(screen.getAllByTestId(`tab-icon-${id}`).length).toBeGreaterThan(0);
    }
  });

  it('keeps the bottom-tab label from shrinking under the icon', async () => {
    await render(
      <AppRoot client={fakeClient(SESSION, [{ device_id: 'dev-1', name: 'Pendant' }])} />,
    );
    await waitFor(() => expect(screen.getByText("Today's Reel")).toBeTruthy());
    // On web the default 48px bar leaves 38px for a 28px icon wrapper plus the
    // label, so flex-shrink squeezed the label to 10px and its
    // `overflow: hidden` cut off descenders ("y", "g"). An explicit line height
    // gives descenders room even when the font's own line box is too short.
    const label = screen.getByText('Raw Footage');
    expect(StyleSheet.flatten(label.props.style)).toMatchObject({
      flexShrink: 0,
      lineHeight: 14,
    });
  });

  it('offers to set up a pendant when the account has no device', async () => {
    await render(<AppRoot client={fakeClient(SESSION, [])} />);
    await waitFor(() => expect(screen.getByText('Set up your Visio')).toBeTruthy());
    expect(screen.getByTestId('setup-button')).toBeTruthy();
  });

  it('pairs a first pendant from the setup screen and then shows the tabs', async () => {
    // The app's lookup and the pairing screen's baseline find no device; the
    // pendant's claim creates one, which the next poll and the refresh see.
    const PENDANT = { device_id: 'dev-new', name: 'visio-pendant', device_user_id: 'p1' };
    let reads = 0;
    const client = fakeClient(SESSION, () => (reads++ < 2 ? [] : [PENDANT]));
    await render(<AppRoot client={client} />);
    await waitFor(() => expect(screen.getByTestId('setup-button')).toBeTruthy());

    await fireEvent.press(screen.getByTestId('setup-button'));
    await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2pass');
    await fireEvent.press(screen.getByTestId('generate-qr-button'));
    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
    // No device yet, so the code asks the claim to create one.
    expect(client.rpc).toHaveBeenCalledWith('create_pairing_code', {});

    await waitFor(() => expect(screen.getByTestId('paired-message')).toBeTruthy(), {
      timeout: 5000,
    });
    await fireEvent.press(screen.getByTestId('done-button'));

    await waitFor(() => expect(screen.getByText("Today's Reel")).toBeTruthy());
  });
});
