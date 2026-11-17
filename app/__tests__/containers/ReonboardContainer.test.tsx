import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { Session } from '@supabase/supabase-js';
import React from 'react';

import { ReonboardContainer } from '../../src/containers/ReonboardContainer';
import type { VisionClient } from '../../src/lib/supabase';

jest.mock(
  'react-native-qrcode-svg',
  () =>
    function MockQrCode(props: { value: string }) {
      const { Text } = jest.requireActual('react-native');
      return <Text testID="qr-code">{props.value}</Text>;
    },
);

const SESSION = {
  access_token: 'access-abc',
  refresh_token: 'refresh-xyz',
} as unknown as Session;

function fakeClient(
  session: Session | null,
  error: { message: string } | null = null,
): VisionClient {
  return {
    auth: {
      getSession: () => Promise.resolve({ data: { session }, error }),
    },
  } as unknown as VisionClient;
}

function fakeNavigation() {
  return { navigate: jest.fn(), goBack: jest.fn() } as unknown as Parameters<
    typeof ReonboardContainer
  >[0]['navigation'];
}

describe('ReonboardContainer', () => {
  it('builds a QR payload with exactly the fields firmware expects', async () => {
    await render(
      <ReonboardContainer
        client={fakeClient(SESSION)}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    await fireEvent.press(screen.getByTestId('generate-qr-button'));

    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
    const payload = JSON.parse(screen.getByTestId('qr-code').props.children);
    expect(payload).toEqual({
      ssid: 'HomeNet',
      password: 'hunter2',
      user_access_token: 'access-abc',
      user_refresh_token: 'refresh-xyz',
    });
  });

  it('shows the error step when getSession returns no session', async () => {
    await render(
      <ReonboardContainer
        client={fakeClient(null)}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    await fireEvent.press(screen.getByTestId('generate-qr-button'));

    await waitFor(() => expect(screen.getByTestId('reonboard-error')).toBeTruthy());
  });

  it('shows the error step when getSession errors', async () => {
    await render(
      <ReonboardContainer
        client={fakeClient(null, { message: 'network down' })}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    await fireEvent.press(screen.getByTestId('generate-qr-button'));

    await waitFor(() =>
      expect(screen.getByTestId('reonboard-error')).toHaveTextContent('network down'),
    );
  });

  it('shows the error step when getSession rejects instead of resolving', async () => {
    const rejectingClient = {
      auth: { getSession: () => Promise.reject(new Error('fetch failed')) },
    } as unknown as VisionClient;
    await render(
      <ReonboardContainer
        client={rejectingClient}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    await fireEvent.press(screen.getByTestId('generate-qr-button'));

    await waitFor(() =>
      expect(screen.getByTestId('reonboard-error')).toHaveTextContent('fetch failed'),
    );
  });

  it('ignores a second press while getSession is still in flight', async () => {
    let resolveSession: (value: { data: { session: Session | null }; error: null }) => void;
    const getSession = jest.fn(
      () =>
        new Promise<{ data: { session: Session | null }; error: null }>((resolve) => {
          resolveSession = resolve;
        }),
    );
    const client = { auth: { getSession } } as unknown as VisionClient;

    await render(
      <ReonboardContainer
        client={client}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    // fireEvent resolves only once the async onPress handler settles, and this
    // one is parked on getSession, so hold the first press open while the
    // second one lands on the now-disabled button.
    const firstPress = fireEvent.press(screen.getByTestId('generate-qr-button'));
    await waitFor(() =>
      expect(screen.getByTestId('generate-qr-button').props.accessibilityState.disabled).toBe(true),
    );
    await fireEvent.press(screen.getByTestId('generate-qr-button'));

    expect(getSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveSession!({ data: { session: SESSION }, error: null });
      await firstPress;
    });
    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
  });

  it('calls navigation.goBack() from onDone', async () => {
    const navigation = fakeNavigation();
    await render(
      <ReonboardContainer
        client={fakeClient(SESSION)}
        navigation={navigation}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    await fireEvent.press(screen.getByTestId('generate-qr-button'));
    await waitFor(() => expect(screen.getByTestId('done-button')).toBeTruthy());

    await fireEvent.press(screen.getByTestId('done-button'));
    expect(navigation.goBack).toHaveBeenCalled();
  });

  it('keeps the typed SSID/password after Try again', async () => {
    await render(
      <ReonboardContainer
        client={fakeClient(null)}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    await fireEvent.press(screen.getByTestId('generate-qr-button'));
    await waitFor(() => expect(screen.getByTestId('try-again-button')).toBeTruthy());

    await fireEvent.press(screen.getByTestId('try-again-button'));
    expect(screen.getByTestId('ssid-input').props.value).toBe('HomeNet');
    expect(screen.getByTestId('password-input').props.value).toBe('hunter2');
  });
});
