import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';

import { ReonboardScreen } from '../../src/screens/ReonboardScreen';

jest.mock(
  'react-native-qrcode-svg',
  () =>
    function MockQrCode(props: { value: string; size?: number; ecl?: string }) {
      const { Text } = jest.requireActual('react-native');
      return (
        <Text testID="qr-code" accessibilityValue={{ text: `${props.size}:${props.ecl}` }}>
          {props.value}
        </Text>
      );
    },
);

function noop() {}

describe('ReonboardScreen', () => {
  it('renders the form step with SSID and password inputs', () => {
    render(
      <ReonboardScreen
        step="form"
        ssid=""
        password=""
        qrValue=""
        errorMessage=""
        isSubmitting={false}
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={noop}
        onRetry={noop}
      />,
    );
    expect(screen.getByTestId('ssid-input')).toBeTruthy();
    expect(screen.getByTestId('password-input')).toBeTruthy();
  });

  it('disables Generate QR until both fields are filled', () => {
    render(
      <ReonboardScreen
        step="form"
        ssid=""
        password=""
        qrValue=""
        errorMessage=""
        isSubmitting={false}
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={noop}
        onRetry={noop}
      />,
    );
    expect(screen.getByTestId('generate-qr-button').props.accessibilityState.disabled).toBe(true);
  });

  it('disables Generate QR and shows a submitting label while isSubmitting is true', () => {
    render(
      <ReonboardScreen
        step="form"
        ssid="HomeNet"
        password="hunter2"
        qrValue=""
        errorMessage=""
        isSubmitting={true}
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={noop}
        onRetry={noop}
      />,
    );
    const button = screen.getByTestId('generate-qr-button');
    expect(button.props.accessibilityState.disabled).toBe(true);
    expect(screen.getByText('Generating...')).toBeTruthy();
  });

  it('calls onSubmit with the typed SSID and password', () => {
    const onSubmit = jest.fn();
    render(
      <ReonboardScreen
        step="form"
        ssid="HomeNet"
        password="hunter2"
        qrValue=""
        errorMessage=""
        isSubmitting={false}
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={onSubmit}
        onDone={noop}
        onRetry={noop}
      />,
    );
    fireEvent.press(screen.getByTestId('generate-qr-button'));
    expect(onSubmit).toHaveBeenCalledWith('HomeNet', 'hunter2');
  });

  it('reports typed input via onSsidChange/onPasswordChange', () => {
    const onSsidChange = jest.fn();
    const onPasswordChange = jest.fn();
    render(
      <ReonboardScreen
        step="form"
        ssid=""
        password=""
        qrValue=""
        errorMessage=""
        isSubmitting={false}
        onSsidChange={onSsidChange}
        onPasswordChange={onPasswordChange}
        onSubmit={noop}
        onDone={noop}
        onRetry={noop}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    expect(onSsidChange).toHaveBeenCalledWith('HomeNet');
    expect(onPasswordChange).toHaveBeenCalledWith('hunter2');
  });

  it('renders the QR code with exactly qrValue on the ready step, plus the privacy hint', () => {
    render(
      <ReonboardScreen
        step="ready"
        ssid="HomeNet"
        password="hunter2"
        qrValue='{"ssid":"HomeNet"}'
        errorMessage=""
        isSubmitting={false}
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={noop}
        onRetry={noop}
      />,
    );
    expect(screen.getByTestId('qr-code')).toHaveTextContent('{"ssid":"HomeNet"}');
    expect(screen.getByTestId('qr-code').props.accessibilityValue.text).toBe('260:L');
    expect(
      screen.getByText(
        "This code contains your WiFi password and an active login - only show it to your Visio device's camera",
      ),
    ).toBeTruthy();
  });

  it('calls onDone when Done is pressed on the ready step', () => {
    const onDone = jest.fn();
    render(
      <ReonboardScreen
        step="ready"
        ssid="HomeNet"
        password="hunter2"
        qrValue='{"ssid":"HomeNet"}'
        errorMessage=""
        isSubmitting={false}
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={onDone}
        onRetry={noop}
      />,
    );
    fireEvent.press(screen.getByTestId('done-button'));
    expect(onDone).toHaveBeenCalled();
  });

  it('shows the error message and calls onRetry on the error step', () => {
    const onRetry = jest.fn();
    render(
      <ReonboardScreen
        step="error"
        ssid="HomeNet"
        password="hunter2"
        qrValue=""
        errorMessage="No active session - please sign in again."
        isSubmitting={false}
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={noop}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByTestId('reonboard-error')).toHaveTextContent(
      'No active session - please sign in again.',
    );
    fireEvent.press(screen.getByTestId('try-again-button'));
    expect(onRetry).toHaveBeenCalled();
  });
});
