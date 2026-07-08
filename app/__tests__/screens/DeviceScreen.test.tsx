import { fireEvent, render } from '@testing-library/react-native';
import React from 'react';

import { DeviceScreen } from '../../src/screens/DeviceScreen';

const STATUS = {
  batteryPct: 72,
  storageUsedGb: 4.2,
  storageFreeGb: 118,
  segmentsPending: 1,
  segmentsUploadedToday: 42,
  recordingActive: true,
};

describe('DeviceScreen', () => {
  it('shows a loading state when status is null', () => {
    const { getByText } = render(<DeviceScreen status={null} onReonboardPress={jest.fn()} />);
    expect(getByText('Loading device status...')).toBeTruthy();
  });

  it('renders the battery percentage and recording state', () => {
    const { getByText } = render(<DeviceScreen status={STATUS} onReonboardPress={jest.fn()} />);
    expect(getByText('Battery: 72%')).toBeTruthy();
    expect(getByText('Recording')).toBeTruthy();
  });

  it('calls onReonboardPress when the button is pressed', () => {
    const onReonboardPress = jest.fn();
    const { getByTestId } = render(<DeviceScreen status={STATUS} onReonboardPress={onReonboardPress} />);
    fireEvent.press(getByTestId('reonboard-button'));
    expect(onReonboardPress).toHaveBeenCalledTimes(1);
  });
});
