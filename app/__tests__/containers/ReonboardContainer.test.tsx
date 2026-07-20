import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { Session, SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

jest.mock('react-native-qrcode-svg', () => (props: { value: string }) => {
  const { Text } = jest.requireActual('react-native');
  return <Text testID="qr-code">{props.value}</Text>;
});

import { ReonboardContainer } from '../../src/containers/ReonboardContainer';

const SESSION = {
  access_token: 'access-abc',
  refresh_token: 'refresh-xyz',
} as unknown as Session;

function fakeClient(session: Session | null, error: { message: string } | null = null): SupabaseClient {
  return {
    auth: {
      getSession: () => Promise.resolve({ data: { session }, error }),
    },
  } as unknown as SupabaseClient;
}

function fakeNavigation() {
  return { navigate: jest.fn(), goBack: jest.fn() } as unknown as Parameters<
    typeof ReonboardContainer
  >[0]['navigation'];
}

describe('ReonboardContainer', () => {
  it('builds a QR payload with exactly the fields firmware expects', async () => {
    render(
      <ReonboardContainer
        client={fakeClient(SESSION)}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    fireEvent.press(screen.getByTestId('generate-qr-button'));

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
    render(
      <ReonboardContainer
        client={fakeClient(null)}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    fireEvent.press(screen.getByTestId('generate-qr-button'));

    await waitFor(() => expect(screen.getByTestId('reonboard-error')).toBeTruthy());
  });

  it('shows the error step when getSession errors', async () => {
    render(
      <ReonboardContainer
        client={fakeClient(null, { message: 'network down' })}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    fireEvent.press(screen.getByTestId('generate-qr-button'));

    await waitFor(() =>
      expect(screen.getByTestId('reonboard-error')).toHaveTextContent('network down'),
    );
  });

  it('calls navigation.goBack() from onDone', async () => {
    const navigation = fakeNavigation();
    render(
      <ReonboardContainer
        client={fakeClient(SESSION)}
        navigation={navigation}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    fireEvent.press(screen.getByTestId('generate-qr-button'));
    await waitFor(() => expect(screen.getByTestId('done-button')).toBeTruthy());

    fireEvent.press(screen.getByTestId('done-button'));
    expect(navigation.goBack).toHaveBeenCalled();
  });

  it('keeps the typed SSID/password after Try again', async () => {
    render(
      <ReonboardContainer
        client={fakeClient(null)}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    fireEvent.press(screen.getByTestId('generate-qr-button'));
    await waitFor(() => expect(screen.getByTestId('try-again-button')).toBeTruthy());

    fireEvent.press(screen.getByTestId('try-again-button'));
    expect(screen.getByTestId('ssid-input').props.value).toBe('HomeNet');
    expect(screen.getByTestId('password-input').props.value).toBe('hunter2');
  });
});
