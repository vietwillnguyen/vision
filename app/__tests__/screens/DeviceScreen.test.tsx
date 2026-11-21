import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';

import { DeviceScreen } from '../../src/screens/DeviceScreen';

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

describe('DeviceScreen', () => {
  it('shows a loading state', async () => {
    await render(<DeviceScreen state={{ kind: 'loading' }} onReonboardPress={jest.fn()} />);
    expect(screen.getByText('Loading device status...')).toBeTruthy();
  });

  it('shows an error state', async () => {
    await render(
      <DeviceScreen
        state={{ kind: 'error', message: 'network down' }}
        onReonboardPress={jest.fn()}
      />,
    );
    expect(screen.getByTestId('device-error')).toHaveTextContent('network down');
  });

  it('renders status fields when ready and live, without a stale banner', async () => {
    await render(<DeviceScreen state={READY} onReonboardPress={jest.fn()} />);
    expect(screen.getByText('Battery: 72%')).toBeTruthy();
    expect(screen.getByText('Recording')).toBeTruthy();
    expect(screen.queryByTestId('stale-banner')).toBeNull();
  });

  it('shows a stale banner when realtime is stale', async () => {
    await render(
      <DeviceScreen state={{ ...READY, realtime: 'stale' }} onReonboardPress={jest.fn()} />,
    );
    expect(screen.getByTestId('stale-banner')).toHaveTextContent(
      'Live updates disconnected - data may be stale',
    );
  });

  it('labels the re-onboard button for accessibility', async () => {
    await render(<DeviceScreen state={READY} onReonboardPress={jest.fn()} />);
    expect(screen.getByLabelText('Re-onboard device WiFi')).toBeTruthy();
  });

  it('disables Wi-Fi setup for a read-only account and explains why', async () => {
    const onReonboardPress = jest.fn();
    await render(
      <DeviceScreen
        state={READY}
        onReonboardPress={onReonboardPress}
        deviceName="Visio bench prototype"
        readOnly
      />,
    );
    expect(screen.getByText('Visio bench prototype')).toBeTruthy();
    expect(screen.getByTestId('reonboard-disabled-note')).toHaveTextContent(/read-only demo/);
    await fireEvent.press(screen.getByTestId('reonboard-button'));
    expect(onReonboardPress).not.toHaveBeenCalled();
  });
});
