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
  it('renders the form step with SSID and password inputs', async () => {
    await render(
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
        onNewCode={noop}
      />,
    );
    expect(screen.getByTestId('ssid-input')).toBeTruthy();
    expect(screen.getByTestId('password-input')).toBeTruthy();
  });

  it('disables Generate QR until both fields are filled', async () => {
    await render(
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
        onNewCode={noop}
      />,
    );
    expect(screen.getByTestId('generate-qr-button').props.accessibilityState.disabled).toBe(true);
  });

  it('disables Generate QR and shows a submitting label while isSubmitting is true', async () => {
    await render(
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
        onNewCode={noop}
      />,
    );
    const button = screen.getByTestId('generate-qr-button');
    expect(button.props.accessibilityState.disabled).toBe(true);
    expect(screen.getByText('Generating...')).toBeTruthy();
  });

  it('calls onSubmit with the typed SSID and password', async () => {
    const onSubmit = jest.fn();
    await render(
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
        onNewCode={noop}
      />,
    );
    await fireEvent.press(screen.getByTestId('generate-qr-button'));
    expect(onSubmit).toHaveBeenCalledWith('HomeNet', 'hunter2');
  });

  it('reports typed input via onSsidChange/onPasswordChange', async () => {
    const onSsidChange = jest.fn();
    const onPasswordChange = jest.fn();
    await render(
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
        onNewCode={noop}
      />,
    );
    await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    expect(onSsidChange).toHaveBeenCalledWith('HomeNet');
    expect(onPasswordChange).toHaveBeenCalledWith('hunter2');
  });

  it('renders the QR code with exactly qrValue on the ready step, plus the privacy hint', async () => {
    await render(
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
        onNewCode={noop}
      />,
    );
    expect(screen.getByTestId('qr-code')).toHaveTextContent('{"ssid":"HomeNet"}');
    // The payload is under 150 characters now, so the library's default error
    // correction scans fine; the low-ECL workaround for two JWTs is gone.
    expect(screen.getByTestId('qr-code').props.accessibilityValue.text).toBe('260:undefined');
    expect(
      screen.getByText(
        "This code contains your Wi-Fi password and a one-time pairing code. Only show it to your Visio pendant's camera.",
      ),
    ).toBeTruthy();
    expect(screen.getByText('Waiting for your pendant to scan this code...')).toBeTruthy();
  });

  it('calls onDone when Done is pressed on the ready step', async () => {
    const onDone = jest.fn();
    await render(
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
        onNewCode={noop}
      />,
    );
    await fireEvent.press(screen.getByTestId('done-button'));
    expect(onDone).toHaveBeenCalled();
  });

  it('shows the error message and calls onRetry on the error step', async () => {
    const onRetry = jest.fn();
    await render(
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
        onNewCode={noop}
      />,
    );
    expect(screen.getByTestId('reonboard-error')).toHaveTextContent(
      'No active session - please sign in again.',
    );
    await fireEvent.press(screen.getByTestId('try-again-button'));
    expect(onRetry).toHaveBeenCalled();
  });

  it('confirms the pairing on the paired step and calls onDone from Done', async () => {
    const onDone = jest.fn();
    await render(
      <ReonboardScreen
        step="paired"
        ssid="HomeNet"
        password="hunter2"
        qrValue=""
        errorMessage=""
        isSubmitting={false}
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={onDone}
        onRetry={noop}
        onNewCode={noop}
      />,
    );
    expect(screen.getByTestId('paired-message')).toHaveTextContent('Pendant paired');
    expect(screen.queryByTestId('qr-code')).toBeNull();
    await fireEvent.press(screen.getByTestId('done-button'));
    expect(onDone).toHaveBeenCalled();
  });

  it('explains an expired code and offers a new one', async () => {
    const onNewCode = jest.fn();
    await render(
      <ReonboardScreen
        step="expired"
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
        onNewCode={onNewCode}
      />,
    );
    expect(screen.getByTestId('expired-message')).toHaveTextContent('This code has expired');
    expect(screen.queryByTestId('qr-code')).toBeNull();
    await fireEvent.press(screen.getByTestId('new-code-button'));
    expect(onNewCode).toHaveBeenCalled();
  });

  it('explains the form and lets the user back out of it', async () => {
    const onDone = jest.fn();
    await render(
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
        onDone={onDone}
        onRetry={noop}
        onNewCode={noop}
      />,
    );
    expect(screen.getByText('Pair your pendant')).toBeTruthy();
    await fireEvent.press(screen.getByTestId('cancel-button'));
    expect(onDone).toHaveBeenCalled();
  });

  it.each(['expired', 'error'] as const)('lets the user back out of the %s step', async (step) => {
    const onDone = jest.fn();
    await render(
      <ReonboardScreen
        step={step}
        ssid="HomeNet"
        password="hunter2"
        qrValue=""
        errorMessage="boom"
        isSubmitting={false}
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={onDone}
        onRetry={noop}
        onNewCode={noop}
      />,
    );
    await fireEvent.press(screen.getByTestId('cancel-button'));
    expect(onDone).toHaveBeenCalled();
  });
});
