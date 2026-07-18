import { render, screen } from '@testing-library/react-native';
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
  it('shows a loading state', () => {
    render(<DeviceScreen state={{ kind: 'loading' }} onReonboardPress={jest.fn()} />);
    expect(screen.getByText('Loading device status...')).toBeTruthy();
  });

  it('shows an error state', () => {
    render(
      <DeviceScreen state={{ kind: 'error', message: 'network down' }} onReonboardPress={jest.fn()} />,
    );
    expect(screen.getByTestId('device-error')).toHaveTextContent('network down');
  });

  it('renders status fields when ready and live, without a stale banner', () => {
    render(<DeviceScreen state={READY} onReonboardPress={jest.fn()} />);
    expect(screen.getByText('Battery: 72%')).toBeTruthy();
    expect(screen.getByText('Recording')).toBeTruthy();
    expect(screen.queryByTestId('stale-banner')).toBeNull();
  });

  it('shows a stale banner when realtime is stale', () => {
    render(<DeviceScreen state={{ ...READY, realtime: 'stale' }} onReonboardPress={jest.fn()} />);
    expect(screen.getByTestId('stale-banner')).toHaveTextContent(
      'Live updates disconnected - data may be stale',
    );
  });

  it('labels the re-onboard button for accessibility', () => {
    render(<DeviceScreen state={READY} onReonboardPress={jest.fn()} />);
    expect(screen.getByLabelText('Re-onboard device WiFi')).toBeTruthy();
  });
});
