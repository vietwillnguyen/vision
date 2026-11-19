import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import React from 'react';

import { DeviceContainer } from '../../src/containers/DeviceContainer';
import type { VisionClient } from '../../src/lib/supabase';

const ROW = {
  battery_pct: 55,
  storage_used_gb: 1,
  storage_free_gb: 10,
  segments_pending: 0,
  segments_uploaded_today: 3,
  recording_active: false,
};

function fakeClient(
  row: Record<string, unknown>,
  rpc: jest.Mock = jest.fn(() => Promise.resolve({ data: null, error: null })),
): VisionClient {
  return {
    from: () => ({
      select: () => ({ eq: () => ({ single: () => Promise.resolve({ data: row, error: null }) }) }),
    }),
    channel: () => ({ on: () => ({ subscribe: () => ({}) }) }),
    removeChannel: () => {},
    rpc,
  } as unknown as VisionClient;
}

function fakeNavigation() {
  return { navigate: jest.fn(), goBack: jest.fn() } as unknown as Parameters<
    typeof DeviceContainer
  >[0]['navigation'];
}

it('wires useDeviceStatus into DeviceScreen', async () => {
  await render(
    <DeviceContainer
      client={fakeClient({
        battery_pct: 55,
        storage_used_gb: 1,
        storage_free_gb: 10,
        segments_pending: 0,
        segments_uploaded_today: 3,
        recording_active: false,
      })}
      deviceId="dev-1"
      paired
      onDeviceChanged={jest.fn()}
      navigation={fakeNavigation()}
      route={{ key: 'DeviceHome', name: 'DeviceHome' }}
    />,
  );
  await waitFor(() => expect(screen.getByText('Battery: 55%')).toBeTruthy());
  expect(screen.getByText('Paused')).toBeTruthy();
});

it('navigates to Reonboard when the re-onboard button is pressed', async () => {
  const navigation = fakeNavigation();
  await render(
    <DeviceContainer
      client={fakeClient({
        battery_pct: 55,
        storage_used_gb: 1,
        storage_free_gb: 10,
        segments_pending: 0,
        segments_uploaded_today: 3,
        recording_active: false,
      })}
      deviceId="dev-1"
      paired
      onDeviceChanged={jest.fn()}
      navigation={navigation}
      route={{ key: 'DeviceHome', name: 'DeviceHome' }}
    />,
  );
  await waitFor(() => expect(screen.getByTestId('reonboard-button')).toBeTruthy());
  await fireEvent.press(screen.getByTestId('reonboard-button'));
  expect(navigation.navigate).toHaveBeenCalledWith('Reonboard');
});

it('keeps showing the last known status when the status row is deleted', async () => {
  // A realtime DELETE carries the vanished row in `old` and an empty `new`;
  // mapping that `new` would put a screenful of `undefined` in front of the user.
  let deliver: ((payload: { eventType: string; new: object; old: object }) => void) | undefined;
  const row = {
    battery_pct: 55,
    storage_used_gb: 1,
    storage_free_gb: 10,
    segments_pending: 0,
    segments_uploaded_today: 3,
    recording_active: false,
  };
  const client = {
    from: () => ({
      select: () => ({ eq: () => ({ single: () => Promise.resolve({ data: row, error: null }) }) }),
    }),
    channel: () => ({
      on: (_type: string, _filter: object, callback: typeof deliver) => {
        deliver = callback;
        return { subscribe: () => ({}) };
      },
    }),
    removeChannel: () => {},
  } as unknown as VisionClient;

  await render(
    <DeviceContainer
      client={client}
      deviceId="dev-1"
      paired
      onDeviceChanged={jest.fn()}
      navigation={fakeNavigation()}
      route={{ key: 'DeviceHome', name: 'DeviceHome' }}
    />,
  );
  await waitFor(() => expect(screen.getByText('Battery: 55%')).toBeTruthy());

  await act(() => deliver?.({ eventType: 'DELETE', new: {}, old: row }));

  expect(screen.getByText('Battery: 55%')).toBeTruthy();
  expect(screen.queryByText('Battery: undefined%')).toBeNull();
});

async function renderPaired(rpc: jest.Mock, onDeviceChanged = jest.fn()) {
  await render(
    <DeviceContainer
      client={fakeClient(ROW, rpc)}
      deviceId="dev-1"
      paired
      onDeviceChanged={onDeviceChanged}
      navigation={fakeNavigation()}
      route={{ key: 'DeviceHome', name: 'DeviceHome' }}
    />,
  );
  await waitFor(() => expect(screen.getByTestId('remove-device-button')).toBeTruthy());
}

it('removes the device only after confirmation, then refreshes it', async () => {
  const rpc = jest.fn(() => Promise.resolve({ data: null, error: null }));
  const onDeviceChanged = jest.fn();
  await renderPaired(rpc, onDeviceChanged);

  await fireEvent.press(screen.getByTestId('remove-device-button'));
  expect(rpc).not.toHaveBeenCalled();
  await fireEvent.press(screen.getByTestId('confirm-remove-button'));

  expect(rpc).toHaveBeenCalledWith('unpair_device', { p_device_id: 'dev-1' });
  await waitFor(() => expect(onDeviceChanged).toHaveBeenCalled());
  expect(screen.queryByTestId('remove-confirmation')).toBeNull();
});

it('cancelling the confirmation calls nothing', async () => {
  const rpc = jest.fn();
  await renderPaired(rpc);

  await fireEvent.press(screen.getByTestId('remove-device-button'));
  await fireEvent.press(screen.getByTestId('cancel-remove-button'));

  expect(rpc).not.toHaveBeenCalled();
  expect(screen.getByTestId('remove-device-button')).toBeTruthy();
});

it('shows an error from the removal', async () => {
  const rpc = jest.fn(() =>
    Promise.resolve({ data: null, error: { message: 'device not found' } }),
  );
  const onDeviceChanged = jest.fn();
  await renderPaired(rpc, onDeviceChanged);

  await fireEvent.press(screen.getByTestId('remove-device-button'));
  await fireEvent.press(screen.getByTestId('confirm-remove-button'));

  await waitFor(() =>
    expect(screen.getByTestId('remove-error')).toHaveTextContent('device not found'),
  );
  expect(onDeviceChanged).not.toHaveBeenCalled();
});

it('shows an error when the removal request fails outright', async () => {
  const rpc = jest.fn(() => Promise.reject(new Error('fetch failed')));
  await renderPaired(rpc);

  await fireEvent.press(screen.getByTestId('remove-device-button'));
  await fireEvent.press(screen.getByTestId('confirm-remove-button'));

  await waitFor(() => expect(screen.getByTestId('remove-error')).toHaveTextContent('fetch failed'));
});
