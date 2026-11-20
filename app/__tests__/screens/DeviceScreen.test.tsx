import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';

import { DeviceScreen } from '../../src/screens/DeviceScreen';
import type { DeviceScreenProps } from '../../src/screens/DeviceScreen';

const READY = {
  kind: 'ready' as const,
  status: {
    batteryPct: 72,
    storageUsedGb: 4.2,
    storageFreeGb: 118,
    segmentsPending: 1,
    segmentsUploadedToday: 12,
    recordingActive: true,
  },
  realtime: 'live' as const,
};

function props(overrides: Partial<DeviceScreenProps> = {}): DeviceScreenProps {
  return {
    state: READY,
    paired: true,
    removal: { kind: 'idle' },
    onReonboardPress: jest.fn(),
    onRemovePress: jest.fn(),
    onConfirmRemove: jest.fn(),
    onCancelRemove: jest.fn(),
    ...overrides,
  };
}

describe('DeviceScreen', () => {
  it('shows a loading state', async () => {
    await render(<DeviceScreen {...props({ state: { kind: 'loading' } })} />);
    expect(screen.getByText('Loading device status...')).toBeTruthy();
  });

  it('shows an error state', async () => {
    await render(
      <DeviceScreen {...props({ state: { kind: 'error', message: 'network down' } })} />,
    );
    expect(screen.getByTestId('device-error')).toHaveTextContent('network down');
  });

  it('renders status fields when ready and live, without a stale banner', async () => {
    await render(<DeviceScreen {...props()} />);
    expect(screen.getByText('Battery: 72%')).toBeTruthy();
    expect(screen.getByText('Recording')).toBeTruthy();
    expect(screen.queryByTestId('stale-banner')).toBeNull();
  });

  it('shows a stale banner when realtime is stale', async () => {
    await render(<DeviceScreen {...props({ state: { ...READY, realtime: 'stale' } })} />);
    expect(screen.getByTestId('stale-banner')).toHaveTextContent(
      'Live updates disconnected - data may be stale',
    );
  });

  it('labels the re-onboard button for accessibility', async () => {
    await render(<DeviceScreen {...props()} />);
    expect(screen.getByLabelText('Re-onboard device WiFi')).toBeTruthy();
  });

  it('offers Remove device for a paired pendant', async () => {
    const onRemovePress = jest.fn();
    await render(<DeviceScreen {...props({ onRemovePress })} />);
    expect(screen.queryByTestId('not-paired')).toBeNull();
    await fireEvent.press(screen.getByTestId('remove-device-button'));
    expect(onRemovePress).toHaveBeenCalled();
  });

  it('asks for confirmation before removing', async () => {
    const onConfirmRemove = jest.fn();
    const onCancelRemove = jest.fn();
    await render(
      <DeviceScreen
        {...props({ removal: { kind: 'confirming' }, onConfirmRemove, onCancelRemove })}
      />,
    );
    expect(screen.getByTestId('remove-confirmation')).toHaveTextContent(
      'The pendant will be signed out. Your footage stays in your account.',
      { exact: false },
    );
    expect(screen.queryByTestId('remove-device-button')).toBeNull();
    await fireEvent.press(screen.getByTestId('cancel-remove-button'));
    expect(onCancelRemove).toHaveBeenCalled();
    await fireEvent.press(screen.getByTestId('confirm-remove-button'));
    expect(onConfirmRemove).toHaveBeenCalled();
  });

  it('disables the confirmation while the removal is in flight', async () => {
    await render(<DeviceScreen {...props({ removal: { kind: 'removing' } })} />);
    expect(screen.getByTestId('confirm-remove-button').props.accessibilityState.disabled).toBe(
      true,
    );
    expect(screen.getByText('Removing...')).toBeTruthy();
  });

  it('shows a removal error', async () => {
    await render(
      <DeviceScreen {...props({ removal: { kind: 'error', message: 'device not found' } })} />,
    );
    expect(screen.getByTestId('remove-error')).toHaveTextContent('device not found');
  });

  it('shows Not paired with a way to pair when the pendant was removed', async () => {
    const onReonboardPress = jest.fn();
    await render(<DeviceScreen {...props({ paired: false, onReonboardPress })} />);
    expect(screen.getByTestId('not-paired')).toHaveTextContent('Not paired', { exact: false });
    expect(screen.queryByTestId('remove-device-button')).toBeNull();
    expect(screen.queryByTestId('reonboard-button')).toBeNull();
    await fireEvent.press(screen.getByTestId('pair-button'));
    expect(onReonboardPress).toHaveBeenCalled();
  });

  it('does not sit on a loading message for a removed pendant with no status', async () => {
    await render(<DeviceScreen {...props({ paired: false, state: { kind: 'loading' } })} />);
    expect(screen.getByTestId('not-paired')).toBeTruthy();
    expect(screen.queryByText('Loading device status...')).toBeNull();
  });
});
